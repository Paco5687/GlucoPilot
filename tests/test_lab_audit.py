from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from server import db, lab_audit
from server.backup import create_verified_backup, verify_backup
from server.migrations import run_migrations


pytestmark = pytest.mark.risk_critical
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "golden" / "clinical_edge_cases.json"


@pytest.fixture(scope="module")
def audit_case() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["auditable_lab_extraction"]


@pytest.fixture
def audit_database(tmp_path, monkeypatch):
    database = tmp_path / "app.sqlite3"
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    return database


def _record() -> dict:
    return db.create_entity(
        "MedicalRecord",
        {"filename": "synthetic-labs.pdf", "owner_email": "owner@glucopilot.local"},
    )


def test_migration_adds_audit_tables_indexes_and_immutable_history(audit_database):
    with sqlite3.connect(audit_database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        triggers = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
    assert {
        "lab_extraction_runs",
        "lab_extraction_observations",
        "lab_verification_events",
    } <= tables
    assert {
        "idx_lab_extraction_runs_record",
        "idx_lab_observations_record_status",
        "idx_lab_observations_source_key",
        "idx_lab_verification_events_observation",
    } <= indexes
    assert {
        "lab_extraction_observations_immutable_delete",
        "lab_verification_events_immutable_update",
        "lab_verification_events_immutable_delete",
    } <= triggers


def test_validation_preserves_originals_and_detects_edge_cases(audit_case):
    rows = lab_audit.normalize_and_validate(audit_case["extracted"], audit_case["record_id"])
    codes = sorted(
        {issue["code"] for row in rows for issue in row["validation_issues"]}
    )

    assert len(rows) == 8
    assert codes == audit_case["expected_issue_codes"]
    first = rows[0]
    assert first["original_name"] == "Synthetic Glucose, Serum"
    assert first["normalized_name"] == "Synthetic Glucose"
    assert first["original_value"] == "101 H"
    assert first["normalized_value"] == 101
    assert first["original_reference_range"] == "70 - 99"
    assert first["source_page"] == 2
    assert first["extraction_location"] == {"description": "Results table, row 3"}
    assert first["parser_confidence"] == 0.97
    invalid_range = next(row for row in rows if row["normalized_name"] == "Synthetic Invalid Range")
    assert invalid_range["validation_status"] == "invalid"
    assert invalid_range["reference_low"] is None
    assert invalid_range["reference_high"] is None


def test_titer_detection_handles_adversarial_whitespace_without_regex_backtracking():
    extracted = {
        "record_date": "2026-05-11",
        "lab_results": [
            {
                "test_name": "Synthetic ANA Titer",
                "original_value": "  1  :  160  ",
                "source_page": 1,
            },
            {
                "test_name": "Synthetic malformed titer",
                "original_value": (" " * 50_000) + "1 : 160" + (" " * 50_000) + "x",
                "source_page": 2,
            },
        ],
    }

    rows = lab_audit.normalize_and_validate(extracted, "synthetic-titer-record")

    assert rows[0]["value_kind"] == "titer"
    assert rows[0]["normalized_value"] == 160
    assert rows[1]["value_kind"] == "qualitative"
    assert rows[1]["normalized_value"] is None


def test_review_history_supersession_and_reprocess_preserve_correction(
    audit_case,
    audit_database,
):
    record = _record()
    rows = lab_audit.normalize_and_validate(audit_case["extracted"], record["id"])
    run_id = lab_audit.start_run(record["id"], audit_case["source_hash"], 5)
    projected, preserved = lab_audit.replace_unverified_with_run(run_id, record["id"], rows)

    assert preserved == 0
    assert len(projected) == 5
    review_data = lab_audit.record_extractions(record["id"])
    assert len(review_data["observations"]) == 8
    assert {row["value_kind"] for row in review_data["observations"]} == {
        "numeric", "qualitative", "titer"
    }

    glucose = next(row for row in projected if row["test_name"] == "Synthetic Glucose")
    approved = lab_audit.review(record["id"], glucose["id"], "approve")
    assert approved["lab"]["verification_status"] == "approved"
    edited = lab_audit.review(
        record["id"],
        glucose["id"],
        "edit",
        {"value": 99, "reference_high": 100, "flag": "normal"},
        "Synthetic fixture correction",
    )
    assert edited["lab"]["id"] == glucose["id"]
    assert edited["lab"]["value"] == 99
    assert edited["lab"]["verification_status"] == "edited"
    current_observation_id = edited["lab"]["extraction_observation_id"]

    with sqlite3.connect(audit_database) as connection:
        old = connection.execute(
            "SELECT verification_status, superseded_by_observation_id FROM lab_extraction_observations WHERE id=?",
            (approved["observation"]["id"],),
        ).fetchone()
        assert old == ("superseded", current_observation_id)
        event = connection.execute(
            "SELECT id FROM lab_verification_events WHERE observation_id=? AND action='edit'",
            (current_observation_id,),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE lab_verification_events SET reason='changed' WHERE id=?", (event[0],)
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM lab_verification_events WHERE id=?", (event[0],))

    second_run = lab_audit.start_run(record["id"], audit_case["source_hash"], 5)
    _, preserved = lab_audit.replace_unverified_with_run(second_run, record["id"], rows)
    assert preserved >= 1
    stored = db.query_entities("LabResult", {"id": glucose["id"]}, limit=1)[0]
    assert stored["value"] == 99
    assert stored["verification_status"] == "edited"
    assert stored["extraction_observation_id"] == current_observation_id

    for lab in db.query_entities("LabResult", {"record_id": record["id"]}):
        db.delete_entity("LabResult", lab["id"])
    assert db.delete_entity("MedicalRecord", record["id"]) is True
    with sqlite3.connect(audit_database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM lab_extraction_runs WHERE record_entity_id=?", (record["id"],)
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM lab_verification_events WHERE record_entity_id=?", (record["id"],)
        ).fetchone() == (0,)


def test_invalid_and_rejected_results_are_not_summary_eligible(audit_case, audit_database):
    record = _record()
    rows = lab_audit.normalize_and_validate(audit_case["extracted"], record["id"])
    run_id = lab_audit.start_run(record["id"], audit_case["source_hash"], 5)
    projected, _ = lab_audit.replace_unverified_with_run(run_id, record["id"], rows)

    invalid = next(row for row in projected if row["test_name"] == "Synthetic Invalid Range")
    assert lab_audit.summary_eligible(invalid) is False
    valid = next(row for row in projected if row["test_name"] == "Synthetic Glucose")
    rejected = lab_audit.review(record["id"], valid["id"], "reject")["lab"]
    assert lab_audit.summary_eligible(rejected) is False


def test_failed_run_does_not_persist_provider_or_source_error_text(audit_case, audit_database):
    record = _record()
    run_id = lab_audit.start_run(record["id"], audit_case["source_hash"], 1)

    lab_audit.fail_run(run_id, RuntimeError("Bearer synthetic-secret patient source text"))

    with sqlite3.connect(audit_database) as connection:
        row = connection.execute(
            "SELECT status, error_summary FROM lab_extraction_runs WHERE id=?", (run_id,)
        ).fetchone()
    assert row == ("failed", "RuntimeError: extraction failed")


def test_qualitative_extraction_can_be_approved_without_numeric_projection(
    audit_case,
    audit_database,
):
    record = _record()
    rows = lab_audit.normalize_and_validate(audit_case["extracted"], record["id"])
    run_id = lab_audit.start_run(record["id"], audit_case["source_hash"], 5)
    lab_audit.replace_unverified_with_run(run_id, record["id"], rows)
    observation = next(
        row for row in lab_audit.record_extractions(record["id"])["observations"]
        if row["value_kind"] == "qualitative"
    )

    result = lab_audit.review_observation(record["id"], observation["id"], "approve")

    assert result["lab"] is None
    assert result["observation"]["verification_status"] == "approved"
    second_run = lab_audit.start_run(record["id"], audit_case["source_hash"], 5)
    _, preserved = lab_audit.replace_unverified_with_run(second_run, record["id"], rows)
    current = lab_audit.record_extractions(record["id"])["observations"]
    qualitative = [row for row in current if row["value_kind"] == "qualitative"]
    assert preserved == 1
    assert len(qualitative) == 1
    assert qualitative[0]["verification_status"] == "approved"


def test_verified_backup_accounts_for_lab_audit_history(audit_case, audit_database):
    record = _record()
    rows = lab_audit.normalize_and_validate(audit_case["extracted"], record["id"])
    run_id = lab_audit.start_run(record["id"], audit_case["source_hash"], 5)
    lab_audit.replace_unverified_with_run(run_id, record["id"], rows)
    observation = lab_audit.record_extractions(record["id"])["observations"][0]
    lab_audit.review_observation(record["id"], observation["id"], "approve")

    backup, verification = create_verified_backup(
        audit_database.parent,
        audit_database.parent / "backups",
        reason="synthetic lab audit",
    )

    assert verification["lab_extraction_run_count"] == 1
    assert verification["lab_extraction_observation_count"] == 8
    assert verification["lab_verified_observation_count"] == 1
    assert verification["lab_verification_event_count"] == 1
    assert verify_backup(backup) == verification
