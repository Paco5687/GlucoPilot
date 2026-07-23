from __future__ import annotations

import json
import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from server import db
from server.backup import create_verified_backup, verify_backup
from server.data_contracts import DEPLOYMENT_OWNER_ID
from server.migrations import run_migrations
from server.readings import persist_readings_deduped
from server.repositories import (
    FingerstickRepository,
    GlucoseRepository,
    LegacyRepositoryCatalog,
)
from server.typed_glucose import (
    MAPPING_VERSION,
    GlucoseMappingError,
    SqliteTypedFingerstickRepository,
    SqliteTypedGlucoseRepository,
    backfill_typed_glucose,
    compare_glucose_stores,
    map_legacy_fingerstick,
    map_legacy_glucose,
)
from server.unit_of_work import unit_of_work


pytestmark = pytest.mark.risk_critical
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "golden" / "typed_glucose.json"
ENVELOPE = {
    "created_date": "2026-03-01T13:00:00Z",
    "updated_date": "2026-03-01T13:01:00Z",
}


@pytest.fixture(scope="module")
def glucose_cases() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def glucose_database(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database = data_dir / "app.sqlite3"
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    return database


def _without_id(row: dict) -> dict:
    return {key: value for key, value in row.items() if key != "id"}


def _with_envelope(row: dict) -> dict:
    return {**row, **ENVELOPE}


def test_fixture_is_public_safe_and_explicitly_synthetic(glucose_cases):
    encoded = json.dumps(glucose_cases, sort_keys=True).lower()
    assert glucose_cases["synthetic"] is True
    assert glucose_cases["owner_email"] == "owner@glucopilot.local"
    assert "password" not in encoded
    assert "access_token" not in encoded
    assert "refresh_token" not in encoded
    assert all(row["id"].startswith("synthetic-") for row in glucose_cases["glucose"])


def test_migration_adds_strict_tables_indexes_and_repository_boundaries(glucose_database):
    with sqlite3.connect(glucose_database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO glucose_readings (
                    entity_id, canonical_id, owner_id, owner_email, source,
                    observed_at, source_timestamp, local_date, value_mg_dl,
                    assertion_kind, source_class, legacy_fingerprint,
                    mapping_version, received_at, recorded_at, created_at, updated_at
                ) VALUES (
                    'missing', 'urn:synthetic', ?, 'owner@glucopilot.local', 'synthetic',
                    '2026-03-01T10:00:00.000Z', '2026-03-01T10:00:00Z', '2026-03-01', 5,
                    'source_fact', 'import', ?, ?,
                    '2026-03-01T10:00:00.000Z', '2026-03-01T10:00:00.000Z',
                    '2026-03-01T10:00:00.000Z', '2026-03-01T10:00:00.000Z'
                )
                """,
                (DEPLOYMENT_OWNER_ID, "sha256:" + "0" * 64, MAPPING_VERSION),
            )

    assert {"glucose_readings", "fingerstick_readings"} <= tables
    assert {
        "idx_glucose_readings_owner_time",
        "idx_glucose_readings_owner_source_time",
        "idx_glucose_readings_source_record",
        "idx_fingerstick_readings_owner_time",
        "idx_fingerstick_readings_pair",
    } <= indexes
    repositories = LegacyRepositoryCatalog()
    assert isinstance(repositories.glucose, GlucoseRepository)
    assert isinstance(repositories.fingersticks, FingerstickRepository)

    with sqlite3.connect(glucose_database) as connection:
        plan = " ".join(
            str(column)
            for row in connection.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT * FROM glucose_readings
                WHERE owner_id=? AND observed_at>=?
                ORDER BY observed_at, entity_id
                LIMIT 100
                """,
                (DEPLOYMENT_OWNER_ID, "2026-03-01T00:00:00.000Z"),
            )
            for column in row
        )
    assert "idx_glucose_readings_owner_time" in plan


def test_mapping_preserves_identity_time_value_pair_and_assertion(glucose_cases):
    glucose = map_legacy_glucose(_with_envelope(glucose_cases["glucose"][1])).row
    assert glucose["owner_id"] == DEPLOYMENT_OWNER_ID
    assert glucose["observed_at"] == "2026-03-01T10:05:00.000Z"
    assert glucose["source_timestamp"] == "2026-03-01T10:05:00Z"
    assert glucose["value_mg_dl"] == 112
    assert glucose["assertion_kind"] == "source_fact"
    assert glucose["source_record_canonical_id"].startswith("urn:glucopilot:source-record:")

    fingerstick = map_legacy_fingerstick(_with_envelope(glucose_cases["fingerstick"])).row
    assert fingerstick["assertion_kind"] == "patient_report"
    assert fingerstick["paired_glucose_entity_id"] == "synthetic-glucose-share"
    assert fingerstick["paired_delta_mg_dl"] == 5
    assert fingerstick["paired_glucose_source_timestamp"] == "2026-03-01T10:00:00Z"
    assert fingerstick["pair_offset_seconds"] == -60
    assert fingerstick["paired_glucose_trend"] == "Flat"
    assert fingerstick["absolute_difference_mg_dl"] == 5
    assert fingerstick["relative_difference_percent"] == 5.3
    assert fingerstick["directional_difference"] == "within_comparison_band"
    assert fingerstick["low_classification"] == "neither_low"
    assert json.loads(fingerstick["context_json"])["sensor_day"] == 2
    assert fingerstick["reconciliation_version"] == "glucose-reconciliation/1.0.0"

    with pytest.raises(GlucoseMappingError, match="between 20 and 600"):
        map_legacy_glucose(_with_envelope(glucose_cases["invalid_glucose"]))
    with pytest.raises(GlucoseMappingError, match="does not match"):
        map_legacy_fingerstick(_with_envelope(glucose_cases["invalid_fingerstick"]))


def test_feature_gated_dual_write_is_atomic_idempotent_and_cascades(
    glucose_cases,
    glucose_database,
    monkeypatch,
):
    monkeypatch.setenv("TYPED_GLUCOSE_WRITES_ENABLED", "true")
    glucose_rows = db.bulk_create_entities(
        "GlucoseReading", [_without_id(row) for row in glucose_cases["glucose"]]
    )
    fingerstick_payload = {
        **_without_id(glucose_cases["fingerstick"]),
        "cgm_reading_id": glucose_rows[0]["id"],
    }
    fingerstick = db.create_entity("FingerstickReading", fingerstick_payload)

    typed_glucose = SqliteTypedGlucoseRepository()
    typed_fingersticks = SqliteTypedFingerstickRepository()
    assert typed_glucose.get(glucose_rows[0]["id"])["value"] == 100
    typed_fingerstick = typed_fingersticks.get(fingerstick["id"])
    assert typed_fingerstick["cgm_reading_id"] == glucose_rows[0]["id"]
    assert typed_fingerstick["sensor_day"] == 2
    assert typed_fingerstick["compression_possible"] is False
    assert typed_fingerstick["low_classification"] == "neither_low"

    typed_glucose.sync_entities(glucose_rows)
    with sqlite3.connect(glucose_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM glucose_readings").fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE glucose_readings SET value_mg_dl=5")

    db.update_entity("GlucoseReading", glucose_rows[0]["id"], {"value": 105})
    assert typed_glucose.get(glucose_rows[0]["id"])["value"] == 105
    assert db.delete_entity("FingerstickReading", fingerstick["id"])
    assert typed_fingersticks.get(fingerstick["id"]) is None


def test_repository_dedup_matches_boundary_and_is_idempotent(
    glucose_cases,
    glucose_database,
    monkeypatch,
):
    monkeypatch.setenv("TYPED_GLUCOSE_WRITES_ENABLED", "true")
    db.create_entity("GlucoseReading", _without_id(glucose_cases["glucose"][0]))

    created, skipped = persist_readings_deduped(glucose_cases["dedup_incoming"])
    assert (created, skipped) == (1, 3)
    stored = db.query_entities("GlucoseReading", sort="timestamp")
    assert [row["timestamp"] for row in stored] == [
        "2026-03-01T10:00:00Z",
        "2026-03-01T10:04:01Z",
    ]
    assert SqliteTypedGlucoseRepository().query(sort="timestamp") == stored

    assert persist_readings_deduped(glucose_cases["dedup_incoming"]) == (0, 4)
    assert compare_glucose_stores(glucose_database)["glucose"]["matched"] == 2


def test_repository_dedup_serializes_overlapping_writers(glucose_database):
    barrier = threading.Barrier(2)

    def write(value: int) -> tuple[int, int]:
        barrier.wait()
        created, skipped = LegacyRepositoryCatalog().glucose.create_deduplicated(
            [
                {
                    "timestamp": "2026-03-01T10:00:00Z",
                    "value": value,
                    "source": "synthetic",
                    "owner_email": "owner@glucopilot.local",
                }
            ]
        )
        return len(created), skipped

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(write, (100, 101)))

    assert sorted(outcomes) == [(0, 1), (1, 0)]
    assert len(db.query_entities("GlucoseReading")) == 1


def test_backfill_is_restartable_and_parity_checks_counts_checksums_order_and_aggregate(
    glucose_cases,
    glucose_database,
    monkeypatch,
):
    monkeypatch.setenv("TYPED_GLUCOSE_WRITES_ENABLED", "false")
    glucose_rows = db.bulk_create_entities(
        "GlucoseReading", [_without_id(row) for row in glucose_cases["glucose"]]
    )
    fingerstick = db.create_entity(
        "FingerstickReading",
        {**_without_id(glucose_cases["fingerstick"]), "cgm_reading_id": glucose_rows[0]["id"]},
    )
    db.create_entity("GlucoseReading", _without_id(glucose_cases["invalid_glucose"]))
    assert SqliteTypedGlucoseRepository().get(glucose_rows[0]["id"]) is None

    monkeypatch.setenv("TYPED_GLUCOSE_WRITES_ENABLED", "true")
    SqliteTypedGlucoseRepository().sync_entities([glucose_rows[0]])
    first = backfill_typed_glucose(glucose_database, batch_size=1)
    second = backfill_typed_glucose(glucose_database, batch_size=2)
    assert first == second == {
        "glucose_legacy_scanned": 3,
        "glucose_typed_written": 2,
        "glucose_unmappable": 1,
        "fingerstick_legacy_scanned": 1,
        "fingerstick_typed_written": 1,
        "fingerstick_unmappable": 0,
    }
    comparison = compare_glucose_stores(glucose_database)
    for domain in ("glucose", "fingersticks"):
        assert comparison[domain]["missing"] == 0
        assert comparison[domain]["mismatched"] == 0
        assert comparison[domain]["extra"] == 0
        assert comparison[domain]["query"]["count_match"] is True
        assert comparison[domain]["query"]["checksum_match"] is True
        assert comparison[domain]["query"]["ordering_match"] is True
        assert comparison[domain]["query"]["aggregate_match"] is True
    assert comparison["glucose"]["unmappable_by_reason"] == {
        "value_out_of_range": 1
    }
    assert comparison["fingersticks"]["unmappable_by_reason"] == {}
    assert SqliteTypedFingerstickRepository().get(fingerstick["id"])["delta"] == 5


def test_shadow_and_typed_read_flags_preserve_supported_queries(
    glucose_cases,
    glucose_database,
    monkeypatch,
    caplog,
):
    monkeypatch.setenv("TYPED_GLUCOSE_WRITES_ENABLED", "true")
    glucose_rows = db.bulk_create_entities(
        "GlucoseReading",
        [_without_id(row) for row in glucose_cases["glucose"]],
    )
    db.create_entity(
        "FingerstickReading",
        {
            **_without_id(glucose_cases["fingerstick"]),
            "cgm_reading_id": glucose_rows[0]["id"],
        },
    )
    repositories = LegacyRepositoryCatalog()
    filters = {"owner_email": "owner@glucopilot.local", "timestamp": {"$gte": "2026-03-01T10:00:00Z"}}

    monkeypatch.setenv("TYPED_GLUCOSE_SHADOW_READS_ENABLED", "true")
    monkeypatch.setenv("TYPED_GLUCOSE_READS_ENABLED", "false")
    with caplog.at_level(logging.INFO, logger="glucopilot.typed_glucose"):
        legacy = repositories.glucose.query(filters, "timestamp", 100)
    assert "typed glucose shadow" in caplog.text
    assert '"checksum_match": true' in caplog.text
    assert '"legacy_ms":' in caplog.text

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="glucopilot.typed_glucose"):
        repositories.fingersticks.query(
            {"owner_email": "owner@glucopilot.local"},
            "timestamp",
            100,
        )
    assert "typed fingerstick shadow" in caplog.text
    assert '"checksum_match": true' in caplog.text
    assert '"legacy_ms":' in caplog.text

    monkeypatch.setenv("TYPED_GLUCOSE_READS_ENABLED", "true")
    typed = repositories.glucose.query(filters, "timestamp", 100)
    assert typed == legacy
    assert repositories.glucose.query({"unknown_legacy_field": None}, limit=10) == db.query_entities(
        "GlucoseReading", {"unknown_legacy_field": None}, limit=10
    )

    monkeypatch.setenv("TYPED_GLUCOSE_SHADOW_READS_ENABLED", "false")

    def unexpected_legacy_query(*_args, **_kwargs):
        raise AssertionError("typed-only supported queries must not touch legacy storage")

    monkeypatch.setattr(repositories.glucose._legacy, "query", unexpected_legacy_query)
    assert repositories.glucose.query(filters, "timestamp", 100) == typed


def test_legacy_and_typed_writes_roll_back_together(glucose_database, monkeypatch):
    monkeypatch.setenv("TYPED_GLUCOSE_WRITES_ENABLED", "true")
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        with unit_of_work() as work:
            work.repositories.glucose.create(
                {
                    "timestamp": "2026-03-01T10:00:00Z",
                    "value": 100,
                    "source": "synthetic",
                    "owner_email": "owner@glucopilot.local",
                }
            )
            work.commit()
            raise RuntimeError("synthetic rollback")
    with sqlite3.connect(glucose_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM glucose_readings").fetchone()[0] == 0


def test_verified_backup_preserves_typed_glucose_counts(
    glucose_cases,
    glucose_database,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("TYPED_GLUCOSE_WRITES_ENABLED", "true")
    glucose_rows = db.bulk_create_entities(
        "GlucoseReading", [_without_id(row) for row in glucose_cases["glucose"]]
    )
    db.create_entity(
        "FingerstickReading",
        {**_without_id(glucose_cases["fingerstick"]), "cgm_reading_id": glucose_rows[0]["id"]},
    )
    (glucose_database.parent / "records").mkdir()
    backup, verification = create_verified_backup(
        glucose_database.parent,
        tmp_path / "backups",
        reason="synthetic-typed-glucose",
    )
    assert verification["typed_glucose_reading_count"] == 2
    assert verification["typed_fingerstick_reading_count"] == 1
    assert verify_backup(backup) == verification
