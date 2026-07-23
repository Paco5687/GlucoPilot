from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from server import db
from server.backup import create_verified_backup, verify_backup
from server.data_contracts import DEPLOYMENT_OWNER_ID
from server.migrations import run_migrations
from server.repositories import LegacyRepositoryCatalog, WearableRepository
from server.typed_wearables import (
    MAPPING_VERSION,
    WEARABLE_TYPES,
    SqliteTypedWearableRepository,
    WearableMappingError,
    backfill_typed_wearables,
    compare_wearable_stores,
    map_legacy_daily,
    map_legacy_sample,
)


pytestmark = pytest.mark.risk_critical
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "golden" / "typed_wearables.json"
ENVELOPE = {
    "created_date": "2026-03-02T13:00:00Z",
    "updated_date": "2026-03-02T13:01:00Z",
}


@pytest.fixture(scope="module")
def wearable_cases() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def wearable_database(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database = data_dir / "app.sqlite3"
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    return database


def _payload(row: dict) -> dict:
    return {key: value for key, value in row.items() if key not in {"id", "entity_type"}}


def _create_fixture_rows(cases: dict) -> dict[str, list[dict]]:
    created = {entity_type: [] for entity_type in WEARABLE_TYPES}
    for row in cases["daily"] + cases["samples"]:
        created[row["entity_type"]].append(db.create_entity(row["entity_type"], _payload(row)))
    return created


def test_fixture_is_public_safe_and_explicitly_synthetic(wearable_cases):
    encoded = json.dumps(wearable_cases, sort_keys=True).lower()
    assert wearable_cases["synthetic"] is True
    assert wearable_cases["owner_email"] == "owner@glucopilot.local"
    assert "password" not in encoded
    assert "access_token" not in encoded
    assert "refresh_token" not in encoded
    for row in wearable_cases["daily"] + wearable_cases["samples"]:
        assert row["id"].startswith("synthetic-")


def test_migration_adds_strict_tables_indexes_and_repository_boundaries(wearable_database):
    with sqlite3.connect(wearable_database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        daily_plan = " ".join(
            str(column)
            for row in connection.execute(
                """
                EXPLAIN QUERY PLAN SELECT * FROM wearable_daily
                WHERE owner_id=? AND entity_type=? AND observed_date>=?
                ORDER BY observed_date, entity_id LIMIT 120
                """,
                (DEPLOYMENT_OWNER_ID, "FitbitDaily", "2026-01-01"),
            )
            for column in row
        )
        sample_plan = " ".join(
            str(column)
            for row in connection.execute(
                """
                EXPLAIN QUERY PLAN SELECT * FROM wearable_samples
                WHERE owner_id=? AND entity_type=? AND observed_at>=?
                ORDER BY observed_at, entity_id LIMIT 400
                """,
                (DEPLOYMENT_OWNER_ID, "FitbitHeartRate", "2026-03-01T00:00:00.000Z"),
            )
            for column in row
        )

    assert {"wearable_daily", "wearable_samples"} <= tables
    assert {
        "idx_wearable_daily_owner_type_date",
        "idx_wearable_daily_owner_provider_date",
        "idx_wearable_daily_source_record",
        "idx_wearable_samples_owner_type_time",
        "idx_wearable_samples_owner_provider_time",
        "idx_wearable_samples_source_record",
    } <= indexes
    assert "idx_wearable_daily_owner_type_date" in daily_plan
    assert "idx_wearable_samples_owner_type_time" in sample_plan
    repositories = LegacyRepositoryCatalog()
    assert isinstance(repositories.oura_daily, WearableRepository)
    assert isinstance(repositories.oura_heart_rate, WearableRepository)
    assert isinstance(repositories.fitbit_daily, WearableRepository)
    assert isinstance(repositories.fitbit_heart_rate, WearableRepository)


def test_mapping_preserves_provider_overlap_null_presence_extras_and_time(wearable_cases):
    oura = map_legacy_daily(
        "OuraDaily", {**wearable_cases["daily"][0], **ENVELOPE}
    ).row
    fitbit = map_legacy_daily(
        "FitbitDaily", {**wearable_cases["daily"][1], **ENVELOPE}
    ).row
    google = map_legacy_daily(
        "FitbitDaily", {**wearable_cases["daily"][2], **ENVELOPE}
    ).row
    sample = map_legacy_sample(
        "FitbitHeartRate", {**wearable_cases["samples"][1], **ENVELOPE}
    ).row

    assert oura["provider"] == "oura"
    assert oura["source_present"] == 0
    assert "readiness_hrv_balance" in json.loads(oura["present_fields_json"])
    assert json.loads(oura["compatibility_extra_json"])["custom_synthetic_metric"] == "retained"
    assert {fitbit["provider"], google["provider"]} == {"fitbit", "google_health"}
    assert fitbit["observed_date"] == google["observed_date"]
    assert sample["observed_at"] == "2026-03-02T10:00:00.000Z"
    assert sample["source_timestamp"] == "2026-03-02T10:00:00Z"
    assert sample["source_record_canonical_id"].startswith("urn:glucopilot:source-record:")
    assert sample["mapping_version"] == MAPPING_VERSION

    with pytest.raises(WearableMappingError, match="between 0 and 100"):
        map_legacy_daily("OuraDaily", {**wearable_cases["invalid_daily"], **ENVELOPE})
    with pytest.raises(WearableMappingError, match="between 1 and 400"):
        map_legacy_sample("FitbitHeartRate", {**wearable_cases["invalid_sample"], **ENVELOPE})


def test_feature_gated_dual_write_bulk_upsert_and_cascade(
    wearable_cases,
    wearable_database,
    monkeypatch,
):
    monkeypatch.setenv("TYPED_WEARABLE_WRITES_ENABLED", "true")
    created = _create_fixture_rows(wearable_cases)
    typed_oura = SqliteTypedWearableRepository("OuraDaily")
    typed_fitbit = SqliteTypedWearableRepository("FitbitDaily")
    typed_sample = SqliteTypedWearableRepository("FitbitHeartRate")

    oura_row = created["OuraDaily"][0]
    assert typed_oura.get(oura_row["id"])["custom_synthetic_metric"] == "retained"
    assert "readiness_hrv_balance" in typed_oura.get(oura_row["id"])
    assert typed_oura.get(oura_row["id"])["readiness_hrv_balance"] is None
    assert len(typed_fitbit.query({"date": "2026-03-02"}, "source")) == 2
    assert len(typed_fitbit.query({"source": "google_health"})) == 1

    db.update_entity("OuraDaily", oura_row["id"], {"sleep_score": 88})
    assert typed_oura.get(oura_row["id"])["sleep_score"] == 88
    sample = created["FitbitHeartRate"][0]
    assert db.delete_entity("FitbitHeartRate", sample["id"])
    assert typed_sample.get(sample["id"]) is None

    with sqlite3.connect(wearable_database) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE wearable_daily SET sleep_score=120")


def test_backfill_is_bounded_restartable_and_query_parity_matches(
    wearable_cases,
    wearable_database,
    monkeypatch,
):
    monkeypatch.setenv("TYPED_WEARABLE_WRITES_ENABLED", "false")
    _create_fixture_rows(wearable_cases)
    db.create_entity("OuraDaily", _payload(wearable_cases["invalid_daily"]))
    db.create_entity("FitbitHeartRate", _payload(wearable_cases["invalid_sample"]))
    start = datetime(2026, 3, 1, tzinfo=timezone.utc)
    rows = [
        {
            "timestamp": (start + timedelta(minutes=index)).isoformat().replace("+00:00", "Z"),
            "bpm": 55 + index % 40,
            "source": "oura",
            "owner_email": "owner@glucopilot.local",
        }
        for index in range(205)
    ]
    db.bulk_create_entities("OuraHeartRate", rows)

    observed_batches: list[int] = []
    original = SqliteTypedWearableRepository.sync_entities

    def observe(self, entities):
        observed_batches.append(len(entities))
        return original(self, entities)

    monkeypatch.setattr(SqliteTypedWearableRepository, "sync_entities", observe)
    monkeypatch.setenv("TYPED_WEARABLE_WRITES_ENABLED", "true")
    first = backfill_typed_wearables(wearable_database, batch_size=17)
    second = backfill_typed_wearables(wearable_database, batch_size=31)
    assert max(observed_batches) <= 31
    assert first == second
    assert first["OuraDaily"] == {"legacy_scanned": 2, "typed_written": 1, "unmappable": 1}
    assert first["OuraHeartRate"] == {
        "legacy_scanned": 206,
        "typed_written": 206,
        "unmappable": 0,
    }
    assert first["FitbitHeartRate"] == {
        "legacy_scanned": 2,
        "typed_written": 1,
        "unmappable": 1,
    }
    comparison = compare_wearable_stores(wearable_database)
    for entity_type in WEARABLE_TYPES:
        domain = comparison["domains"][entity_type]
        assert domain["missing"] == 0
        assert domain["mismatched"] == 0
        assert domain["extra"] == 0
        assert domain["query"]["count_match"] is True
        assert domain["query"]["checksum_match"] is True
        assert domain["query"]["ordering_match"] is True
        assert domain["query"]["aggregate_match"] is True
    assert comparison["domains"]["OuraDaily"]["unmappable_by_reason"] == {
        "value_out_of_range": 1
    }
    assert comparison["domains"]["FitbitHeartRate"]["unmappable_by_reason"] == {
        "value_out_of_range": 1
    }


def test_high_volume_typed_query_avoids_json_store(
    wearable_database,
    monkeypatch,
):
    monkeypatch.setenv("TYPED_WEARABLE_WRITES_ENABLED", "true")
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db.bulk_create_entities(
        "FitbitHeartRate",
        [
            {
                "timestamp": (start + timedelta(minutes=index)).isoformat().replace("+00:00", "Z"),
                "bpm": 50 + index % 60,
                "source": "google_health",
                "owner_email": "owner@glucopilot.local",
            }
            for index in range(3000)
        ],
    )
    repositories = LegacyRepositoryCatalog()
    monkeypatch.setenv("TYPED_WEARABLE_READS_ENABLED", "true")
    monkeypatch.setenv("TYPED_WEARABLE_SHADOW_READS_ENABLED", "false")

    def unexpected_legacy_query(*_args, **_kwargs):
        raise AssertionError("supported typed wearable query touched JSON storage")

    monkeypatch.setattr(repositories.fitbit_heart_rate._legacy, "query", unexpected_legacy_query)
    rows = repositories.fitbit_heart_rate.query(
        {"timestamp": {"$gte": "2026-01-02T00:00:00Z"}}, "-timestamp", 400
    )
    assert len(rows) == 400
    assert rows[0]["timestamp"] > rows[-1]["timestamp"]


def test_shadow_and_read_flags_preserve_supported_and_fallback_queries(
    wearable_cases,
    wearable_database,
    monkeypatch,
    caplog,
):
    monkeypatch.setenv("TYPED_WEARABLE_WRITES_ENABLED", "true")
    _create_fixture_rows(wearable_cases)
    repositories = LegacyRepositoryCatalog()
    filters = {"owner_email": "owner@glucopilot.local", "source": "google_health"}

    monkeypatch.setenv("TYPED_WEARABLE_SHADOW_READS_ENABLED", "true")
    with caplog.at_level(logging.INFO, logger="glucopilot.typed_wearables"):
        legacy = repositories.fitbit_daily.query(filters, "date", 100)
    assert "typed wearable shadow" in caplog.text
    assert '"checksum_match": true' in caplog.text

    monkeypatch.setenv("TYPED_WEARABLE_READS_ENABLED", "true")
    assert repositories.fitbit_daily.query(filters, "date", 100) == legacy
    assert repositories.fitbit_daily.query({"unknown_legacy_field": None}) == db.query_entities(
        "FitbitDaily", {"unknown_legacy_field": None}
    )
    assert repositories.fitbit_daily.query({"source": {"$in": ["google_health"]}}) == db.query_entities(
        "FitbitDaily", {"source": {"$in": ["google_health"]}}
    )


def test_projection_failure_rolls_back_legacy_write(wearable_database, monkeypatch):
    monkeypatch.setenv("TYPED_WEARABLE_WRITES_ENABLED", "true")

    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic typed wearable failure")

    monkeypatch.setattr("server.typed_wearables._upsert_many", fail)
    with pytest.raises(RuntimeError, match="synthetic typed wearable failure"):
        db.create_entity(
            "FitbitDaily",
            {
                "date": "2026-03-02",
                "source": "google_health",
                "steps": 100,
                "owner_email": "owner@glucopilot.local",
            },
        )
    assert db.query_entities("FitbitDaily") == []


def test_verified_backup_preserves_typed_wearable_counts(
    wearable_cases,
    wearable_database,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("TYPED_WEARABLE_WRITES_ENABLED", "true")
    _create_fixture_rows(wearable_cases)
    (wearable_database.parent / "records").mkdir()
    backup, verification = create_verified_backup(
        wearable_database.parent,
        tmp_path / "backups",
        reason="synthetic-typed-wearables",
    )
    assert verification["typed_wearable_daily_count"] == 3
    assert verification["typed_wearable_sample_count"] == 2
    assert verify_backup(backup) == verification
