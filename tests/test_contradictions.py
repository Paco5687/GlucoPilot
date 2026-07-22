from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from server import contradictions, db
from server.backup import create_verified_backup, verify_backup
from server.migrations import run_migrations


pytestmark = pytest.mark.risk_critical
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "golden" / "contradictions.json"


@pytest.fixture(scope="module")
def contradiction_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def contradiction_database(tmp_path, monkeypatch):
    database = tmp_path / "app.sqlite3"
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    return database


def test_fixture_is_explicitly_synthetic_and_secret_free(contradiction_fixture):
    encoded = json.dumps(contradiction_fixture, sort_keys=True).lower()
    assert contradiction_fixture["synthetic"] is True
    assert contradiction_fixture["subject"]["id"].startswith("synthetic-")
    assert contradiction_fixture["subject"]["owner_email"] == "owner@glucopilot.local"
    assert "password" not in encoded
    assert "access_token" not in encoded
    assert "refresh_token" not in encoded


def test_rules_are_deterministic_and_preserve_both_sides(contradiction_fixture):
    snapshot = contradiction_fixture["snapshot"]
    detections, input_version = contradictions.evaluate_snapshot(snapshot)
    reversed_snapshot = deepcopy(snapshot)
    for key in ("fingersticks", "lab_observations", "labs", "period_logs", "source_records"):
        reversed_snapshot[key].reverse()
    reversed_snapshot["tdd_reconciliation"]["days"].reverse()
    for day in reversed_snapshot["tdd_reconciliation"]["days"]:
        day["pump_reported"]["candidates"].reverse()
    reversed_detections, reversed_version = contradictions.evaluate_snapshot(reversed_snapshot)

    assert [row["rule_id"] for row in detections] == contradiction_fixture["expected_rule_ids"]
    assert detections == reversed_detections
    assert input_version == reversed_version
    assert input_version.startswith("sha256:")
    assert all(row["left"] and row["right"] for row in detections)
    assert all(row["detection_key"].startswith("sha256:") for row in detections)

    glucose = next(row for row in detections if row["rule_id"] == "glucose.cgm_vs_fingerstick")
    assert glucose["severity"] == "blocking"
    assert glucose["left"]["value"] == 100
    assert glucose["right"]["value"] == 150
    assert glucose["context"]["threshold_is_review_signal_not_device_accuracy_claim"] is True


def test_hormone_rule_requires_an_explicit_source_timing_declaration(contradiction_fixture):
    snapshot = deepcopy(contradiction_fixture["snapshot"])
    snapshot["labs"][0].pop("expected_cycle_phase")

    detections, _ = contradictions.evaluate_snapshot(snapshot)

    assert "labs.hormone_cycle_phase_timing" not in {row["rule_id"] for row in detections}


def test_provider_role_cannot_mutate_a_contradiction():
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/contradictions/contr_synthetic/resolve",
        "headers": [],
        "session": {"logged_in": True, "role": "provider", "provider_name": "Synthetic Provider"},
    })

    with pytest.raises(HTTPException) as raised:
        contradictions.resolve_contradiction(
            "contr_synthetic",
            contradictions.ResolveBody(
                resolution_kind="accepted_left",
                note="Synthetic provider must not be able to save this.",
            ),
            request,
        )

    assert raised.value.status_code == 403


def test_migration_adds_ledger_indexes_and_immutable_history(contradiction_database):
    with sqlite3.connect(contradiction_database) as connection:
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
    assert {"contradiction_runs", "contradictions", "contradiction_events"} <= tables
    assert {
        "idx_contradictions_resolution",
        "idx_contradictions_subject",
        "idx_contradiction_events_record",
    } <= indexes
    assert {
        "contradictions_immutable_delete",
        "contradiction_events_immutable_update",
        "contradiction_events_immutable_delete",
    } <= triggers


def test_blocking_resolution_is_explicit_attributed_and_never_silently_reopened(
    contradiction_fixture,
    contradiction_database,
):
    detections, input_version = contradictions.evaluate_snapshot(contradiction_fixture["snapshot"])
    repository = contradictions.SqliteContradictionRepository()
    first_run = repository.reconcile(detections, input_version)
    assert first_run["created"] == len(detections)
    assert first_run["detection_count"] == len(detections)

    rows = repository.list()
    blocking = next(row for row in rows if row["severity"] == "blocking")
    with pytest.raises(contradictions.ContradictionError, match="explicit resolution note"):
        repository.resolve(
            blocking["id"],
            "data_corrected",
            "",
            {"id": "owner", "role": "admin", "name": "Synthetic Admin"},
        )

    resolved = repository.resolve(
        blocking["id"],
        "accepted_left",
        "Synthetic review confirmed the meter source.",
        {"id": "owner", "role": "admin", "name": "Synthetic Admin"},
    )
    assert resolved["resolution_state"] == "resolved"
    assert resolved["resolved_by"] == "owner"

    second_run = repository.reconcile(detections, input_version)
    assert second_run["created"] == 0
    all_rows = repository.list(include_resolved=True)
    stored = next(row for row in all_rows if row["id"] == blocking["id"])
    assert stored["resolution_state"] == "resolved"
    assert [event["action"] for event in stored["history"]] == ["detected", "resolved"]
    assert stored["history"][-1]["actor_name"] == "Synthetic Admin"

    repository.reconcile([], contradictions._hash({"synthetic": "empty"}))
    all_rows = repository.list(include_resolved=True)
    stored = next(row for row in all_rows if row["id"] == blocking["id"])
    assert stored["resolution_state"] == "resolved"
    assert stored["detection_state"] == "not_current"
    unresolved_blocking = [
        row for row in repository.list() if row["severity"] == "blocking"
    ]
    assert unresolved_blocking
    assert all(row["resolution_state"] == "unresolved" for row in unresolved_blocking)
    assert all(row["detection_state"] == "not_current" for row in unresolved_blocking)

    reopened = repository.reopen(
        blocking["id"],
        "Synthetic evidence review reopened the item.",
        {"id": "owner", "role": "admin", "name": "Synthetic Admin"},
    )
    assert reopened["resolution_state"] == "unresolved"
    events = next(
        row["history"]
        for row in repository.list(include_resolved=True)
        if row["id"] == blocking["id"]
    )
    assert any(event["action"] == "reopened" for event in events)


def test_changed_evidence_creates_a_new_unresolved_fingerprint(
    contradiction_fixture,
    contradiction_database,
):
    repository = contradictions.SqliteContradictionRepository()
    detections, input_version = contradictions.evaluate_snapshot(contradiction_fixture["snapshot"])
    repository.reconcile(detections, input_version)
    original = next(
        row for row in repository.list() if row["rule_id"] == "glucose.cgm_vs_fingerstick"
    )
    repository.resolve(
        original["id"],
        "accepted_left",
        "Synthetic resolution.",
        {"id": "owner", "role": "admin", "name": "Synthetic Admin"},
    )

    changed = deepcopy(contradiction_fixture["snapshot"])
    changed["fingersticks"][0]["cgm_value"] = 160
    changed_detections, changed_version = contradictions.evaluate_snapshot(changed)
    repository.reconcile(changed_detections, changed_version)

    glucose_rows = [
        row
        for row in repository.list(include_resolved=True)
        if row["rule_id"] == "glucose.cgm_vs_fingerstick"
    ]
    assert len(glucose_rows) == 2
    assert {row["resolution_state"] for row in glucose_rows} == {"resolved", "unresolved"}
    assert len({row["detection_key"] for row in glucose_rows}) == 2


def test_ledger_rows_and_events_cannot_be_deleted_or_rewritten(
    contradiction_fixture,
    contradiction_database,
):
    repository = contradictions.SqliteContradictionRepository()
    detections, input_version = contradictions.evaluate_snapshot(contradiction_fixture["snapshot"])
    repository.reconcile(detections, input_version)
    row = repository.list()[0]
    event_id = row["history"][0]["id"]

    with sqlite3.connect(contradiction_database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM contradictions WHERE id=?", (row["id"],))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE contradiction_events SET reason='changed' WHERE id=?",
                (event_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM contradiction_events WHERE id=?", (event_id,))


def test_verified_backup_preserves_contradiction_counts(
    contradiction_fixture,
    contradiction_database,
    tmp_path,
):
    detections, input_version = contradictions.evaluate_snapshot(contradiction_fixture["snapshot"])
    contradictions.SqliteContradictionRepository().reconcile(detections, input_version)
    data_dir = contradiction_database.parent
    (data_dir / "records").mkdir()
    backup_root = tmp_path / "backups"

    backup_path, created = create_verified_backup(
        data_dir,
        backup_root,
        reason="synthetic-contradiction-ledger",
    )
    verified = verify_backup(backup_path)

    assert created["contradiction_run_count"] == 1
    assert created["contradiction_count"] == len(detections)
    assert created["unresolved_contradiction_count"] == len(detections)
    assert created["unresolved_blocking_contradiction_count"] >= 1
    assert created["contradiction_event_count"] == len(detections)
    assert verified == created
