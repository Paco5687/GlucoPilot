"""Sign-in audit log: provider logins are recorded, attributed and summarized."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from server import auth, db, login_log
from server.migrations import run_migrations

pytestmark = pytest.mark.risk_critical

CHROME_MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
TRAEFIK = ("172.23.255.254", 41000)
CF_EDGE = "162.158.1.20"
# Public addresses: the RFC 5737 documentation ranges count as private.
VISITOR = "81.2.69.160"


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "data" / "app.sqlite3"
    path.parent.mkdir()
    run_migrations(path)
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.delenv("APP_USERNAME", raising=False)
    monkeypatch.delenv("APP_PASSWORD_HASH", raising=False)
    db.set_setting("admin_username", "owner")
    db.set_setting("admin_password_hash", auth.make_password_hash("owner-password-123"))
    auth.save_providers([{"username": "drlee", "password_hash": auth.make_password_hash("provider-pass")}])
    return path


def _request(
    *,
    forwarded_for: str | None = f"{VISITOR}, {CF_EDGE}",
    client=TRAEFIK,
    session: dict | None = None,
    body: dict | None = None,
) -> Request:
    headers = [(b"user-agent", CHROME_MAC.encode())]
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/login",
        "query_string": b"",
        "headers": headers,
        "client": client,
        "session": {} if session is None else session,
    }
    payload = json.dumps(body or {}).encode()

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope, receive)


@pytest.mark.parametrize(
    ("forwarded_for", "client", "expected"),
    [
        # Cloudflare -> Traefik: the visitor sits left of the Cloudflare edge.
        (f"{VISITOR}, {CF_EDGE}", TRAEFIK, VISITOR),
        # A planted leftmost entry cannot displace the real visitor.
        (f"93.184.216.34, {VISITOR}, {CF_EDGE}", TRAEFIK, VISITOR),
        # Direct to Traefik: it overwrites the header with the TCP peer.
        (VISITOR, TRAEFIK, VISITOR),
        # From the LAN every hop is private; report the origin, not the proxy.
        ("10.27.28.1", TRAEFIK, "10.27.28.1"),
        # No proxy at all.
        (None, (VISITOR, 5000), VISITOR),
        # Garbage entries are skipped.
        (f"not-an-ip, {VISITOR}", TRAEFIK, VISITOR),
    ],
)
def test_client_ip_is_rightmost_hop_that_is_not_a_proxy(forwarded_for, client, expected):
    assert login_log.client_ip(_request(forwarded_for=forwarded_for, client=client)) == expected


def test_provider_login_is_recorded_and_summarized(database):
    request = _request()
    response = auth.login(request, username="drlee", password="provider-pass")

    assert response.status_code == 303
    [event] = login_log.recent_events()
    assert event["event"] == "login"
    assert event["role"] == "provider"
    assert event["username"] == "drlee"
    assert event["client_ip"] == VISITOR
    assert event["user_agent"] == CHROME_MAC

    [summary] = login_log.provider_activity(["drlee"])
    assert summary["logins_30d"] == 1
    assert summary["logins_total"] == 1
    assert summary["last_login"] == event["occurred_at"]


def test_summary_separates_recent_from_older_logins(database):
    now = datetime.now(timezone.utc)
    with db.connect() as connection:
        for days_ago in (2, 10, 45):
            connection.execute(
                "INSERT INTO login_events (occurred_at, event, role, username) VALUES (?, 'login', 'provider', 'drlee')",
                (login_log._iso(now - timedelta(days=days_ago)),),
            )
        # Failed attempts and other accounts do not count as drlee's sign-ins.
        connection.execute(
            "INSERT INTO login_events (occurred_at, event, role, username) VALUES (?, 'login_failed', 'provider', 'drlee')",
            (login_log._iso(now),),
        )
        connection.execute(
            "INSERT INTO login_events (occurred_at, event, role, username) VALUES (?, 'login', 'provider', 'drpatel')",
            (login_log._iso(now),),
        )

    drlee, never = login_log.provider_activity(["drlee", "drnobody"], now=now)
    assert (drlee["logins_30d"], drlee["logins_total"]) == (2, 3)
    assert drlee["last_login"] == login_log._iso(now - timedelta(days=2))
    assert never == {"username": "drnobody", "last_login": None, "logins_30d": 0, "logins_total": 0}


def test_failed_logins_name_only_real_accounts(database):
    auth.login(_request(), username="drlee", password="wrong-password")
    auth.login(_request(), username="Owner", password="wrong-password")
    # A password typed into the username box must never reach the log.
    auth.login(_request(), username="provider-pass", password="x")

    events = list(reversed(login_log.recent_events()))
    assert [(e["event"], e["role"], e["username"]) for e in events] == [
        ("login_failed", "provider", "drlee"),
        ("login_failed", "admin", "owner"),
        ("login_failed", None, None),
    ]
    assert "provider-pass" not in json.dumps(events)
    assert login_log.provider_activity(["drlee"])[0]["logins_total"] == 0


def test_admin_login_and_logout_are_attributed(database):
    session: dict = {}
    auth.login(_request(session=session), username="owner", password="owner-password-123")
    auth.logout(_request(session=session))
    # A logout without a session is not an event.
    auth.logout(_request(session={}))

    events = list(reversed(login_log.recent_events()))
    assert [(e["event"], e["role"], e["username"]) for e in events] == [
        ("login", "admin", "owner"),
        ("logout", "admin", "owner"),
    ]


def test_provider_logout_is_attributed(database):
    session: dict = {}
    auth.login(_request(session=session), username="drlee", password="provider-pass")
    auth.api_logout(_request(session=session))

    assert login_log.recent_events()[0] | {"occurred_at": None} == {
        "occurred_at": None,
        "event": "logout",
        "role": "provider",
        "username": "drlee",
        "client_ip": VISITOR,
        "user_agent": CHROME_MAC,
    }


def test_invite_acceptance_is_recorded(database):
    token, _ = auth.create_provider_invite()
    questions = [
        {"question": "Name of my first clinic?", "answer": "Riverside"},
        {"question": "Street I grew up on?", "answer": "Maple Ave"},
        {"question": "First concert I attended?", "answer": "The Eagles"},
    ]
    body = {"token": token, "username": " dr-rivera ", "password": "a-strong-pass", "questions": questions}
    asyncio.run(auth.provider_invite_accept(_request(body=body)))

    [event] = login_log.recent_events()
    assert (event["event"], event["role"], event["username"]) == ("invite_accepted", "provider", "dr-rivera")


def test_events_past_retention_are_pruned(database):
    stale = login_log._iso(datetime.now(timezone.utc) - timedelta(days=login_log.RETENTION_DAYS + 1))
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO login_events (occurred_at, event, role, username) VALUES (?, 'login', 'provider', 'drlee')",
            (stale,),
        )
    auth.login(_request(), username="drlee", password="provider-pass")

    [kept] = login_log.recent_events()
    assert kept["occurred_at"] != stale


def test_a_logging_failure_never_blocks_sign_in(database):
    with db.connect() as connection:
        connection.execute("DROP TABLE login_events")
    response = auth.login(_request(), username="drlee", password="provider-pass")
    assert response.status_code == 303


def test_activity_endpoint_is_admin_only(database):
    auth.login(_request(), username="drlee", password="provider-pass")

    provider_session = {"logged_in": True, "role": "provider", "provider_name": "drlee"}
    with pytest.raises(HTTPException) as denied:
        auth.provider_activity_get(_request(session=provider_session))
    assert denied.value.status_code == 403

    payload = auth.provider_activity_get(_request(session={"logged_in": True, "role": "admin"}))
    assert payload["recording_since"]
    assert [p["username"] for p in payload["providers"]] == ["drlee"]
    assert payload["providers"][0]["logins_30d"] == 1
    assert payload["events"][0]["event"] == "login"
