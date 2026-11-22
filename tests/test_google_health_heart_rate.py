"""Google Health intraday heart rate is stored once per minute."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from server import db, google_health
from server.config import OWNER_EMAIL
from server.migrations import run_migrations
from server.repositories import get_repositories

# Mid-minute on purpose: a 30-minute poll's cutoff then lands at 12:10:30.
NOW = datetime(2026, 11, 16, 12, 40, 30, tzinfo=timezone.utc)


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


@pytest.fixture
def heart_rate_api(tmp_path, monkeypatch):
    """Serve `state["samples"]` newest-first in pages of 1000, like Google."""
    database = tmp_path / "data" / "app.sqlite3"
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(google_health, "datetime", _FixedDatetime)
    monkeypatch.setattr(google_health, "_get_connection", lambda: {"connected": True})

    async def token(_client):
        return "token"

    monkeypatch.setattr(google_health, "_access_token", token)
    state = {"samples": []}

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("pageToken") or 0)
        page = state["samples"][start : start + 1000]
        body = {
            "dataPoints": [
                {
                    "heartRate": {
                        "sampleTime": {"physicalTime": at.isoformat().replace("+00:00", "Z")},
                        "beatsPerMinute": str(bpm),
                    }
                }
                for at, bpm in page
            ]
        }
        if start + 1000 < len(state["samples"]):
            body["nextPageToken"] = str(start + 1000)
        return httpx.Response(200, json=body)

    real_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(google_health.httpx, "AsyncClient", client)
    return state


def _samples(minutes: int, every_seconds: int) -> list[tuple[datetime, int]]:
    """Newest-first samples from just before NOW back `minutes` minutes."""
    count = minutes * 60 // every_seconds
    return [(NOW - timedelta(seconds=10 + i * every_seconds), 60 + i % 40) for i in range(count)]


def _stored_minutes() -> list[str]:
    rows = get_repositories().fitbit_heart_rate.query({"owner_email": OWNER_EMAIL}, "-timestamp")
    return [row["timestamp"].replace(".000Z", "Z") for row in rows]


def test_repeat_poll_does_not_resave_the_cutoff_minute(heart_rate_api):
    heart_rate_api["samples"] = _samples(minutes=60, every_seconds=10)

    first = asyncio.run(google_health._sync_heart_rate(30))
    second = asyncio.run(google_health._sync_heart_rate(30))

    stored = _stored_minutes()
    assert "2026-11-16T12:10:00Z" in stored  # the cutoff's own minute
    assert first["created"] == len(stored) == len(set(stored))
    assert second["created"] == 0


def test_backfill_skips_every_stored_minute_past_5000(heart_rate_api):
    heart_rate_api["samples"] = _samples(minutes=5200, every_seconds=60)
    minutes = 5200

    first = asyncio.run(google_health._sync_heart_rate(minutes))
    second = asyncio.run(google_health._sync_heart_rate(minutes))

    stored = _stored_minutes()
    assert first["created"] == len(stored) == len(set(stored)) > 5000
    assert second["created"] == 0
