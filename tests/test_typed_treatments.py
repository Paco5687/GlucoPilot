from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from server import db
from server.backup import create_verified_backup, verify_backup
from server.data_contracts import DEPLOYMENT_OWNER_ID
from server.migrations import run_migrations
from server.repositories import (
    BasalSegmentRepository,
    LegacyRepositoryCatalog,
    PumpDailyTotalRepository,
    TreatmentRepository,
)
from server.typed_treatments import (
    MAPPING_VERSION,
    SqliteBasalSegmentRepository,
    SqlitePumpDailyTotalRepository,
    SqliteTypedTreatmentRepository,
    TreatmentMappingError,
    backfill_typed_treatments,
    compare_treatment_stores,
    map_legacy_treatment,
    parse_pump_daily_total,
)
from server.unit_of_work import unit_of_work


pytestmark = pytest.mark.risk_critical
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "golden" / "clinical_edge_cases.json"


@pytest.fixture(scope="module")
def treatment_cases() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["typed_treatments"]


@pytest.fixture
def treatment_database(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database = data_dir / "app.sqlite3"
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    return database


def _without_id(row: dict) -> dict:
    return {key: value for key, value in row.items() if key != "id"}


def test_migration_adds_strict_tables_indexes_and_repository_boundaries(treatment_database):
    with sqlite3.connect(treatment_database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
    assert {"typed_treatments", "basal_segments", "pump_daily_totals"} <= tables
    assert {
        "idx_typed_treatments_owner_time",
        "idx_typed_treatments_owner_source_time",
        "idx_typed_treatments_source_record",
        "idx_basal_segments_owner_time",
        "idx_pump_daily_totals_owner_date",
    } <= indexes

    repositories = LegacyRepositoryCatalog()
    assert isinstance(repositories.treatments, TreatmentRepository)
    assert isinstance(repositories.basal_segments, BasalSegmentRepository)
    assert isinstance(repositories.pump_daily_totals, PumpDailyTotalRepository)

    with sqlite3.connect(treatment_database) as connection, pytest.raises(
        sqlite3.IntegrityError
    ):
        connection.execute(
            """
            INSERT INTO pump_daily_totals (
                treatment_entity_id, owner_id, source, occurred_at, local_date,
                total_units, completeness, mapping_version, created_at, updated_at
            ) VALUES ('missing', ?, 'synthetic', '2026-01-01T00:00:00.000Z',
                      '2026-01-01', -1, 'complete', ?,
                      '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z')
            """,
            (DEPLOYMENT_OWNER_ID, MAPPING_VERSION),
        )


def test_all_legacy_subtypes_map_without_losing_units_or_intervals(treatment_cases):
    projections = {
        row["id"]: map_legacy_treatment(
            {
                **row,
                "created_date": "2026-01-17T00:00:00Z",
                "updated_date": "2026-01-17T00:01:00Z",
            }
        )
        for row in treatment_cases["rows"]
    }
    assert projections["synthetic-treatment-bolus"].treatment["amount_unit"] == "U"
    assert projections["synthetic-treatment-bolus"].treatment["owner_id"] == DEPLOYMENT_OWNER_ID
    assert projections["synthetic-treatment-bolus"].treatment[
        "source_record_canonical_id"
    ].startswith("urn:glucopilot:source-record:")
    assert projections["synthetic-treatment-carbs"].treatment["amount_unit"] == "g"
    assert projections["synthetic-treatment-bg"].treatment["glucose_mg_dl"] == 118
    assert projections["synthetic-treatment-note"].treatment["kind"] == "note"

    basal = projections["synthetic-treatment-basal"].basal_segment
    assert basal == {
        **basal,
        "segment_kind": "temp_basal",
        "started_at": "2026-01-16T11:00:00.000Z",
        "ended_at": "2026-01-16T11:30:00.000Z",
        "duration_seconds": 1800.0,
        "rate_units_per_hour": 0.85,
        "percent_of_profile": 110.0,
    }
    suspension = projections["synthetic-treatment-suspension"].basal_segment
    assert suspension["segment_kind"] == "suspension"
    assert suspension["rate_units_per_hour"] == 0

    total = projections["synthetic-treatment-total"].pump_daily_total
    assert total["total_units"] == 25.75
    assert total["basal_units"] == 18.25
    assert total["bolus_units"] == 7.5
    assert total["completeness"] == "complete"
    assert parse_pump_daily_total("Total: 12U") == {
        "total_units": 12.0,
        "basal_units": None,
        "bolus_units": None,
        "completeness": "partial",
    }

    with pytest.raises(TreatmentMappingError, match="timestamp"):
        map_legacy_treatment(treatment_cases["invalid"])


def test_feature_gated_dual_write_is_atomic_idempotent_and_cascades(
    treatment_cases,
    treatment_database,
    monkeypatch,
):
    monkeypatch.setenv("TYPED_TREATMENT_WRITES_ENABLED", "true")
    rows = db.bulk_create_entities(
        "Treatment", [_without_id(row) for row in treatment_cases["rows"]]
    )
    expected = treatment_cases["expected"]
    with sqlite3.connect(treatment_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM typed_treatments").fetchone()[0] == expected[
            "typed_count"
        ]
        assert connection.execute("SELECT COUNT(*) FROM basal_segments").fetchone()[0] == expected[
            "basal_count"
        ]
        assert connection.execute("SELECT COUNT(*) FROM pump_daily_totals").fetchone()[0] == expected[
            "daily_total_count"
        ]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE typed_treatments SET amount_value=-1")

    repository = SqliteTypedTreatmentRepository()
    projections = repository.sync_entities(rows)
    assert all(projections)
    with sqlite3.connect(treatment_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM typed_treatments").fetchone()[0] == len(rows)

    basal_row = next(row for row in rows if row["type"] == "tempbasal")
    db.update_entity(
        "Treatment",
        basal_row["id"],
        {"type": "note", "event_type": "Synthetic update", "duration": None, "absolute": None},
    )
    assert SqliteBasalSegmentRepository().for_treatment(basal_row["id"]) is None
    assert repository.get(basal_row["id"])["type"] == "note"

    total_row = next(row for row in rows if row.get("event_type") == "Daily Total")
    assert SqlitePumpDailyTotalRepository().for_treatment(total_row["id"])["total_units"] == 25.75
    assert db.delete_entity("Treatment", total_row["id"])
    assert repository.get(total_row["id"]) is None
    assert SqlitePumpDailyTotalRepository().for_treatment(total_row["id"]) is None


def test_flag_off_preserves_generic_api_shape_and_backfill_is_explicit(
    treatment_cases,
    treatment_database,
    monkeypatch,
):
    monkeypatch.setenv("TYPED_TREATMENT_WRITES_ENABLED", "false")
    requested = _without_id(treatment_cases["rows"][0])
    legacy = db.create_entity("Treatment", requested)
    invalid = db.create_entity("Treatment", _without_id(treatment_cases["invalid"]))
    assert {key: legacy[key] for key in requested} == requested
    assert db.query_entities("Treatment", {"id": legacy["id"]})[0] == legacy
    assert SqliteTypedTreatmentRepository().get(legacy["id"]) is None
    with pytest.raises(RuntimeError, match="must be true"):
        backfill_typed_treatments(treatment_database)

    monkeypatch.setenv("TYPED_TREATMENT_WRITES_ENABLED", "true")
    first = backfill_typed_treatments(treatment_database, batch_size=1)
    second = backfill_typed_treatments(treatment_database, batch_size=2)
    assert first == {"legacy_scanned": 2, "typed_written": 1, "unmappable": 1}
    assert second == first
    assert compare_treatment_stores(treatment_database) == {
        "legacy_total": 2,
        "mappable": 1,
        "unmappable": 1,
        "typed_total": 1,
        "matched": 1,
        "missing": 0,
        "mismatched": 0,
        "fingerprint_drift": 0,
        "extra": 0,
        "duplicate_source_identities": 0,
        "basal_segments": 0,
        "pump_daily_totals": 0,
        "mapping_version": MAPPING_VERSION,
    }
    with sqlite3.connect(treatment_database) as connection:
        connection.execute(
            "UPDATE typed_treatments SET legacy_fingerprint=? WHERE entity_id=?",
            ("sha256:" + "0" * 64, legacy["id"]),
        )
    changed = compare_treatment_stores(treatment_database)
    assert changed["matched"] == 0
    assert changed["mismatched"] == 1
    assert changed["fingerprint_drift"] == 1
    assert db.query_entities("Treatment", {"id": invalid["id"]})[0]["timestamp"] == "not-a-time"


def test_duplicate_provider_identity_is_visible_without_breaking_legacy_writes(
    treatment_database,
    monkeypatch,
):
    monkeypatch.setenv("TYPED_TREATMENT_WRITES_ENABLED", "true")
    shared = {
        "timestamp": "2026-01-16T10:00:00Z",
        "source": "synthetic",
        "ns_id": "synthetic-shared-source-id",
        "type": "insulin",
        "amount": 1,
        "owner_email": "owner@glucopilot.local",
    }
    first, second = db.bulk_create_entities("Treatment", [shared, shared])
    assert first["id"] != second["id"]
    comparison = compare_treatment_stores(treatment_database)
    assert comparison["matched"] == 2
    assert comparison["duplicate_source_identities"] == 1


def test_typed_read_flag_preserves_supported_legacy_query_results(
    treatment_cases,
    treatment_database,
    monkeypatch,
):
    monkeypatch.setenv("TYPED_TREATMENT_WRITES_ENABLED", "true")
    db.bulk_create_entities("Treatment", [_without_id(row) for row in treatment_cases["rows"]])
    repositories = LegacyRepositoryCatalog()
    filters = {
        "owner_email": "owner@glucopilot.local",
        "timestamp": {"$gte": "2026-01-16T10:00:00Z"},
    }
    monkeypatch.setenv("TYPED_TREATMENT_READS_ENABLED", "false")
    legacy = repositories.treatments.query(filters, "timestamp", 100)
    monkeypatch.setenv("TYPED_TREATMENT_READS_ENABLED", "true")
    typed = repositories.treatments.query(filters, "timestamp", 100)
    fields = ("id", "timestamp", "source", "type", "event_type", "amount", "ns_id")
    assert [{key: row.get(key) for key in fields} for row in typed] == [
        {key: row.get(key) for key in fields} for row in legacy
    ]

    # An unknown legacy JSON field intentionally falls back instead of changing
    # an existing core consumer's query semantics during this additive release.
    assert repositories.treatments.query({"percent": 110}, limit=10)[0]["type"] == "tempbasal"


def test_legacy_and_typed_writes_roll_back_in_one_unit_of_work(
    treatment_database,
    monkeypatch,
):
    monkeypatch.setenv("TYPED_TREATMENT_WRITES_ENABLED", "true")
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        with unit_of_work() as work:
            work.repositories.treatments.create(
                {
                    "timestamp": "2026-01-16T10:00:00Z",
                    "source": "synthetic",
                    "type": "insulin",
                    "amount": 1,
                    "owner_email": "owner@glucopilot.local",
                }
            )
            work.commit()
            raise RuntimeError("synthetic rollback")

    with sqlite3.connect(treatment_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM typed_treatments").fetchone()[0] == 0


def test_verified_backup_preserves_all_typed_treatment_counts(
    treatment_database,
    treatment_cases,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("TYPED_TREATMENT_WRITES_ENABLED", "true")
    db.bulk_create_entities("Treatment", [_without_id(row) for row in treatment_cases["rows"]])
    data_dir = treatment_database.parent
    backup, verification = create_verified_backup(data_dir, tmp_path / "backups", reason="typed-test")
    assert verification["typed_treatment_count"] == treatment_cases["expected"]["typed_count"]
    assert verification["basal_segment_count"] == treatment_cases["expected"]["basal_count"]
    assert verification["pump_daily_total_count"] == treatment_cases["expected"]["daily_total_count"]
    assert verify_backup(backup)["typed_treatment_count"] == treatment_cases["expected"]["typed_count"]
