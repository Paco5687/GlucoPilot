"""Regression coverage for P4 activity/position interval analytics."""

import asyncio
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from server import companion_evidence, db, google_health
from server.backup import create_verified_backup
from server.activity_position import (
    ALGORITHM_VERSION,
    CorrectionBody,
    IntervalBody,
    SqliteActivityPositionRepository,
    build_analysis,
    resolve_intervals,
)
from server.config import OWNER_EMAIL
from server.evidence_bundle import (
    EvidenceBundleQuery,
    EvidenceDomain,
    build_bundle,
    clear_bundle_cache,
)
from server.migrations import run_migrations


pytestmark = pytest.mark.risk_critical
BASE = datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)


def _interval(index: int, **values):
    start = BASE + timedelta(days=index)
    return {
        "id": f"interval-{index}",
        "start_time": start.isoformat().replace("+00:00", "Z"),
        "end_time": (start + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "activity": "resting",
        "position": "sitting",
        "origin_kind": "manual",
        "origin_label": "Synthetic test",
        "correction_of_id": None,
        "created_at": start.isoformat().replace("+00:00", "Z"),
        "confidence": {
            "confidence_score": None,
            "confidence_label": "not_assessed",
        },
        **values,
    }


def _glucose(index: int):
    start = BASE + timedelta(days=index)
    return [
        {
            "id": f"g-{index}-start",
            "timestamp": start.isoformat().replace("+00:00", "Z"),
            "value": 100 + index,
            "source": "synthetic-cgm",
        },
        {
            "id": f"g-{index}-end",
            "timestamp": (start + timedelta(hours=1)).isoformat().replace(
                "+00:00", "Z"
            ),
            "value": 110 + index,
            "source": "synthetic-cgm",
        },
    ]


def test_manual_interval_overrides_inference_without_deleting_it():
    wearable = _interval(
        0,
        id="wearable",
        activity="walking",
        position="unknown",
        origin_kind="wearable",
        origin_label="Synthetic wearable",
        confidence={"confidence_score": 0.55, "confidence_label": "low"},
    )
    manual = _interval(
        0,
        id="manual",
        position="standing",
        correction_of_id="wearable",
        created_at=(BASE + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
    )

    resolved = resolve_intervals([wearable, manual])

    assert [item["id"] for item in resolved] == ["wearable", "manual"]
    assert resolved[0]["effective"] is False
    assert resolved[0]["overridden_by"] == ["manual"]
    assert resolved[1]["effective"] is True
    assert resolved[1]["precedence"] == "manual_over_wearable"


def test_partial_manual_overlap_keeps_nonoverlapping_inferred_time():
    wearable = _interval(
        0,
        id="wearable",
        activity="walking",
        position="unknown",
        origin_kind="wearable",
        origin_label="Synthetic wearable",
        confidence={"confidence_score": 0.55, "confidence_label": "low"},
    )
    manual = _interval(
        0,
        id="manual",
        start_time=(BASE + timedelta(minutes=20)).isoformat().replace(
            "+00:00", "Z"
        ),
        end_time=(BASE + timedelta(minutes=40)).isoformat().replace(
            "+00:00", "Z"
        ),
        position="standing",
    )

    inferred = resolve_intervals([wearable, manual])[0]

    assert inferred["effective"] is True
    assert inferred["coverage_status"] == "partially_overridden"
    assert inferred["effective_segments"] == [
        {
            "start_time": BASE.isoformat().replace("+00:00", "Z"),
            "end_time": (BASE + timedelta(minutes=20)).isoformat().replace(
                "+00:00", "Z"
            ),
        },
        {
            "start_time": (BASE + timedelta(minutes=40)).isoformat().replace(
                "+00:00", "Z"
            ),
            "end_time": (BASE + timedelta(hours=1)).isoformat().replace(
                "+00:00", "Z"
            ),
        },
    ]


def test_effects_include_sample_interval_missingness_and_replication_metadata():
    intervals = [_interval(index) for index in range(14)]
    glucose = [row for index in range(14) for row in _glucose(index)]

    result = build_analysis(
        intervals,
        glucose,
        [],
        [],
        start=BASE,
        end=BASE + timedelta(days=13, hours=1),
        timezone_name="UTC",
    )

    effect = next(
        item
        for item in result["effects"]
        if item["dimension"] == "position"
        and item["state"] == "sitting"
        and item["metric"] == "glucose_slope_mg_dl_per_hour"
    )
    assert effect["algorithm_version"] == ALGORITHM_VERSION
    assert effect["observed_mean"] == 10
    assert effect["sample_count"] == 14
    assert effect["interval_count"] == 14
    assert effect["interval_missingness"] == {
        "available_intervals": 14,
        "measured_intervals": 14,
        "missing_intervals": 0,
        "missing_rate": 0,
    }
    assert effect["analytics_confidence"]["confidence_interval"]["method"] == (
        "normal_mean"
    )
    assert effect["analytics_confidence"]["missingness"]["missing_days"] == 0
    assert effect["analytics_confidence"]["discovery_status"] == "emerging"
    assert effect["replication_status"] == "not-attempted"
    assert effect["qualifies_for_companion"] is True
    assert effect["language"]["causal_allowed"] is False
    assert "does not establish" in effect["language"]["statement"]


def test_all_required_response_families_join_to_event_time_state():
    response_event = {
        "id": "response-1",
        "classification": "clean",
        "observed": {
            "bolus": {
                "id": "bolus-1",
                "timestamp": BASE.isoformat().replace("+00:00", "Z"),
            },
            "start_glucose": _glucose(0)[0],
            "end_glucose": _glucose(0)[1],
            "nadir_glucose": _glucose(0)[1],
        },
        "calculations": {"nadir_drop_per_unit_mg_dl": 22},
    }
    fingerstick = {
        "id": "fingerstick-1",
        "timestamp": (BASE + timedelta(minutes=30)).isoformat().replace(
            "+00:00", "Z"
        ),
        "delta": 7,
    }

    result = build_analysis(
        [_interval(0)],
        _glucose(0),
        [fingerstick],
        [response_event],
        start=BASE,
        end=BASE + timedelta(hours=1),
        timezone_name="UTC",
    )

    metrics = {
        item["metric"]
        for item in result["effects"]
        if item["dimension"] == "position" and item["state"] == "sitting"
    }
    assert metrics == {
        "glucose_slope_mg_dl_per_hour",
        "morning_glucose_slope_mg_dl_per_hour",
        "bolus_response_mg_dl_per_unit",
        "cgm_minus_fingerstick_mg_dl",
    }


@pytest.fixture
def activity_database(tmp_path, monkeypatch):
    database = tmp_path / "data" / "app.sqlite3"
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setenv("RELATIONSHIP_READS_ENABLED", "false")
    monkeypatch.setenv("EVIDENCE_SET_READS_ENABLED", "false")
    clear_bundle_cache()
    yield database
    clear_bundle_cache()


def test_repository_appends_auditable_correction(activity_database, tmp_path):
    repository = SqliteActivityPositionRepository()
    original = repository.create_manual(
        IntervalBody(
            start_time=BASE,
            end_time=BASE + timedelta(hours=1),
            activity="resting",
            position="sitting",
            notes="synthetic",
        ),
        actor_label="Test owner",
    )
    correction = repository.create_manual(
        CorrectionBody(
            start_time=BASE,
            end_time=BASE + timedelta(hours=1),
            activity="resting",
            position="standing",
            notes="synthetic correction",
            reason="The original position was entered incorrectly",
        ),
        actor_label="Test owner",
        correction_of_id=original["id"],
        reason="The original position was entered incorrectly",
    )

    rows = resolve_intervals(
        repository.list(BASE - timedelta(minutes=1), BASE + timedelta(hours=2))
    )
    assert len(rows) == 2
    assert next(item for item in rows if item["id"] == original["id"])[
        "effective"
    ] is False
    assert correction["correction_of_id"] == original["id"]
    history = repository.history(original["id"])
    assert [event["action"] for event in history] == ["created", "corrected"]
    assert history[1]["before"]["id"] == original["id"]
    assert history[1]["after"]["id"] == correction["id"]
    with sqlite3.connect(activity_database) as connection:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="corrected by appending",
        ):
            connection.execute(
                "UPDATE activity_position_intervals SET notes='mutated' WHERE id=?",
                (original["id"],),
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="cannot be deleted",
        ):
            connection.execute(
                "DELETE FROM activity_position_intervals WHERE id=?",
                (original["id"],),
            )
    _backup, verification = create_verified_backup(
        activity_database.parent,
        tmp_path / "backups",
        reason="activity position regression",
    )
    assert verification["activity_position_interval_count"] == 2
    assert verification["manual_activity_position_interval_count"] == 2
    assert verification["wearable_activity_position_interval_count"] == 0
    assert verification["activity_position_correction_count"] == 1
    assert verification["activity_position_event_count"] == 2


def test_evidence_bundle_exposes_effects_and_companion_uses_only_qualifying(
    activity_database,
):
    repository = SqliteActivityPositionRepository()
    for index in range(14):
        start = BASE + timedelta(days=index)
        repository.create_manual(
            IntervalBody(
                start_time=start,
                end_time=start + timedelta(hours=1),
                activity="resting",
                position="sitting",
            ),
            actor_label="Synthetic owner",
        )
        for row in _glucose(index):
            db.create_entity(
                "GlucoseReading",
                {**row, "owner_email": OWNER_EMAIL},
            )

    query = EvidenceBundleQuery(
        start=BASE,
        end=BASE + timedelta(days=13, hours=2),
        domains=(EvidenceDomain.WEARABLES,),
        question_intent="glucose response while sitting",
        item_budget=50,
    )
    bundle = build_bundle(query)
    effects = [
        item
        for item in bundle["evidence"]["derived_metrics"]
        if item["entity_type"] == "ActivityPositionEffect"
    ]

    assert bundle["bundle_version"] == "2.3.0"
    assert effects
    assert all(item["data"]["language"]["causal_allowed"] is False for item in effects)
    assert all(item["source_links"] for item in effects)
    assert any(
        item["data"]["qualifies_for_companion"]
        and item["data"]["metric"] == "glucose_slope_mg_dl_per_hour"
        for item in effects
    )

    public, reasoning = companion_evidence.build_context(
        "What happens to my glucose while sitting?",
        as_of=date(2026, 7, 14),
        refresh=False,
    )
    companion_effects = [
        item
        for item in public["evidence_items"]
        if item["entity_type"] == "ActivityPositionEffect"
    ]
    assert companion_effects
    reasoning_by_id = {item["id"]: item for item in reasoning["items"]}
    assert all(
        reasoning_by_id[item["id"]]["data"]["sample_count"] >= 14
        for item in companion_effects
    )


def test_companion_omits_exploratory_activity_effects(activity_database):
    repository = SqliteActivityPositionRepository()
    for index in range(2):
        start = BASE + timedelta(days=index)
        repository.create_manual(
            IntervalBody(
                start_time=start,
                end_time=start + timedelta(hours=1),
                activity="walking",
                position="standing",
            ),
            actor_label="Synthetic owner",
        )
        for row in _glucose(index):
            db.create_entity(
                "GlucoseReading",
                {**row, "owner_email": OWNER_EMAIL},
            )

    public, _reasoning = companion_evidence.build_context(
        "What happens to my glucose while walking?",
        as_of=date(2026, 7, 2),
        refresh=False,
    )

    assert not [
        item
        for item in public["evidence_items"]
        if item["entity_type"] == "ActivityPositionEffect"
    ]


def test_google_health_only_infers_short_timestamped_step_intervals(
    monkeypatch,
):
    async def intervals(*_args, **_kwargs):
        return [
            (
                "2026-07-01",
                {
                    "count": 20,
                    "interval": {
                        "startTime": "2026-07-01T08:00:00Z",
                        "endTime": "2026-07-01T08:05:00Z",
                    },
                },
            ),
            (
                "2026-07-01",
                {
                    "count": 1_000,
                    "interval": {
                        "startTime": "2026-07-01T09:00:00Z",
                        "endTime": "2026-07-01T17:00:00Z",
                    },
                },
            ),
        ]

    monkeypatch.setattr(google_health, "_list_interval", intervals)
    by_day = {}

    def day(day_key):
        return by_day.setdefault(day_key, {})

    asyncio.run(
        google_health._fetch_steps(
            None,
            "token",
            by_day,
            day,
            "2026-07-01",
            "2026-07-02",
            timezone.utc,
        )
    )

    assert by_day["2026-07-01"]["steps"] == 1_020
    assert by_day["2026-07-01"]["_activity_intervals"] == [
        {
            "start_time": "2026-07-01T08:00:00Z",
            "end_time": "2026-07-01T08:05:00Z",
            "step_count": 20,
            "raw_interval": {
                "startTime": "2026-07-01T08:00:00Z",
                "endTime": "2026-07-01T08:05:00Z",
            },
        }
    ]

    bouts = google_health._coalesce_step_intervals(
        [
            {
                "start_time": "2026-07-01T08:00:00Z",
                "end_time": "2026-07-01T08:01:00Z",
                "step_count": 8,
            },
            {
                "start_time": "2026-07-01T08:01:00Z",
                "end_time": "2026-07-01T08:02:00Z",
                "step_count": 9,
            },
        ]
    )
    assert bouts == [
        {
            "start_time": "2026-07-01T08:00:00Z",
            "end_time": "2026-07-01T08:02:00Z",
            "step_count": 17,
            "component_intervals": 2,
        }
    ]
