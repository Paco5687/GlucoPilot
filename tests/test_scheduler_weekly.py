"""Weekly Patterns/Insights regeneration: due-ness, cursor, and failure."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from server import db, scheduler
from server.config import OWNER_EMAIL
from server.migrations import run_migrations


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "data" / "app.sqlite3"
    path.parent.mkdir()
    run_migrations(path)
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


@pytest.fixture
def only_weekly(monkeypatch):
    """Silence every other scheduler job so _tick exercises just the weekly ones."""
    last = {key: float("inf") for key in scheduler._last_run}
    last["patterns"] = 0.0
    last["insights"] = 0.0
    monkeypatch.setattr(scheduler, "_last_run", last)
    calls = []

    def job(name, fail=False):
        async def run():
            calls.append(name)
            if fail:
                raise RuntimeError("model unavailable")
            return {"ok": True}
        return run

    def install(fail=()):
        monkeypatch.setattr(scheduler, "WEEKLY_ANALYSES", (
            ("patterns", "patterns_last_run", job("patterns", "patterns" in fail)),
            ("insights", "insights_last_run", job("insights", "insights" in fail)),
        ))
    return calls, install


def _stamp(cursor, days_ago):
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.set_config_value(cursor, when.isoformat(timespec="seconds").replace("+00:00", "Z"))


def _glucose():
    db.create_entity("GlucoseReading", {"timestamp": "2026-09-27T12:00:00.000Z", "value": 110, "owner_email": OWNER_EMAIL})


def test_first_run_bootstraps_and_stamps_the_cursor(database, only_weekly):
    calls, install = only_weekly
    install()
    _glucose()
    asyncio.run(scheduler._tick())
    assert calls == ["patterns", "insights"]
    assert db.config_value("patterns_last_run") and db.config_value("insights_last_run")
    assert not scheduler._weekly_due("patterns_last_run")


def test_runs_only_when_a_week_has_passed(database, only_weekly):
    calls, install = only_weekly
    install()
    _glucose()
    _stamp("patterns_last_run", 3)
    _stamp("insights_last_run", 8)
    asyncio.run(scheduler._tick())
    assert calls == ["insights"]


def test_nothing_runs_without_glucose_data(database, only_weekly):
    calls, install = only_weekly
    install()
    asyncio.run(scheduler._tick())
    assert calls == []


def test_a_failure_leaves_the_cursor_unset_and_is_throttled(database, only_weekly):
    calls, install = only_weekly
    install(fail=("patterns",))
    _glucose()
    asyncio.run(scheduler._tick())
    assert calls == ["patterns", "insights"]
    assert not db.config_value("patterns_last_run")  # retried next hour, not next week
    assert db.config_value("insights_last_run")
    asyncio.run(scheduler._tick())  # immediately again: the hourly throttle holds
    assert calls == ["patterns", "insights"]


def test_manual_refresh_restarts_the_week(database):
    scheduler.mark_weekly_run("patterns_last_run")
    assert not scheduler._weekly_due("patterns_last_run")


def test_health_summary_keeps_its_behaviour(database):
    assert scheduler._health_summary_due()
    _stamp("health_summary_last_run", 2)
    assert not scheduler._health_summary_due()
    _stamp("health_summary_last_run", 7.1)
    assert scheduler._health_summary_due()
