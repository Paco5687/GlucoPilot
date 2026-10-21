"""Programmed pump settings from Glooko's devices_and_settings snapshots.

Glooko records a snapshot only occasionally, so the newest can be months old.
These tests pin that every snapshot keeps its own timestamp, history is kept
and deduped, only the latest snapshot is current evidence, and a settings
outage never breaks the treatment sync. All values are synthetic.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from server import glooko, pump_settings

pytestmark = pytest.mark.risk_critical

ACTIVE = "pump-guid-active"
OLD_PUMP = "pump-guid-retired"


def _snapshot(*, carb_ratio_morning: float = 10, basal_night: float = 0.6) -> dict:
    return {
        "generalSettings": {"activeInsulinTime": 3.0},
        "basalSettings": {"maxBasalRate": 3.0, "activeBasalProgram": "Weekday"},
        "bolusSettings": {"maxBolus": 10.0, "bolusCalculatorMinBg": 70},
        "profilesBolus": [{
            "insulinToCarbRatioSegments": {"data": [
                {"segmentStart": 0, "duration": 6, "value": 12},
                {"segmentStart": 6, "duration": 5.5, "value": carb_ratio_morning},
                {"segmentStart": 11.5, "duration": 12.5, "value": 11},
            ]},
            "isfSegments": {"data": [{"segmentStart": 0, "duration": 24, "value": 45}]},
            "targetBgSegments": {"data": [
                {"segmentStart": 0, "duration": 7, "value": 120},
                {"segmentStart": 7, "duration": 17, "value": 110},
            ]},
            "bgCorrectionThresholdSegments": {"data": [{"segmentStart": 0, "duration": 24, "value": 130}]},
        }],
        "pumpProfilesBasal": [
            {"name": "Weekday", "data": [
                {"segmentStart": 0, "duration": 6, "value": basal_night},
                {"segmentStart": 6, "duration": 18, "value": 0.8},
            ]},
            {"name": "Sick day", "data": [{"segmentStart": 0, "duration": 24, "value": 1.0}]},
        ],
        "graphData": {"ignored": True},
        "additionalGraphData": [1, 2, 3],
    }


def _payload(snapshots: dict | None = None) -> dict:
    return {
        "activePumpGuid": ACTIVE,
        "deviceSettings": {"pumps": {
            ACTIVE: snapshots if snapshots is not None else {
                "2026-03-02T15:00:00.000Z": _snapshot(carb_ratio_morning=9),
                "2026-06-03T14:30:00.000Z": _snapshot(),
            },
            OLD_PUMP: {"2026-09-01T12:00:00.000Z": _snapshot(carb_ratio_morning=5)},
        }},
    }


@pytest.fixture
def database(tmp_path, monkeypatch):
    from server import db
    from server.migrations import run_migrations

    path = tmp_path / "data" / "app.sqlite3"
    path.parent.mkdir()
    run_migrations(path)
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


def _rows():
    from server import db
    from server.config import OWNER_EMAIL

    return db.query_entities("PumpSettings", {"owner_email": OWNER_EMAIL}, "snapshot_at", 100)


class TestMapping:
    def test_segments_scalars_and_basal_map_from_a_snapshot(self):
        row = pump_settings.map_snapshot(_snapshot(), device_guid=ACTIVE, snapshot_key="2026-06-03T14:30:00.000Z")

        assert row["snapshot_at"] == "2026-06-03T14:30:00Z"
        assert row["dedupe_key"] == f"{ACTIVE}:2026-06-03T14:30:00Z"
        assert [(s["start"], s["end"], s["value"]) for s in row["carb_ratio_segments"]] == [
            ("00:00", "06:00", 12), ("06:00", "11:30", 10), ("11:30", "24:00", 11),
        ]
        assert row["isf_segments"][0]["value"] == 45
        assert [s["value"] for s in row["target_segments"]] == [120, 110]
        assert row["correction_threshold_segments"][0]["value"] == 130
        assert row["active_insulin_time_hours"] == 3.0
        assert row["max_bolus_units"] == 10.0
        assert row["max_basal_rate_units_per_hour"] == 3.0
        assert row["bolus_calculator_min_bg"] == 70
        assert row["active_basal_program"] == "Weekday"
        weekday, sick = row["basal_programs"]
        assert weekday["active"] and not sick["active"]
        assert weekday["units_per_day"] == pytest.approx(0.6 * 6 + 0.8 * 18)

    def test_graph_data_is_not_kept(self):
        _guid, snapshots = glooko_payload_snapshots(_payload())
        assert all("graphData" not in s and "additionalGraphData" not in s for _key, s in snapshots)

    def test_only_the_active_pump_is_read(self):
        guid, snapshots = pump_settings.snapshots_from_payload(_payload())
        assert guid == ACTIVE
        assert sorted(key for key, _snapshot in snapshots) == [
            "2026-03-02T15:00:00.000Z", "2026-06-03T14:30:00.000Z",
        ]

    def test_missing_durations_run_to_the_next_segment(self):
        snapshot = _snapshot()
        snapshot["profilesBolus"][0]["isfSegments"] = {"data": [
            {"segmentStart": 0, "value": 50}, {"segmentStart": 8, "value": 40},
        ]}
        row = pump_settings.map_snapshot(snapshot, device_guid=ACTIVE, snapshot_key="2026-06-03T14:30:00Z")
        assert [(s["start"], s["end"]) for s in row["isf_segments"]] == [("00:00", "08:00"), ("08:00", "24:00")]

    def test_snapshot_without_settings_maps_to_nothing(self):
        assert pump_settings.map_snapshot({"generalSettings": {}}, device_guid=ACTIVE, snapshot_key="2026-06-03T14:30:00Z") is None
        assert pump_settings.map_snapshot(_snapshot(), device_guid=ACTIVE, snapshot_key="not-a-time") is None

    def test_companion_text_names_the_snapshot_date_and_values(self):
        row = pump_settings.map_snapshot(_snapshot(), device_guid=ACTIVE, snapshot_key="2026-06-03T14:30:00Z")
        assert "recorded 2026-06-03" in row["summary"]
        assert "not a live reading" in row["summary"]
        assert "Carb ratio g/U: 00:00-06:00 12; 06:00-11:30 10; 11:30-24:00 11." in row["summary"]
        assert "Correction factor (ISF) mg/dL/U: 00:00-24:00 45." in row["summary"]
        assert '"Weekday" (active)' in row["details"]
        assert len(row["summary"]) <= 700 and len(row["details"]) <= 700


def glooko_payload_snapshots(payload):
    return pump_settings.snapshots_from_payload(payload)


class TestPersistence:
    def test_history_is_kept_deduped_and_latest_marked(self, database):
        first = glooko._store_pump_settings(_payload())
        assert first == {"status": "ok", "snapshots": 2, "created": 2, "skipped": 0}
        again = glooko._store_pump_settings(_payload())
        assert again == {"status": "ok", "snapshots": 2, "created": 0, "skipped": 2}

        rows = _rows()
        assert len(rows) == 2
        assert [row["is_latest"] for row in rows] == [False, True]
        assert rows[1]["snapshot_at"] == "2026-06-03T14:30:00Z"

    def test_a_new_snapshot_moves_the_latest_mark_and_records_a_change(self, database):
        glooko._store_pump_settings(_payload())
        newer = _payload({
            "2026-03-02T15:00:00.000Z": _snapshot(carb_ratio_morning=9),
            "2026-06-03T14:30:00.000Z": _snapshot(),
            "2026-10-01T09:00:00.000Z": _snapshot(basal_night=0.7),
        })
        assert glooko._store_pump_settings(newer)["created"] == 1

        latest = [row for row in _rows() if row["is_latest"]]
        assert [row["snapshot_at"] for row in latest] == ["2026-10-01T09:00:00Z"]

        view = pump_settings.view(today=date(2026, 10, 20))
        assert view["snapshot_count"] == 3
        assert view["latest"]["as_of"] == "2026-10-01"
        assert view["latest"]["age_days"] == 19
        # Newest change first; each names what moved.
        assert [c["changed"] for c in view["changes"]] == [["Basal programs"], ["Carb ratio"]]

    def test_report_block_carries_as_of_and_age(self, database, monkeypatch):
        glooko._store_pump_settings(_payload())
        block = pump_settings.report_block()
        assert block["as_of"] == "2026-06-03"
        assert block["age_days"] >= 0
        assert [s["label"] for s in block["schedules"]] == [
            "Carb ratio", "Correction factor (ISF)", "Target glucose", "Correct above",
        ]
        assert {s["key"] for s in block["scalars"]} >= {"active_insulin_time_hours", "max_bolus_units"}

    def test_no_snapshots_means_no_report_block(self, database):
        assert glooko._store_pump_settings({"activePumpGuid": ACTIVE}) == {"status": "empty", "snapshots": 0}
        assert pump_settings.report_block() is None
        assert pump_settings.view()["available"] is False


class TestEvidence:
    def test_only_the_latest_snapshot_is_companion_evidence(self, database):
        from server.evidence_bundle import EvidenceBundleQuery, EvidenceDomain, build_bundle, clear_bundle_cache

        glooko._store_pump_settings(_payload())
        clear_bundle_cache()
        now = datetime(2026, 10, 20, tzinfo=timezone.utc)
        bundle = build_bundle(EvidenceBundleQuery(
            start=now - timedelta(days=30),
            end=now,
            domains=(EvidenceDomain.INSULIN,),
            question_intent="what is my carb ratio and isf",
            item_budget=10,
        ))
        items = [
            item
            for group in bundle["evidence"].values() if isinstance(group, list)
            for item in group
            if item.get("entity_type") == "PumpSettings"
        ]
        # Months older than the window, yet present — and only once.
        assert len(items) == 1
        assert items[0]["observed_at"] == "2026-06-03T14:30:00Z"
        assert "Carb ratio" in items[0]["data"]["summary"]

    def test_settings_questions_select_the_metabolic_scope(self):
        from server.companion_evidence import _selected_scopes

        assert [scope.name for scope in _selected_scopes("what's my ISF?")] == ["metabolic"]
        assert [scope.name for scope in _selected_scopes("what are my carb ratios")] == ["metabolic"]


class TestSyncIntegration:
    @pytest.fixture
    def glooko_api(self, monkeypatch):
        calls: list[str] = []
        state = {"settings_status": 200}

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/api/v2/users/sign_in":
                return httpx.Response(200, json={"userLogin": {"glookoCode": "code-1"}})
            if request.url.path == "/api/v3/devices_and_settings":
                if state["settings_status"] != 200:
                    return httpx.Response(state["settings_status"], json={"error": "nope"})
                return httpx.Response(200, json=_payload())
            if request.url.path == "/api/v3/graph/data":
                return httpx.Response(200, json={"series": {"dailyInsulinTotals": {}}})
            return httpx.Response(200, json={"lastPage": True})

        real_client = httpx.AsyncClient

        def client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(glooko.httpx, "AsyncClient", client)
        return calls, state

    def test_sync_fetches_settings_once_a_day(self, database, glooko_api):
        calls, _state = glooko_api
        first = asyncio.run(glooko._sync(2, False))
        assert first["ok"] and first["pump_settings"]["created"] == 2
        second = asyncio.run(glooko._sync(2, False))
        assert second["pump_settings"] == {"status": "not_due"}
        assert calls.count("/api/v3/devices_and_settings") == 1
        # A backfill forces a fresh fetch.
        forced = asyncio.run(glooko._sync(2, False, force_settings=True))
        assert forced["pump_settings"]["skipped"] == 2

    def test_settings_outage_does_not_fail_the_sync(self, database, glooko_api):
        from server import db

        _calls, state = glooko_api
        state["settings_status"] = 500
        result = asyncio.run(glooko._sync(2, False))
        assert result["ok"] is True
        assert result["pump_settings"] == {"status": "failed"}
        # Not marked fetched, so the next sync retries.
        assert db.get_setting(glooko.PUMP_SETTINGS_FETCHED_KEY) == ""
        assert _rows() == []


def test_settings_endpoint_requires_login(client):
    client.cookies.clear()
    assert client.get("/api/pump-settings").status_code == 401
