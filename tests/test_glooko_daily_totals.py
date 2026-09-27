"""Glooko daily insulin totals — the only complete insulin figure available.

On an Omnipod 5 the v2 event streams carry the programmed schedule and manual
boluses only, missing over half the day. These totals come from the v3 graph
API and are what makes a real TDD possible, so the mapping is pinned here.
"""

from __future__ import annotations

import pytest

from server import glooko
from server.typed_treatments import parse_pump_daily_total

pytestmark = pytest.mark.risk_critical


def test_note_format_round_trips_through_the_reconciler_parser():
    mapped = glooko._map_daily_total({"date": "2026-08-03", "total": 36.1, "basal": 35.6, "bolus": 0.5})

    assert mapped["event_type"] == "Daily Total"
    assert mapped["type"] == "insulin"
    assert mapped["source"] == "glooko"
    # The reconciler reads the day label off the ISO prefix, so it must equal
    # the local date rather than drifting to a neighbouring day.
    assert mapped["timestamp"][:10] == "2026-08-03"

    parsed = parse_pump_daily_total(mapped["notes"])
    assert parsed["completeness"] == "complete"
    assert parsed["total_units"] == 36.1
    assert parsed["bolus_units"] == 0.5


def test_components_are_made_to_sum_exactly():
    """Glooko rounds each field independently; the note must still reconcile.

    Left alone, basal+bolus can miss the total by 0.1 U and trip the
    reconciler's 0.05 component check — which silently drops the day.
    """
    # Glooko's own numbers here sum to 36.7, not the stated 36.6.
    mapped = glooko._map_daily_total({"date": "2026-08-01", "total": 36.6, "basal": 36.2, "bolus": 0.5})
    parsed = parse_pump_daily_total(mapped["notes"])

    assert parsed["completeness"] == "complete"
    assert parsed["total_units"] == 36.6
    assert parsed["bolus_units"] == 0.5
    # basal is derived from the authoritative total, so the components agree.
    assert parsed["basal_units"] == 36.1
    assert abs(parsed["basal_units"] + parsed["bolus_units"] - parsed["total_units"]) <= 0.05


def test_total_without_a_bolus_component_still_maps():
    mapped = glooko._map_daily_total({"date": "2026-08-04", "total": 35.7, "basal": None, "bolus": None})
    parsed = parse_pump_daily_total(mapped["notes"])

    assert parsed["total_units"] == 35.7
    assert parsed["completeness"] == "partial"


def test_missing_total_maps_to_nothing():
    assert glooko._map_daily_total({"date": "2026-08-04", "total": None, "basal": 1, "bolus": 2}) is None


def test_ns_id_is_stable_per_day_so_resync_does_not_duplicate():
    row = {"date": "2026-08-05", "total": 42.3, "basal": 41.8, "bolus": 0.6}
    assert glooko._map_daily_total(row)["ns_id"] == glooko._map_daily_total(row)["ns_id"]
    assert glooko._map_daily_total(row)["ns_id"] == "glooko-dailytotal-2026-08-05"


def test_us_graph_host_is_region_prefixed(monkeypatch):
    # The v3 graph API answers on the region-prefixed host the web app uses.
    monkeypatch.setattr(glooko, "_region", lambda: "us")
    assert glooko._graph_base_url() == "https://us.api.glooko.com"
    monkeypatch.setattr(glooko, "_region", lambda: "eu")
    assert glooko._graph_base_url() == "https://eu.api.glooko.com"


def test_pod_activation_becomes_a_site_change(monkeypatch):
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(glooko, "_pump_tz", lambda: ZoneInfo("America/New_York"))
    mapped = glooko._map_pump_event({
        "type": "pod_activating", "pumpTimestamp": "2026-08-08T14:40:33.000Z",
        "guid": "abc-123",
    })
    assert mapped["event_type"] == "Site Change"
    assert mapped["ns_id"] == "glooko-abc-123"
    # The pump's 14:40 is Eastern wall-clock (EDT) -> 18:40 UTC.
    assert mapped["timestamp"].startswith("2026-08-08T18:40:33")
    assert mapped["pump_clock"] == "local"


def test_only_the_activation_event_maps_from_a_pod_swap():
    # One physical swap emits five events; four of them must map to nothing.
    for kind in ("pod_deactivated", "reservoir_change", "prime_cannula", "prime_tubing"):
        assert glooko._map_pump_event({"type": kind, "pumpTimestamp": "2026-08-08T14:40:00.000Z"}) is None
    sensor = glooko._map_pump_event({"type": "cgm_sensor_change", "pumpTimestamp": "2026-08-08T15:00:00.000Z"})
    assert sensor["event_type"] == "Sensor Start"


def _pump_local(dt):
    """Format an instant the way the pump reports it: local digits, fake Z."""
    from zoneinfo import ZoneInfo
    return dt.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_settled_mode_period_maps_with_minutes(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(glooko, "_pump_tz", lambda: ZoneInfo("America/New_York"))
    start = datetime.now(timezone.utc) - timedelta(hours=14)
    end = start + timedelta(seconds=39001)
    mapped = glooko._map_mode({
        "type": "manual",
        "pumpTimestamp": _pump_local(start),
        "endTimestamp": _pump_local(end),
        "duration": 39001,
        "guid": "mode-guid-1",
    })
    assert mapped["type"] == "pump_mode"
    assert mapped["mode"] == "manual"
    # Seconds in, minutes stored — consistent with every other Treatment duration.
    assert round(mapped["duration"]) == 650
    assert mapped["ns_id"] == "glooko-mode-guid-1"


def test_open_or_fresh_mode_periods_are_not_stored(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(glooko, "_pump_tz", lambda: ZoneInfo("America/New_York"))
    now = datetime.now(timezone.utc)
    # No end: still open. Fresh end: could still be extended by the next sync.
    assert glooko._map_mode({"type": "automatic",
        "pumpTimestamp": _pump_local(now - timedelta(hours=3)),
        "duration": 10800}) is None
    assert glooko._map_mode({"type": "automatic",
        "pumpTimestamp": _pump_local(now - timedelta(hours=2)),
        "endTimestamp": _pump_local(now - timedelta(minutes=30)),
        "duration": 5400}) is None


class TestDailyTotalUpsert:
    """Glooko settles a day's total days late; a stored value must follow it."""

    @pytest.fixture
    def database(self, tmp_path, monkeypatch):
        from server import db
        from server.migrations import run_migrations

        path = tmp_path / "data" / "app.sqlite3"
        path.parent.mkdir()
        run_migrations(path)
        monkeypatch.setattr(db, "DB_PATH", path)
        return path

    def _total(self, date: str, total: float, basal: float) -> dict:
        return glooko._map_daily_total({"date": date, "total": total, "basal": basal, "bolus": round(total - basal, 2)})

    def test_changed_daily_total_is_updated_in_place(self, database):
        from server import db
        from server.config import OWNER_EMAIL

        first = self._total("2026-09-18", 5.5, 5.5)  # the lagging first upload
        assert glooko._persist_treatments([first]) == (1, 0, 0)

        settled = self._total("2026-09-18", 42.5, 42.5)
        assert glooko._persist_treatments([settled]) == (0, 0, 1)

        rows = [t for t in db.query_entities("Treatment", {"owner_email": OWNER_EMAIL}, "timestamp", 10)
                if t.get("event_type") == "Daily Total"]
        assert len(rows) == 1  # updated, not duplicated
        assert rows[0]["notes"] == settled["notes"]
        assert rows[0]["ns_id"] == "glooko-dailytotal-2026-09-18"

    def test_unchanged_daily_total_is_skipped_not_rewritten(self, database):
        same = self._total("2026-09-18", 42.5, 42.5)
        assert glooko._persist_treatments([same]) == (1, 0, 0)
        assert glooko._persist_treatments([same]) == (0, 1, 0)

    def test_other_duplicates_still_skip(self, database):
        bolus = {
            "type": "insulin", "event_type": "Bolus", "timestamp": "2026-09-18T12:00:00.000Z",
            "amount": 2.0, "notes": "first", "source": "glooko", "ns_id": "glooko-bolus-1",
            "owner_email": __import__("server.config", fromlist=["OWNER_EMAIL"]).OWNER_EMAIL,
        }
        assert glooko._persist_treatments([bolus]) == (1, 0, 0)
        changed = {**bolus, "notes": "second"}
        # Non-total treatments keep the original never-update contract.
        assert glooko._persist_treatments([changed]) == (0, 1, 0)



class TestPumpClock:
    """Glooko labels the pump's local wall clock as UTC. Read it as local."""

    @pytest.fixture(autouse=True)
    def eastern(self, monkeypatch):
        from zoneinfo import ZoneInfo
        monkeypatch.setattr(glooko, "_pump_tz", lambda: ZoneInfo("America/New_York"))

    def test_summer_and_winter_offsets_follow_dst(self):
        summer = glooko._parse_pump_ts("2026-09-25T10:03:19.000Z")
        winter = glooko._parse_pump_ts("2026-01-15T10:03:19.000Z")
        assert summer.isoformat() == "2026-09-25T14:03:19+00:00"   # EDT, +4
        assert winter.isoformat() == "2026-01-15T15:03:19+00:00"   # EST, +5

    def test_bolus_maps_to_true_utc(self):
        rows = glooko._map_bolus({"pumpTimestamp": "2026-09-25T10:03:19.000Z", "insulinDelivered": 0.25, "guid": "b1"})
        assert rows and rows[0]["timestamp"] == "2026-09-25T14:03:19.000Z"

    def test_sync_timestamps_are_still_real_utc(self):
        assert glooko._parse_ts("2026-09-26T13:42:11.540Z").isoformat() == "2026-09-26T13:42:11.540000+00:00"

    def test_stored_rows_are_repaired_once_and_daily_totals_left_alone(self, tmp_path, monkeypatch):
        from server import db
        from server.migrations import run_migrations
        from server.config import OWNER_EMAIL

        path = tmp_path / "data" / "app.sqlite3"
        path.parent.mkdir()
        run_migrations(path)
        monkeypatch.setattr(db, "DB_PATH", path)
        db.create_entity("Treatment", {"type": "insulin", "event_type": "manual", "timestamp": "2026-09-25T10:03:19.000Z",
                                                "amount": 0.25, "source": "glooko", "ns_id": "glooko-b1", "owner_email": OWNER_EMAIL})
        total = db.create_entity("Treatment", {"type": "insulin", "event_type": "Daily Total", "timestamp": "2026-09-25T12:00:00.000Z",
                                               "notes": "Total: 38U", "source": "glooko", "ns_id": "glooko-dailytotal-2026-09-25", "owner_email": OWNER_EMAIL})
        fresh = db.create_entity("Treatment", {"type": "pump_mode", "event_type": "Pump Mode", "timestamp": "2026-09-25T14:00:00.000Z",
                                               "mode": "manual", "duration": 60.0, "source": "glooko", "ns_id": "glooko-m1",
                                               "pump_clock": "local", "owner_email": OWNER_EMAIL})
        assert glooko.fix_stored_pump_clock() == {"shifted": 1, "skipped": 2}
        rows = {r["ns_id"]: r for r in db.query_entities("Treatment", {"owner_email": OWNER_EMAIL}, "timestamp", 10)}
        assert rows["glooko-b1"]["timestamp"] == "2026-09-25T14:03:19.000Z" and rows["glooko-b1"]["pump_clock"] == "local"
        assert rows["glooko-dailytotal-2026-09-25"]["timestamp"] == total["timestamp"]
        assert rows["glooko-m1"]["timestamp"] == fresh["timestamp"]
        # Idempotent: a second pass shifts nothing.
        assert glooko.fix_stored_pump_clock() == {"shifted": 0, "skipped": 3}
        # And the guard runs it only once per database.
        assert glooko.ensure_pump_clock_fixed() == {"shifted": 0, "skipped": 3}
        assert glooko.ensure_pump_clock_fixed() is None
