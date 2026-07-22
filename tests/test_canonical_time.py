from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from server import db, report
from server.backup import create_verified_backup, verify_backup
from server.canonical_time import (
    NORMALIZER_VERSION,
    SqliteClinicalTimeRepository,
    backfill_canonical_times,
    normalize_entity_times,
    normalize_time_value,
)
from server.migrations import run_migrations
from server.repositories import ClinicalTimeRepository, LegacyRepositoryCatalog
from server.unit_of_work import unit_of_work


pytestmark = pytest.mark.risk_critical
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "golden" / "clinical_time_edge_cases.json"


@pytest.fixture
def cases() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def time_database(tmp_path, monkeypatch):
    database = tmp_path / "app.sqlite3"
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    return database


def _entity(entity_id: str, **values):
    return {
        "id": entity_id,
        "owner_email": "owner@glucopilot.local",
        "created_date": "2026-07-21T16:00:00Z",
        "updated_date": "2026-07-21T16:05:00Z",
        **values,
    }


def test_migration_adds_queryable_canonical_time_sidecar(time_database):
    with sqlite3.connect(time_database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(canonical_times)")}
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(canonical_times)")}
    assert {
        "entity_id",
        "timeline_role",
        "source_text",
        "timeline_at",
        "observed_at",
        "recorded_at",
        "received_at",
        "effective_start",
        "effective_end",
        "local_date",
        "timezone",
        "precision",
        "basis",
        "dst_resolution",
        "normalization_status",
        "duration_seconds",
        "normalizer_version",
    } <= columns
    assert {"idx_canonical_times_timeline", "idx_canonical_times_local_date"} <= indexes
    assert isinstance(LegacyRepositoryCatalog().clinical_time, ClinicalTimeRepository)


def test_cross_source_offsets_align_on_one_canonical_timeline(cases, time_database, monkeypatch):
    monkeypatch.setenv("CANONICAL_TIME_ENABLED", "true")
    first = db.create_entity(
        "GlucoseReading",
        {"timestamp": cases["aligned_instants"][0], "value": 120, "source": "dexcom"},
    )
    second = db.create_entity(
        "FitbitHeartRate",
        {"timestamp": cases["aligned_instants"][1], "bpm": 70, "source": "fitbit"},
    )

    rows = SqliteClinicalTimeRepository().timeline(
        "2026-07-21T14:29:00Z",
        "2026-07-21T14:31:00Z",
    )
    clinical = [row for row in rows if row["entity_id"] in {first["id"], second["id"]}]
    assert len(clinical) == 2
    assert {row["timeline_at"] for row in clinical} == {"2026-07-21T14:30:00.000Z"}
    assert {row["source_text"] for row in clinical} == set(cases["aligned_instants"])


def test_date_only_and_inferred_values_never_claim_exact_instants(cases):
    lab = normalize_entity_times(
        "LabResult",
        _entity("lab", collected_date=cases["date_only"], source="medical_record"),
        default_timezone=cases["timezone"],
    )
    observed = next(row for row in lab if row["role"] == "observed")
    assert observed["precision"] == "day"
    assert observed["normalization_status"] == "partial"
    assert observed["source_text"] == cases["date_only"]
    assert observed["canonical_at"] is None

    period = normalize_entity_times(
        "PeriodLog",
        _entity("period", date=cases["inferred_date"], source="oura_inferred"),
        default_timezone=cases["timezone"],
    )
    inferred = next(row for row in period if row["role"] == "observed")
    assert inferred["basis"] == "inferred"
    assert inferred["inferred"] == 1
    assert inferred["canonical_at"] is None


def test_dst_fold_gap_and_explicit_resolution_are_lossless(cases):
    fold = cases["fall_back"]
    unresolved = normalize_time_value(
        "GlucoseReading",
        _entity("fold", timestamp=fold["local"], source="synthetic", timezone=cases["timezone"]),
        "timestamp",
        "observed",
        fold["local"],
        default_timezone=cases["timezone"],
    )
    assert unresolved["normalization_status"] == "ambiguous"
    assert unresolved["dst_resolution"] == "unresolved"
    assert unresolved["canonical_at"] is None

    for offset, expected, resolution in (
        (fold["earlier_offset"], fold["earlier_utc"], "ambiguous_earlier_offset"),
        (fold["later_offset"], fold["later_utc"], "ambiguous_later_offset"),
    ):
        resolved = normalize_time_value(
            "GlucoseReading",
            _entity(
                f"fold-{offset}",
                timestamp=fold["local"],
                source="synthetic",
                timezone=cases["timezone"],
                utc_offset=offset,
            ),
            "timestamp",
            "observed",
            fold["local"],
            default_timezone=cases["timezone"],
        )
        assert resolved["canonical_at"] == expected
        assert resolved["dst_resolution"] == resolution

    gap = cases["spring_forward"]["local"]
    nonexistent = normalize_time_value(
        "GlucoseReading",
        _entity("gap", timestamp=gap, source="synthetic", timezone=cases["timezone"]),
        "timestamp",
        "observed",
        gap,
        default_timezone=cases["timezone"],
    )
    assert nonexistent["normalization_status"] == "nonexistent"
    assert nonexistent["dst_resolution"] == "nonexistent_local_time"
    assert nonexistent["canonical_at"] is None


def test_duration_creates_an_explicit_effective_interval(cases):
    fixture = cases["duration"]
    rows = normalize_entity_times(
        "Treatment",
        _entity(
            "treatment",
            timestamp=fixture["start"],
            duration=fixture["minutes"],
            type="tempbasal",
            source="nightscout",
        ),
    )
    start = next(row for row in rows if row["role"] == "effective_start")
    end = next(row for row in rows if row["role"] == "effective_end")
    assert start["canonical_at"] == "2026-07-21T14:30:00.000Z"
    assert end["canonical_at"] == fixture["end"]
    assert end["duration_seconds"] == fixture["minutes"] * 60
    assert end["normalizer_version"] == NORMALIZER_VERSION


def test_dual_write_is_atomic_idempotent_and_cascades(time_database, monkeypatch):
    monkeypatch.setenv("CANONICAL_TIME_ENABLED", "true")
    row = db.create_entity(
        "FingerstickReading",
        {"timestamp": "2026-07-21T10:00:00Z", "value": 123, "source": "manual"},
    )
    repository = SqliteClinicalTimeRepository()
    initial = repository.for_entity("FingerstickReading", row["id"])
    assert len(initial) == 1
    assert initial[0]["timeline_role"] == "observed"
    assert initial[0]["recorded_at"].endswith("Z")
    assert initial[0]["received_at"].endswith("Z")

    db.update_entity("FingerstickReading", row["id"], {"timestamp": "2026-07-21T10:01:00Z"})
    updated = repository.for_entity("FingerstickReading", row["id"])
    assert len(updated) == len(initial)
    assert updated[0]["observed_at"] == "2026-07-21T10:01:00.000Z"

    assert db.delete_entity("FingerstickReading", row["id"]) is True
    assert repository.for_entity("FingerstickReading", row["id"]) == []


def test_flag_off_preserves_legacy_writes_and_backfill_is_explicit(time_database, monkeypatch):
    monkeypatch.setenv("CANONICAL_TIME_ENABLED", "false")
    row = db.create_entity(
        "GlucoseReading",
        {"timestamp": "2026-07-21T10:00:00Z", "value": 111, "source": "synthetic"},
    )
    assert SqliteClinicalTimeRepository().for_entity("GlucoseReading", row["id"]) == []
    with pytest.raises(RuntimeError, match="must be true"):
        backfill_canonical_times(time_database)

    monkeypatch.setenv("CANONICAL_TIME_ENABLED", "true")
    result = backfill_canonical_times(time_database, batch_size=1)
    assert result["entities_scanned"] == 1
    assert result["time_rows_written"] == 1


def test_entity_and_time_sidecar_roll_back_together(time_database, monkeypatch):
    monkeypatch.setenv("CANONICAL_TIME_ENABLED", "true")
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        with unit_of_work() as work:
            work.repositories.glucose.create(
                {"timestamp": "2026-07-21T10:00:00Z", "value": 111, "source": "synthetic"}
            )
            work.commit()
            raise RuntimeError("synthetic rollback")

    with sqlite3.connect(time_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM canonical_times").fetchone()[0] == 0


def test_visit_report_separates_event_time_from_ingestion_time(time_database, monkeypatch):
    monkeypatch.setenv("CANONICAL_TIME_ENABLED", "false")
    db.create_entity(
        "LabResult",
        {
            "test_name": "Synthetic marker",
            "value": 4.2,
            "unit": "units",
            "collected_date": "2026-07-20",
            "source": "medical_record",
            "category": "Synthetic",
            "owner_email": "owner@glucopilot.local",
        },
    )

    entry = report._labs()["categories"]["Synthetic"][0]
    assert entry["event_time"]["source_text"] == "2026-07-20"
    assert entry["event_time"]["precision"] == "day"
    assert entry["event_time"]["canonical_at"] is None
    assert entry["ingestion_time"]["role"] == "received"
    assert entry["ingestion_time"]["canonical_at"].endswith("Z")


def test_verified_backup_preserves_canonical_time_metadata(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database = data_dir / "app.sqlite3"
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setenv("CANONICAL_TIME_ENABLED", "true")
    db.create_entity(
        "GlucoseReading",
        {"timestamp": "2026-07-21T10:00:00Z", "value": 100, "source": "synthetic"},
    )

    backup, verification = create_verified_backup(data_dir, tmp_path / "backups", reason="time-test")
    assert verification["canonical_time_count"] == 1
    assert verify_backup(backup)["canonical_time_count"] == 1
