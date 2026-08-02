"""Provider invite links: single-use, expiring, and never stored in the clear."""

from __future__ import annotations

import time

import pytest
from fastapi import HTTPException

from server import auth, db
from server.migrations import run_migrations

pytestmark = pytest.mark.risk_critical

QUESTIONS = [
    {"question": "Name of my first clinic?", "answer": "Riverside"},
    {"question": "Street I grew up on?", "answer": "Maple Ave"},
    {"question": "First concert I attended?", "answer": "The Eagles"},
]


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "data" / "app.sqlite3"
    path.parent.mkdir()
    run_migrations(path)
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


def test_invite_roundtrip_creates_provider_and_consumes_token(database):
    token, invite = auth.create_provider_invite()

    # The stored record carries only the hash, never the token.
    stored = auth.load_invites()
    assert len(stored) == 1
    assert token not in str(stored)
    assert stored[0]["token_hash"] == auth._invite_hash(token)

    auth.accept_provider_invite(token, "dr-rivera", "a-strong-pass", QUESTIONS)

    providers = auth.load_providers()
    assert [p["username"] for p in providers] == ["dr-rivera"]
    assert providers[0]["password_hash"]
    # Consumed: the same link cannot mint a second login.
    assert auth.load_invites() == []
    with pytest.raises(HTTPException) as reused:
        auth.accept_provider_invite(token, "dr-other", "another-pass", QUESTIONS)
    assert reused.value.status_code == 410


def test_expired_invites_vanish(database):
    token, _ = auth.create_provider_invite()
    invites = auth.load_invites()
    invites[0]["expires_at"] = int(time.time()) - 1
    auth.save_invites(invites)

    assert auth.load_invites() == []
    with pytest.raises(HTTPException) as expired:
        auth.accept_provider_invite(token, "dr-late", "some-password", QUESTIONS)
    assert expired.value.status_code == 410


def test_username_rules_hold_at_accept_time(database):
    auth.save_providers([{"username": "Bill", "password_hash": "x"}])

    token, _ = auth.create_provider_invite()
    with pytest.raises(HTTPException) as taken:
        auth.accept_provider_invite(token, "bill", "long-enough-pass", QUESTIONS)
    assert "taken" in taken.value.detail

    # A failed accept must not consume the invite.
    assert len(auth.load_invites()) == 1

    with pytest.raises(HTTPException):
        auth.accept_provider_invite(token, "admin", "long-enough-pass", QUESTIONS)
    with pytest.raises(HTTPException):
        auth.accept_provider_invite(token, "x", "long-enough-pass", QUESTIONS)
    with pytest.raises(HTTPException):
        auth.accept_provider_invite(token, "dr-fine", "short", QUESTIONS)

    # The invite survives every rejection and still works for a valid signup.
    auth.accept_provider_invite(token, "dr-fine", "long-enough-pass", QUESTIONS)
    assert {p["username"] for p in auth.load_providers()} == {"Bill", "dr-fine"}


def test_capacity_enforced_at_accept(database):
    auth.save_providers([
        {"username": f"dr-{i}", "password_hash": "x"} for i in range(auth.MAX_PROVIDERS)
    ])
    token, _ = auth.create_provider_invite()
    with pytest.raises(HTTPException) as full:
        auth.accept_provider_invite(token, "dr-extra", "long-enough-pass", QUESTIONS)
    assert "Maximum" in full.value.detail


def test_revoke_by_public_id(database):
    token, invite = auth.create_provider_invite()
    public_id = invite["token_hash"][:12]

    auth.save_invites([i for i in auth.load_invites() if i["token_hash"][:12] != public_id])

    assert auth.load_invites() == []
    with pytest.raises(HTTPException) as revoked:
        auth.accept_provider_invite(token, "dr-revoked", "long-enough-pass", QUESTIONS)
    assert revoked.value.status_code == 410


def test_multiple_pending_invites_are_independent(database):
    token_a, _ = auth.create_provider_invite()
    token_b, _ = auth.create_provider_invite()

    auth.accept_provider_invite(token_a, "dr-a", "long-enough-pass", QUESTIONS)

    # Consuming one leaves the other live.
    assert len(auth.load_invites()) == 1
    auth.accept_provider_invite(token_b, "dr-b", "long-enough-pass", QUESTIONS)
    assert {p["username"] for p in auth.load_providers()} == {"dr-a", "dr-b"}
