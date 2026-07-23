from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from server import db
from server.backup import create_verified_backup, verify_backup
from server.data_contracts import AssertionKind, EvidenceLevel, SourceClass
from server.migrations import run_migrations
from server.relationship_projection import (
    LegacyReferenceProjector,
    ProjectionScope,
    RelationshipProjectionError,
    RelationshipProjectionRepository,
    rebuild_legacy_relationships,
)
from server.relationships import RelationshipWrite, SqliteRelationshipRepository


pytestmark = pytest.mark.risk_critical
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "golden" / "relationship_projection.json"


@pytest.fixture(scope="module")
def projection_cases() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def projection_database(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database = data_dir / "app.sqlite3"
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    return database


def _seed_graph(owner_email: str) -> dict[str, dict]:
    record = db.create_entity("MedicalRecord", {"owner_email": owner_email})
    other_record = db.create_entity("MedicalRecord", {"owner_email": owner_email})
    lab = db.create_entity(
        "LabResult",
        {"owner_email": owner_email, "record_id": record["id"], "verification_status": "approved"},
    )
    thread = db.create_entity("CompanionThread", {"owner_email": owner_email})
    message = db.create_entity(
        "ChatMessage",
        {"owner_email": owner_email, "thread_id": thread["id"]},
    )
    return {
        "record": record,
        "other_record": other_record,
        "lab": lab,
        "thread": thread,
        "message": message,
    }


def test_fixture_is_public_safe_and_declares_versioned_projector(projection_cases):
    encoded = json.dumps(projection_cases, sort_keys=True).lower()
    assert projection_cases["synthetic"] is True
    assert projection_cases["owner_email"] == "owner@glucopilot.local"
    assert projection_cases["algorithm"] == {
        "id": "legacy-reference-projection",
        "version": "1.0.0",
    }
    assert "password" not in encoded
    assert "access_token" not in encoded
    assert "refresh_token" not in encoded


def test_migration_adds_strict_run_publication_and_freshness_storage(projection_database):
    with sqlite3.connect(projection_database) as connection:
        connection.row_factory = sqlite3.Row
        tables = {row["name"]: row for row in connection.execute("PRAGMA table_list")}
        required = {
            "relationship_projection_runs",
            "relationship_projection_run_edges",
            "relationship_projection_active_edges",
            "relationship_projection_state",
        }
        assert required <= set(tables)
        assert all(tables[name]["strict"] == 1 for name in required)
        indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {
            "idx_relationship_projection_runs_status",
            "idx_relationship_projection_run_edges_scope",
            "idx_relationship_projection_active_scope",
            "idx_relationship_projection_active_relationship",
        } <= indexes
        assert tuple(connection.execute(
            "SELECT version, name FROM schema_migrations WHERE version=13"
        ).fetchone()) == (13, "relationship_projection_runs")


def test_full_rebuild_is_idempotent_and_checksum_deterministic(
    projection_cases,
    projection_database,
):
    nodes = _seed_graph(projection_cases["owner_email"])
    projector = LegacyReferenceProjector(projection_database)
    repository = RelationshipProjectionRepository(projection_database)

    first_plan = projector.plan(projection_cases["owner_email"])
    first = repository.run(first_plan)
    second_plan = projector.plan(projection_cases["owner_email"])
    second = repository.run(second_plan)

    assert first_plan.input_hash == second_plan.input_hash
    assert first.relationship_count == projection_cases["expected_full_relationship_count"]
    assert second.relationship_count == first.relationship_count
    assert second.relationship_checksum == first.relationship_checksum
    assert second.graph_checksum == first.graph_checksum
    assert second.graph_relationship_count == first.graph_relationship_count == 4
    assert second.run_id != first.run_id
    freshness = repository.freshness(projection_cases["owner_email"])
    assert freshness.graph_checksum == second.graph_checksum
    assert freshness.relationship_count == 4
    assert freshness.watermark is not None
    assert freshness.published_at is not None
    assert freshness.age_seconds is not None and freshness.age_seconds >= 0
    assert freshness.last_successful_run_id == second.run_id
    assert freshness.latest_run_status == "succeeded"

    typed = SqliteRelationshipRepository(database=projection_database)
    assert [edge.predicate for edge in typed.for_entity(
        projection_cases["owner_email"], "LabResult", nodes["lab"]["id"]
    )] == ["extracted_from"]
    with sqlite3.connect(projection_database) as connection:
        assert connection.execute("SELECT count(*) FROM relationship_projection_runs").fetchone() == (2,)
        assert connection.execute(
            "SELECT count(*) FROM relationship_projection_active_edges"
        ).fetchone() == (4,)
        assert connection.execute("SELECT count(*) FROM entity_relationships").fetchone() == (4,)


def test_failed_run_does_not_publish_partial_or_advance_freshness(
    projection_cases,
    projection_database,
):
    _seed_graph(projection_cases["owner_email"])
    projector = LegacyReferenceProjector(projection_database)
    repository = RelationshipProjectionRepository(projection_database)
    initial = repository.run(projector.plan(projection_cases["owner_email"]))
    before = repository.freshness(projection_cases["owner_email"])

    valid = projector.plan(projection_cases["owner_email"]).relationships[0]
    invalid = replace(
        valid,
        relationship=replace(
            valid.relationship,
            object_id="missing-target",
            projection_key="synthetic-invalid-after-one-valid-edge",
        ),
    )
    failed_plan = replace(
        projector.plan(projection_cases["owner_email"]),
        relationships=(valid, invalid),
    )
    with pytest.raises(RelationshipProjectionError, match="projection run .* failed"):
        repository.run(failed_plan)

    after = repository.freshness(projection_cases["owner_email"])
    assert after.graph_checksum == before.graph_checksum == initial.graph_checksum
    assert after.relationship_count == before.relationship_count == 4
    assert after.watermark == before.watermark
    assert after.published_at == before.published_at
    assert after.last_successful_run_id == initial.run_id
    assert after.latest_run_status == "failed"
    assert after.latest_run_id != initial.run_id
    with sqlite3.connect(projection_database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM relationship_projection_active_edges"
        ).fetchone() == (4,)
        assert connection.execute(
            "SELECT count(*) FROM relationship_projection_run_edges WHERE run_id=?",
            (after.latest_run_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT json_extract(error_json, '$.type') FROM relationship_projection_runs WHERE id=?",
            (after.latest_run_id,),
        ).fetchone()[0] == "RelationshipValidationError"


def test_scoped_rebuild_replaces_only_anchor_relationships(
    projection_cases,
    projection_database,
):
    nodes = _seed_graph(projection_cases["owner_email"])
    projector = LegacyReferenceProjector(projection_database)
    repository = RelationshipProjectionRepository(projection_database)
    initial = repository.run(projector.plan(projection_cases["owner_email"]))
    db.update_entity("LabResult", nodes["lab"]["id"], {"record_id": nodes["other_record"]["id"]})

    scoped = ProjectionScope.entity("LabResult", nodes["lab"]["id"])
    updated = repository.run(projector.plan(projection_cases["owner_email"], scoped))
    typed = SqliteRelationshipRepository(database=projection_database)

    assert updated.relationship_count == 2
    assert updated.graph_relationship_count == 4
    assert updated.graph_checksum != initial.graph_checksum
    assert typed.for_entity(
        projection_cases["owner_email"], "MedicalRecord", nodes["record"]["id"]
    ) == []
    assert [edge.object_id for edge in typed.for_entity(
        projection_cases["owner_email"], "LabResult", nodes["lab"]["id"]
    )] == [nodes["other_record"]["id"]]
    assert [edge.object_id for edge in typed.for_entity(
        projection_cases["owner_email"], "ChatMessage", nodes["message"]["id"]
    )] == [nodes["thread"]["id"]]
    with sqlite3.connect(projection_database) as connection:
        assert connection.execute(
            "SELECT count(DISTINCT run_id) FROM relationship_projection_active_edges"
        ).fetchone() == (2,)


def test_patient_and_clinician_assertions_survive_derived_rebuilds(
    projection_cases,
    projection_database,
):
    nodes = _seed_graph(projection_cases["owner_email"])
    typed = SqliteRelationshipRepository(database=projection_database)
    patient_authored = typed.add(
        RelationshipWrite(
            owner_email=projection_cases["owner_email"],
            subject_type="LabResult",
            subject_id=nodes["lab"]["id"],
            predicate="extracted_from",
            object_type="MedicalRecord",
            object_id=nodes["record"]["id"],
            assertion_kind=AssertionKind.PATIENT_REPORT.value,
            source_class=SourceClass.PATIENT.value,
            source_id="urn:glucopilot:synthetic-patient-assertion",
            input_data_version="synthetic-authored-v1",
            input_hash="sha256:" + "a" * 64,
            projection_key="synthetic-patient-authored-edge",
        )
    )
    clinician_authored = typed.add(
        RelationshipWrite(
            owner_email=projection_cases["owner_email"],
            subject_type="LabResult",
            subject_id=nodes["lab"]["id"],
            predicate="extracted_from",
            object_type="MedicalRecord",
            object_id=nodes["record"]["id"],
            assertion_kind=AssertionKind.CLINICIAN_CONFIRMATION.value,
            source_class=SourceClass.CLINICIAN.value,
            evidence_level=EvidenceLevel.CLINICIAN_REVIEWED.value,
            evidence_ids=("urn:glucopilot:synthetic-reviewed-evidence",),
            source_id="urn:glucopilot:synthetic-clinician-assertion",
            input_data_version="synthetic-clinician-authored-v1",
            input_hash="sha256:" + "b" * 64,
            projection_key="synthetic-clinician-authored-edge",
        )
    )
    projector = LegacyReferenceProjector(projection_database)
    repository = RelationshipProjectionRepository(projection_database)
    repository.run(projector.plan(projection_cases["owner_email"]))
    repository.run(projector.plan(projection_cases["owner_email"]))

    visible = typed.for_entity(
        projection_cases["owner_email"], "LabResult", nodes["lab"]["id"]
    )
    authored_ids = {patient_authored.id, clinician_authored.id}
    assert {edge.id for edge in visible} >= authored_ids
    with sqlite3.connect(projection_database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM entity_relationships WHERE id IN (?, ?)",
            tuple(authored_ids),
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT count(*) FROM relationship_projection_run_edges WHERE relationship_id IN (?, ?)",
            tuple(authored_ids),
        ).fetchone() == (0,)


def test_explicit_write_gate_and_verified_backup_cover_projection_runs(
    projection_cases,
    projection_database,
    tmp_path,
    monkeypatch,
):
    _seed_graph(projection_cases["owner_email"])
    monkeypatch.setenv("RELATIONSHIP_PROJECTION_WRITES_ENABLED", "false")
    with pytest.raises(RelationshipProjectionError, match="writes are disabled"):
        rebuild_legacy_relationships(projection_cases["owner_email"], database=projection_database)
    monkeypatch.setenv("RELATIONSHIP_PROJECTION_WRITES_ENABLED", "true")
    result = rebuild_legacy_relationships(projection_cases["owner_email"], database=projection_database)
    assert result.status == "succeeded"

    projection_database.parent.joinpath("records").mkdir()
    backup, verification = create_verified_backup(
        projection_database.parent,
        tmp_path / "backups",
        reason="synthetic-relationship-projection-runs",
    )
    assert verification["relationship_projection_run_count"] == 1
    assert verification["relationship_projection_run_edge_count"] == 4
    assert verification["relationship_projection_active_edge_count"] == 4
    assert verification["relationship_projection_state_count"] == 1
    assert verify_backup(backup) == verification
