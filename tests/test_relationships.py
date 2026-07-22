from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from server import db
from server.backup import create_verified_backup, verify_backup
from server.data_contracts import DEPLOYMENT_OWNER_ID
from server.migrations import MigrationError, run_migrations
from server.relationship_registry import ALGORITHMS, ASSERTION_STATUSES, EVIDENCE_LEVELS, PREDICATES
from server.relationships import (
    RelationshipConflictError,
    RelationshipValidationError,
    RelationshipWrite,
    SqliteRelationshipRepository,
)
from server.repositories import LegacyRepositoryCatalog


pytestmark = pytest.mark.risk_critical
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "golden" / "relationship_graph.json"


@pytest.fixture(scope="module")
def relationship_cases() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def relationship_database(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database = data_dir / "app.sqlite3"
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    return database


def _nodes(cases: dict) -> dict[str, dict]:
    return {
        node["fixture_key"]: db.create_entity(
            node["entity_type"],
            {"owner_email": cases["owner_email"], "fixture_key": node["fixture_key"]},
        )
        for node in cases["nodes"]
    }


def _write(cases: dict, nodes: dict[str, dict], fixture_key: str, **overrides) -> RelationshipWrite:
    case = next(item for item in cases["relationships"] if item["fixture_key"] == fixture_key)
    types = {item["fixture_key"]: item["entity_type"] for item in cases["nodes"]}
    values = {
        "owner_email": cases["owner_email"],
        "subject_type": types[case["subject"]],
        "subject_id": nodes[case["subject"]]["id"],
        "predicate": case["predicate"],
        "object_type": types[case["object"]],
        "object_id": nodes[case["object"]]["id"],
        "source_id": f"urn:glucopilot:synthetic-source:{fixture_key}",
        "input_data_version": cases["input_data_version"],
        "input_hash": cases["input_hash"],
        "projection_key": fixture_key,
        "generated_at": "2026-03-02T15:00:00.000Z",
        **{key: value for key, value in case.items() if key.startswith(("valid_", "confidence_"))},
        **overrides,
    }
    return RelationshipWrite(**values)


def test_fixture_is_public_safe_and_explicitly_synthetic(relationship_cases):
    encoded = json.dumps(relationship_cases, sort_keys=True).lower()
    assert relationship_cases["synthetic"] is True
    assert relationship_cases["owner_email"] == "owner@glucopilot.local"
    assert "password" not in encoded
    assert "access_token" not in encoded
    assert "refresh_token" not in encoded
    assert all(node["fixture_key"] in {"record", "lab", "thread", "message"} for node in relationship_cases["nodes"])


def test_relationship_repository_imports_cleanly_before_repository_catalog():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from server.relationships import SqliteRelationshipRepository; "
            "from server.repositories import LegacyRepositoryCatalog",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_migration_adds_governed_registries_strict_storage_and_indexes(relationship_database):
    with sqlite3.connect(relationship_database) as connection:
        connection.row_factory = sqlite3.Row
        tables = {
            row["name"]: row
            for row in connection.execute("PRAGMA table_list")
            if row["type"] == "table"
        }
        required = {
            "entity_relationships",
            "relationship_predicate_registry",
            "assertion_status_registry",
            "evidence_level_registry",
            "relationship_algorithm_registry",
        }
        assert required <= set(tables)
        assert tables["entity_relationships"]["strict"] == 1
        assert {row[0] for row in connection.execute("SELECT status FROM assertion_status_registry")} == {
            item.name.value for item in ASSERTION_STATUSES
        }
        assert {row[0] for row in connection.execute("SELECT level FROM evidence_level_registry")} == {
            item.name.value for item in EVIDENCE_LEVELS
        }
        assert {row[0] for row in connection.execute("SELECT predicate FROM relationship_predicate_registry")} == {
            item.name for item in PREDICATES
        }
        assert {
            tuple(row)
            for row in connection.execute("SELECT algorithm_id, version FROM relationship_algorithm_registry")
        } == {(item.algorithm_id, item.version) for item in ALGORITHMS}
        indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {
            "idx_entity_relationships_subject_time",
            "idx_entity_relationships_object_time",
            "idx_entity_relationships_predicate_confidence",
            "idx_entity_relationships_projection",
        } <= indexes
        plan = " ".join(
            str(column)
            for row in connection.execute(
                """
                EXPLAIN QUERY PLAN SELECT * FROM entity_relationships
                WHERE owner_id=? AND subject_type=? AND subject_id=?
                  AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)
                """,
                (DEPLOYMENT_OWNER_ID, "LabResult", "synthetic", "2026-03-02T13:30:00.000Z", "2026-03-02T13:30:00.000Z"),
            )
            for column in row
        )
        assert "idx_entity_relationships_subject_time" in plan


def test_relationships_are_typed_attributable_versioned_owner_scoped_and_idempotent(
    relationship_cases,
    relationship_database,
):
    nodes = _nodes(relationship_cases)
    repository = SqliteRelationshipRepository(database=relationship_database)
    write = _write(relationship_cases, nodes, "lab-record")
    first = repository.add(write)
    second = repository.add(write)
    assert first.id == second.id
    assert first.owner_id == DEPLOYMENT_OWNER_ID
    assert first.owner_email == relationship_cases["owner_email"]
    assert first.assertion_status == "confirmed"
    assert first.evidence_level == "assertion_only"
    assert first.source_id == "urn:glucopilot:synthetic-source:lab-record"
    assert first.generator_id == "legacy-reference-projection"
    assert first.generator_version == "1.0.0"
    assert first.input_data_version == relationship_cases["input_data_version"]
    assert first.input_hash == relationship_cases["input_hash"]
    with sqlite3.connect(relationship_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM entity_relationships").fetchone() == (1,)

    next_input = replace(
        write,
        input_data_version="synthetic-snapshot-2026-03-02-v2",
        input_hash="sha256:" + "b" * 64,
    )
    newer = repository.add(next_input)
    assert newer.id != first.id
    with sqlite3.connect(relationship_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM entity_relationships").fetchone() == (2,)

    with pytest.raises(RelationshipConflictError, match="content changed"):
        repository.add(replace(write, source_id="urn:glucopilot:synthetic-source:changed"))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("predicate", "unknown_predicate", "unknown predicate"),
        ("assertion_status", "unknown_status", "unknown assertion status"),
        ("evidence_level", "unknown_level", "unknown evidence level"),
        ("generator_version", "9.9.9", "unknown relationship generator"),
        ("object_type", "ChatMessage", "invalid subject/object types"),
    ],
)
def test_unknown_governed_values_and_invalid_type_pairs_are_rejected(
    relationship_cases,
    relationship_database,
    field,
    value,
    message,
):
    nodes = _nodes(relationship_cases)
    repository = SqliteRelationshipRepository(database=relationship_database)
    write = replace(_write(relationship_cases, nodes, "lab-record"), **{field: value})
    with pytest.raises(RelationshipValidationError, match=message):
        repository.add(write)


def test_database_foreign_keys_reject_unknown_registry_values(
    relationship_cases,
    relationship_database,
):
    nodes = _nodes(relationship_cases)
    repository = SqliteRelationshipRepository(database=relationship_database)
    edge = repository.add(_write(relationship_cases, nodes, "lab-record"))
    with sqlite3.connect(relationship_database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE entity_relationships SET assertion_status='not-governed' WHERE id=?",
                (edge.id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE entity_relationships SET predicate='not-governed' WHERE id=?",
                (edge.id,),
            )


def test_governed_registry_drift_blocks_startup(relationship_database):
    with sqlite3.connect(relationship_database) as connection:
        connection.execute(
            "UPDATE assertion_status_registry SET description='drifted' WHERE status='confirmed'"
        )
        connection.commit()
    with pytest.raises(MigrationError, match="relationship registry drift"):
        run_migrations(relationship_database)


def test_owner_and_node_identity_are_validated(relationship_cases, relationship_database):
    nodes = _nodes(relationship_cases)
    repository = SqliteRelationshipRepository(database=relationship_database)
    write = _write(relationship_cases, nodes, "lab-record")
    with pytest.raises(RelationshipValidationError, match="outside the requested owner scope"):
        repository.add(replace(write, owner_email="other@glucopilot.local"))
    with pytest.raises(RelationshipValidationError, match="does not exist"):
        repository.add(replace(write, subject_id="synthetic-missing-node"))


def test_temporal_validity_and_confidence_are_queryable(relationship_cases, relationship_database):
    nodes = _nodes(relationship_cases)
    repository = SqliteRelationshipRepository(database=relationship_database)
    interval = repository.add(_write(relationship_cases, nodes, "lab-record"))
    point = repository.add(_write(relationship_cases, nodes, "message-thread"))
    assert repository.for_entity(
        relationship_cases["owner_email"],
        interval.subject_type,
        interval.subject_id,
        valid_at="2026-03-02T13:30:00.000Z",
        min_confidence=0.9,
    ) == [interval]
    assert repository.for_entity(
        relationship_cases["owner_email"],
        interval.subject_type,
        interval.subject_id,
        valid_at="2026-03-02T15:00:00.000Z",
    ) == []
    assert repository.for_entity(
        relationship_cases["owner_email"],
        point.subject_type,
        point.subject_id,
        valid_at="2026-03-02T13:30:00.000Z",
    ) == [point]
    assert repository.for_entity(
        relationship_cases["owner_email"],
        point.subject_type,
        point.subject_id,
        min_confidence=0.8,
    ) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"valid_time_kind": "interval", "valid_from": None, "valid_to": None},
        {"valid_time_kind": "interval", "valid_from": "2026-03-03T00:00:00Z", "valid_to": "2026-03-02T00:00:00Z"},
        {"confidence_score": float("nan")},
        {"confidence_label": "high", "confidence_score": 0.9, "confidence_method": None},
        {"input_hash": "not-a-hash"},
        {"evidence_level": "source_record", "evidence_ids": ()},
        {"evidence_level": "assertion_only", "evidence_ids": ("urn:synthetic:evidence",)},
    ],
)
def test_invalid_temporal_confidence_and_version_metadata_are_rejected(
    relationship_cases,
    relationship_database,
    changes,
):
    nodes = _nodes(relationship_cases)
    repository = SqliteRelationshipRepository(database=relationship_database)
    with pytest.raises(RelationshipValidationError):
        repository.add(replace(_write(relationship_cases, nodes, "lab-record"), **changes))


def test_compatibility_reads_remain_legacy_until_explicit_cutover(
    relationship_cases,
    relationship_database,
    monkeypatch,
):
    catalog = LegacyRepositoryCatalog()
    record = catalog.entity("MedicalRecord").create({"owner_email": relationship_cases["owner_email"]})
    lab = catalog.labs.create(
        {"owner_email": relationship_cases["owner_email"], "record_id": record["id"]}
    )
    write = RelationshipWrite(
        owner_email=relationship_cases["owner_email"],
        subject_type="LabResult",
        subject_id=lab["id"],
        predicate="extracted_from",
        object_type="MedicalRecord",
        object_id=record["id"],
        source_id="urn:glucopilot:synthetic-source:compatibility",
        input_data_version=relationship_cases["input_data_version"],
        input_hash=relationship_cases["input_hash"],
        projection_key="compatibility",
        generated_at="2026-03-02T15:00:00.000Z",
    )
    catalog.typed_relationships.add(write)
    monkeypatch.setenv("RELATIONSHIP_READS_ENABLED", "false")
    legacy = catalog.relationships.for_entity(relationship_cases["owner_email"], "LabResult", lab["id"])
    assert len(legacy) == 1
    assert legacy[0].id is None
    monkeypatch.setenv("RELATIONSHIP_READS_ENABLED", "true")
    typed = catalog.relationships.for_entity(relationship_cases["owner_email"], "LabResult", lab["id"])
    assert len(typed) == 1
    assert typed[0].id is not None


def test_verified_backup_preserves_relationship_and_registry_counts(
    relationship_cases,
    relationship_database,
    tmp_path,
):
    nodes = _nodes(relationship_cases)
    repository = SqliteRelationshipRepository(database=relationship_database)
    repository.add(_write(relationship_cases, nodes, "lab-record"))
    repository.add(_write(relationship_cases, nodes, "message-thread"))
    (relationship_database.parent / "records").mkdir()
    backup, verification = create_verified_backup(
        relationship_database.parent,
        tmp_path / "backups",
        reason="synthetic-relationship-graph",
    )
    assert verification["relationship_count"] == 2
    assert verification["relationship_predicate_count"] == len(PREDICATES)
    assert verification["assertion_status_registry_count"] == len(ASSERTION_STATUSES)
    assert verification["evidence_level_registry_count"] == len(EVIDENCE_LEVELS)
    assert verification["relationship_algorithm_registry_count"] == len(ALGORITHMS)
    assert verify_backup(backup) == verification
