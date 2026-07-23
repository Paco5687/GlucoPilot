"""Regression coverage for versioned observational insulin response events."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from server import db, insulin
from server.config import OWNER_EMAIL
from server.evidence_bundle import (
    EvidenceBundleQuery,
    EvidenceDomain,
    build_bundle,
    clear_bundle_cache,
)
from server.insulin_response import ALGORITHM_VERSION, build_response_events
from server.migrations import run_migrations


pytestmark = pytest.mark.risk_critical

BASE = datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)


def _timestamp(minutes: int) -> str:
    return (BASE + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _cgm(*, start: float = 180, drop_per_point: float = 2) -> list[dict]:
    return [
        {
            "id": f"g-{minutes}",
            "timestamp": _timestamp(minutes),
            "value": start - (minutes // 5) * drop_per_point,
            "source": "test-cgm",
        }
        for minutes in range(0, 121, 5)
    ]


def _bolus(*, minutes: int = 0, amount=2, entity_id: str = "bolus-1") -> dict:
    return {
        "id": entity_id,
        "timestamp": _timestamp(minutes),
        "type": "insulin",
        "event_type": "Correction Bolus",
        "amount": amount,
        "source": "test-pump",
    }


def test_clean_response_is_reproducible_and_keeps_semantic_layers_separate():
    glucose = _cgm()
    result = build_response_events(
        [_bolus()],
        glucose,
        period_logs=[
            {
                "id": "period-1",
                "date": "2026-01-15",
                "phase": "luteal",
                "source": "manual",
            }
        ],
        wearable_days=[
            {"id": "fitbit-1", "date": "2026-01-15", "steps": 4200, "source": "fitbit"},
            {"id": "oura-1", "date": "2026-01-15", "activity_steps": 4000, "source": "oura"},
        ],
        fingersticks=[
            {
                "id": "fingerstick-1",
                "timestamp": _timestamp(3),
                "activity": "light",
                "position": "seated",
            }
        ],
        timezone_name="America/New_York",
    )

    assert result["algorithm_version"] == ALGORITHM_VERSION
    assert result["counts"] == {
        "total": 1,
        "clean": 1,
        "confounded": 0,
        "excluded": 0,
        "included_by_default": 1,
    }
    event = result["events"][0]
    assert event["classification"] == "clean"
    assert event["exclusion_reasons"] == []
    assert event["observed"]["bolus"]["id"] == "bolus-1"
    assert event["calculations"] == {
        "response_window_end": _timestamp(120),
        "cgm_points_in_window": 25,
        "start_glucose_mg_dl": 180.0,
        "end_glucose_mg_dl": 132.0,
        "nadir_glucose_mg_dl": 132.0,
        "time_to_nadir_minutes": 120.0,
        "nadir_drop_mg_dl": 48.0,
        "end_drop_mg_dl": 48.0,
        "nadir_drop_per_unit_mg_dl": 24.0,
        "estimated_iob_units": 0.0,
        "iob_contributors": [],
        "carbohydrates_g": 0,
    }
    assert event["context"]["time_of_day"] == "morning"
    assert event["context"]["cycle"]["phase"] == "luteal"
    assert event["context"]["cycle"]["provenance"] == "recorded_or_imported"
    assert event["context"]["activity"] == "light"
    assert event["context"]["position"] == "seated"
    assert event["context"]["daily_activity"]["semantic_class"] == (
        "daily_context_not_event_time_activity"
    )
    assert result["analysis"]["sample_count"] == 1
    assert result["analysis"]["default_filter"] == "classification=clean"

    reordered = build_response_events(
        list(reversed([_bolus()])),
        list(reversed(glucose)),
        timezone_name="America/New_York",
    )
    plain = build_response_events(
        [_bolus()],
        glucose,
        timezone_name="America/New_York",
    )
    assert reordered["input_data_version"] == plain["input_data_version"]
    assert reordered["events"] == plain["events"]


def test_confounders_are_retained_but_excluded_from_default_analysis():
    treatments = [
        _bolus(minutes=-60, amount=1, entity_id="prior"),
        _bolus(),
        {
            "id": "carb-1",
            "timestamp": _timestamp(10),
            "type": "carb",
            "amount": 12,
            "source": "test-pump",
        },
        _bolus(minutes=90, amount=0.5, entity_id="later"),
    ]
    result = build_response_events(treatments, _cgm())
    event = next(item for item in result["events"] if item["observed"]["bolus"]["id"] == "bolus-1")

    assert event["classification"] == "confounded"
    assert event["hard_exclusion_reasons"] == []
    assert set(event["confounder_reasons"]) == {
        "carbohydrate_in_response_window",
        "prior_estimated_iob",
        "subsequent_bolus_in_response_window",
    }
    assert event["included_by_default"] is False
    assert event["calculations"]["estimated_iob_units"] == 0.75
    assert event["calculations"]["carbohydrates_g"] == 12
    assert event["observed"]["carbohydrates"][0]["id"] == "carb-1"
    assert result["analysis"]["sample_count"] == 0


def test_invalid_and_undercovered_boluses_are_explicit_excluded_events():
    result = build_response_events(
        [_bolus(amount="not-a-number")],
        _cgm()[:4],
    )

    assert result["counts"]["excluded"] == 1
    event = result["events"][0]
    assert event["classification"] == "excluded"
    assert set(event["hard_exclusion_reasons"]) >= {
        "invalid_bolus_amount",
        "missing_end_glucose",
        "insufficient_cgm_coverage",
    }
    assert event["calculations"]["nadir_drop_per_unit_mg_dl"] is None


def test_baseline_before_bolus_never_produces_negative_time_to_response():
    glucose = [
        {
            "id": "g-before",
            "timestamp": _timestamp(-5),
            "value": 100,
            "source": "test-cgm",
        },
        *_cgm(start=180, drop_per_point=1),
    ]
    result = build_response_events([_bolus()], glucose)

    event = result["events"][0]
    assert event["calculations"]["time_to_nadir_minutes"] >= 0


def test_evidence_bundle_exposes_response_event_and_normalized_sources(
    tmp_path, monkeypatch
):
    database = tmp_path / "response-evidence.sqlite3"
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    clear_bundle_cache()
    bolus = db.create_entity(
        "Treatment", {**_bolus(), "owner_email": OWNER_EMAIL}
    )
    glucose_ids = []
    for row in _cgm():
        glucose_ids.append(
            db.create_entity(
                "GlucoseReading", {**row, "owner_email": OWNER_EMAIL}
            )["id"]
        )

    bundle = build_bundle(
        EvidenceBundleQuery(
            start=BASE,
            end=BASE + timedelta(minutes=1),
            domains=(EvidenceDomain.INSULIN,),
            question_intent="insulin response after correction bolus",
            item_budget=20,
        )
    )

    items = bundle["evidence"]["derived_metrics"]
    response = next(
        item for item in items if item["entity_type"] == "InsulinResponseEvent"
    )
    assert response["data"]["algorithm_version"] == ALGORITHM_VERSION
    assert response["data"]["classification"] == "clean"
    linked = {
        (link["entity_type"], link["entity_id"])
        for link in response["source_links"]
    }
    assert ("Treatment", bolus["id"]) in linked
    assert ("GlucoseReading", glucose_ids[0]) in linked
    assert bundle["bundle_version"] == "2.4.0"


def test_absorption_api_preserves_events_when_quality_blocks_summary(monkeypatch):
    event_time = datetime.now(timezone.utc) - timedelta(days=1)

    def event_timestamp(minutes):
        return (event_time + timedelta(minutes=minutes)).isoformat().replace(
            "+00:00", "Z"
        )

    glucose = [
        {
            "id": f"current-g-{minutes}",
            "timestamp": event_timestamp(minutes),
            "value": 180 - minutes / 5,
            "source": "test-cgm",
        }
        for minutes in range(0, 121, 5)
    ]
    bolus = {
        **_bolus(),
        "id": "current-bolus",
        "timestamp": event_timestamp(0),
    }

    class Repository:
        def __init__(self, rows):
            self.rows = rows

        def query(self, *args, **kwargs):
            return self.rows

    entities = {
        "PeriodLog": [],
        "OuraDaily": [],
        "FitbitDaily": [],
        "FingerstickReading": [],
    }
    repositories = SimpleNamespace(
        glucose=Repository(glucose),
        treatments=Repository([bolus]),
        entity=lambda entity_type: Repository(entities[entity_type]),
    )
    monkeypatch.setattr(insulin, "get_repositories", lambda: repositories)
    monkeypatch.setattr(insulin, "_app_timezone", lambda: ZoneInfo("UTC"))

    result = insulin.absorption()

    assert result["available"] is False
    assert result["counts"]["total"] == 1
    assert result["counts"]["clean"] == 1
    assert result["events"][0]["classification"] == "clean"
    assert result["quality"]["ai_eligible"] is False
    assert result["confidence"]["language"]["causal_allowed"] is False
    summary = asyncio.run(
        insulin.handle({"action": "absorption", "include_events": False})
    )
    assert "events" not in summary
    assert summary["counts"] == result["counts"]
