"""Sign-in audit log: who signed in, when, and from where.

Successful and failed logins, logouts, provider invite acceptances and
provider password resets land in the ``login_events`` table (migration 24),
so the owner can see which providers use their access and how often.
Recording is best-effort: a failed write is logged and never blocks a login.
"""

from __future__ import annotations

import ipaddress
import logging
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from fastapi import Request

from . import db
from .config import env

log = logging.getLogger("glucopilot.login_log")

RETENTION_DAYS = 365
USER_AGENT_MAX = 300
RECENT_LIMIT = 50

# Cloudflare's edge ranges (https://www.cloudflare.com/ips), the production
# path being Cloudflare -> Traefik -> app. Keep in sync with Traefik's
# forwardedHeaders.trustedIPs. TRUSTED_PROXY_CIDRS (comma-separated) replaces
# this list for other deployments; private and loopback addresses (container
# networks, the LAN) are always treated as proxies.
CLOUDFLARE_NETWORKS = (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
    "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
)


@lru_cache(maxsize=1)
def _proxy_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    configured = env("TRUSTED_PROXY_CIDRS", "")
    cidrs = [c.strip() for c in configured.split(",") if c.strip()] if configured else CLOUDFLARE_NETWORKS
    networks = []
    for cidr in cidrs:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            log.warning("ignoring invalid TRUSTED_PROXY_CIDRS entry %r", cidr)
    return tuple(networks)


def _is_proxy(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if address.is_private or address.is_loopback or address.is_link_local:
        return True
    return any(address in network for network in _proxy_networks())


def client_ip(request: Request) -> str | None:
    """The visitor's address: the rightmost hop that is not a known proxy.

    Walking X-Forwarded-For from the right, rather than reading its first
    entry or CF-Connecting-IP, means a client cannot plant an address: every
    hop it could forge sits to the left of the first untrusted one.
    """
    chain = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",")]
    if request.client and request.client.host:
        chain.append(request.client.host)
    addresses = []
    for entry in chain:
        try:
            addresses.append(ipaddress.ip_address(entry))
        except ValueError:
            continue
    for address in reversed(addresses):
        if not _is_proxy(address):
            return str(address)
    # Every hop is a proxy or LAN address (e.g. a sign-in from home).
    return str(addresses[0]) if addresses else None


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def record(
    event: str,
    request: Request | None,
    *,
    role: str | None = None,
    username: str | None = None,
) -> None:
    """Append one sign-in event and prune rows past the retention window."""
    try:
        now = datetime.now(timezone.utc)
        user_agent = (request.headers.get("user-agent") or "")[:USER_AGENT_MAX] if request else ""
        with db.connect() as connection:
            connection.execute(
                """
                INSERT INTO login_events (occurred_at, event, role, username, client_ip, user_agent)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    _iso(now),
                    event,
                    role,
                    username or None,
                    client_ip(request) if request else None,
                    user_agent or None,
                ),
            )
            connection.execute(
                "DELETE FROM login_events WHERE occurred_at < ?",
                (_iso(now - timedelta(days=RETENTION_DAYS)),),
            )
    except Exception:
        log.warning("could not record %s sign-in event", event, exc_info=True)


def recording_since() -> str | None:
    """When this database started recording sign-ins (migration 24's apply time)."""
    with db.connect() as connection:
        row = connection.execute(
            "SELECT applied_at FROM schema_migrations WHERE name = 'login_events'"
        ).fetchone()
    return row["applied_at"] if row else None


def provider_activity(usernames: list[str], now: datetime | None = None) -> list[dict]:
    """Per provider: last successful sign-in and how many in 30 days / overall."""
    now = now or datetime.now(timezone.utc)
    since_30d = _iso(now - timedelta(days=30))
    activity = []
    with db.connect() as connection:
        for username in usernames:
            row = connection.execute(
                """
                SELECT MAX(occurred_at) AS last_login,
                       COUNT(*) AS logins_total,
                       SUM(occurred_at >= ?) AS logins_30d
                FROM login_events
                WHERE role = 'provider' AND username = ? AND event = 'login'
                """,
                (since_30d, username),
            ).fetchone()
            activity.append({
                "username": username,
                "last_login": row["last_login"],
                "logins_30d": int(row["logins_30d"] or 0),
                "logins_total": int(row["logins_total"] or 0),
            })
    return activity


def recent_events(limit: int = RECENT_LIMIT) -> list[dict]:
    with db.connect() as connection:
        rows = connection.execute(
            """
            SELECT occurred_at, event, role, username, client_ip, user_agent
            FROM login_events ORDER BY occurred_at DESC, id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]
