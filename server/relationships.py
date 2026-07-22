"""Strict, owner-scoped repository for rebuildable relationship projections."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from . import db
from .data_contracts import (
    DEPLOYMENT_OWNER_ID,
    AssertionKind,
    AssertionStatus,
    ConfidenceLabel,
    EffectiveTimeKind,
    EvidenceLevel,
    SourceClass,
)
if TYPE_CHECKING:
    from .repositories import RelationshipEdge, RelationshipRepository


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_TRUE = {"1", "true", "yes", "on"}


class RelationshipValidationError(ValueError):
    """Raised when a relationship violates governed graph contracts."""


class RelationshipConflictError(RuntimeError):
    """Raised when an immutable projection identity is reused with new content."""


@dataclass(frozen=True)
class RelationshipWrite:
    owner_email: str
    subject_type: str
    subject_id: str
    predicate: str
    object_type: str
    object_id: str
    source_id: str
    input_data_version: str
    input_hash: str
    projection_key: str
    assertion_kind: str = AssertionKind.SOURCE_FACT.value
    assertion_status: str = AssertionStatus.CONFIRMED.value
    evidence_level: str = EvidenceLevel.ASSERTION_ONLY.value
    evidence_ids: tuple[str, ...] = ()
    source_class: str = SourceClass.SYSTEM.value
    generator_id: str = "legacy-reference-projection"
    generator_version: str = "1.0.0"
    valid_time_kind: str = EffectiveTimeKind.UNKNOWN.value
    valid_from: str | None = None
    valid_to: str | None = None
    confidence_label: str = ConfidenceLabel.NOT_ASSESSED.value
    confidence_score: float | None = None
    confidence_method: str | None = None
    confidence_calibration_version: str | None = None
    generated_at: str | None = None


def relationship_reads_enabled() -> bool:
    return os.getenv("RELATIONSHIP_READS_ENABLED", "false").strip().lower() in _TRUE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _instant(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RelationshipValidationError(f"{field} must be a canonical UTC instant")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RelationshipValidationError(f"{field} must be a canonical UTC instant") from error
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise RelationshipValidationError(f"{field} must be normalized to UTC")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _required(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise RelationshipValidationError(f"{field} is required")
    if len(normalized) > 500:
        raise RelationshipValidationError(f"{field} is too long")
    return normalized


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _relationship_id(write: RelationshipWrite) -> str:
    identity = (
        DEPLOYMENT_OWNER_ID,
        write.generator_id,
        write.generator_version,
        write.input_data_version,
        write.projection_key,
    )
    digest = hashlib.sha256(_canonical(identity).encode()).hexdigest()
    return f"urn:glucopilot:relationship:{digest}"


def _edge(row: sqlite3.Row) -> RelationshipEdge:
    from .repositories import RelationshipEdge

    return RelationshipEdge(
        row["subject_type"],
        row["subject_id"],
        row["predicate"],
        row["object_type"],
        row["object_id"],
        id=row["id"],
        owner_id=row["owner_id"],
        owner_email=row["owner_email"],
        assertion_kind=row["assertion_kind"],
        assertion_status=row["assertion_status"],
        evidence_level=row["evidence_level"],
        evidence_ids=tuple(json.loads(row["evidence_ids_json"])),
        source_class=row["source_class"],
        source_id=row["source_id"],
        generator_id=row["generator_id"],
        generator_version=row["generator_version"],
        input_data_version=row["input_data_version"],
        input_hash=row["input_hash"],
        projection_key=row["projection_key"],
        valid_time_kind=row["valid_time_kind"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        confidence_label=row["confidence_label"],
        confidence_score=row["confidence_score"],
        confidence_method=row["confidence_method"],
        confidence_calibration_version=row["confidence_calibration_version"],
        generated_at=row["generated_at"],
        created_at=row["created_at"],
    )


class SqliteRelationshipRepository:
    """Append-only graph projection with governed registry validation."""

    def __init__(
        self,
        connection: sqlite3.Connection | None = None,
        *,
        database=None,
    ) -> None:
        self._connection = connection
        self._database = database

    @contextmanager
    def _scope(self) -> Iterator[tuple[sqlite3.Connection, bool]]:
        if self._connection is not None:
            yield self._connection, False
            return
        connection = db.connect() if self._database is None else sqlite3.connect(self._database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection, True
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _validate_node(
        connection: sqlite3.Connection,
        owner_email: str,
        entity_type: str,
        entity_id: str,
        role: str,
    ) -> None:
        row = connection.execute(
            "SELECT data FROM entities WHERE id=? AND type=?",
            (entity_id, entity_type),
        ).fetchone()
        if row is None:
            raise RelationshipValidationError(f"{role} entity does not exist with the registered type")
        try:
            data = json.loads(row["data"])
        except (TypeError, ValueError) as error:
            raise RelationshipValidationError(f"{role} entity payload is invalid") from error
        if data.get("owner_email") != owner_email:
            raise RelationshipValidationError(f"{role} entity is outside the requested owner scope")

    @staticmethod
    def _validated_row(connection: sqlite3.Connection, write: RelationshipWrite) -> dict[str, object]:
        values = {
            field: _required(getattr(write, field), field)
            for field in (
                "owner_email",
                "subject_type",
                "subject_id",
                "predicate",
                "object_type",
                "object_id",
                "source_id",
                "generator_id",
                "generator_version",
                "input_data_version",
                "projection_key",
            )
        }
        if not _SHA256.fullmatch(str(write.input_hash or "")):
            raise RelationshipValidationError("input_hash must be a lowercase sha256 digest")

        try:
            assertion_kind = AssertionKind(write.assertion_kind).value
            source_class = SourceClass(write.source_class).value
            time_kind = EffectiveTimeKind(write.valid_time_kind).value
            confidence_label = ConfidenceLabel(write.confidence_label).value
        except ValueError as error:
            raise RelationshipValidationError(str(error)) from error

        status = connection.execute(
            "SELECT status FROM assertion_status_registry WHERE status=?",
            (write.assertion_status,),
        ).fetchone()
        if status is None:
            raise RelationshipValidationError("unknown assertion status")
        evidence = connection.execute(
            "SELECT level, requires_evidence, clinician_reviewed FROM evidence_level_registry WHERE level=?",
            (write.evidence_level,),
        ).fetchone()
        if evidence is None:
            raise RelationshipValidationError("unknown evidence level")
        evidence_ids = tuple(_required(item, "evidence_id") for item in write.evidence_ids)
        if len(set(evidence_ids)) != len(evidence_ids):
            raise RelationshipValidationError("evidence IDs must be unique")
        if bool(evidence_ids) != bool(evidence["requires_evidence"]):
            raise RelationshipValidationError("evidence level and evidence IDs are inconsistent")
        predicate = connection.execute(
            """
            SELECT predicate FROM relationship_predicate_registry
            WHERE predicate=? AND subject_type=? AND object_type=?
            """,
            (write.predicate, write.subject_type, write.object_type),
        ).fetchone()
        if predicate is None:
            raise RelationshipValidationError("unknown predicate or invalid subject/object types")
        algorithm = connection.execute(
            """
            SELECT deterministic, rebuildable FROM relationship_algorithm_registry
            WHERE algorithm_id=? AND version=? AND output_kind='relationship'
            """,
            (write.generator_id, write.generator_version),
        ).fetchone()
        if algorithm is None:
            raise RelationshipValidationError("unknown relationship generator/version")
        if not algorithm["deterministic"] or not algorithm["rebuildable"]:
            raise RelationshipValidationError("relationship generator must be deterministic and rebuildable")

        if assertion_kind == AssertionKind.SOURCE_FACT.value:
            if source_class in {SourceClass.ALGORITHM.value, SourceClass.PATIENT.value}:
                raise RelationshipValidationError("source fact has an invalid source class")
            if write.evidence_level == EvidenceLevel.NONE.value:
                raise RelationshipValidationError("source fact requires evidence attribution")
        if assertion_kind == AssertionKind.PATIENT_REPORT.value and source_class != SourceClass.PATIENT.value:
            raise RelationshipValidationError("patient report requires patient source class")
        if assertion_kind == AssertionKind.CLINICIAN_CONFIRMATION.value:
            if source_class != SourceClass.CLINICIAN.value:
                raise RelationshipValidationError("clinician confirmation requires clinician source class")
            if not evidence["clinician_reviewed"]:
                raise RelationshipValidationError("clinician confirmation requires clinician-reviewed evidence")
            if write.assertion_status in {
                AssertionStatus.UNVERIFIED.value,
                AssertionStatus.PROVISIONAL.value,
            }:
                raise RelationshipValidationError("clinician confirmation cannot be tentative")

        valid_from = _instant(write.valid_from, "valid_from")
        valid_to = _instant(write.valid_to, "valid_to")
        expected = {
            EffectiveTimeKind.UNKNOWN.value: (False, False),
            EffectiveTimeKind.POINT.value: (True, False),
            EffectiveTimeKind.OPEN_ENDED.value: (True, False),
            EffectiveTimeKind.INTERVAL.value: (True, True),
        }[time_kind]
        if (valid_from is not None, valid_to is not None) != expected:
            raise RelationshipValidationError("temporal boundaries do not match valid_time_kind")
        if valid_to is not None and valid_to < valid_from:
            raise RelationshipValidationError("valid_to cannot precede valid_from")

        score = write.confidence_score
        if score is not None and (not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1):
            raise RelationshipValidationError("confidence_score must be finite and between 0 and 1")
        method = str(write.confidence_method or "").strip() or None
        calibration = str(write.confidence_calibration_version or "").strip() or None
        if confidence_label == ConfidenceLabel.NOT_ASSESSED.value:
            if score is not None or method is not None or calibration is not None:
                raise RelationshipValidationError("unassessed confidence cannot include assessment metadata")
        elif method is None:
            raise RelationshipValidationError("assessed confidence requires a method")

        generated_at = _instant(write.generated_at or _now(), "generated_at")
        owner_email = str(values["owner_email"])
        SqliteRelationshipRepository._validate_node(
            connection, owner_email, str(values["subject_type"]), str(values["subject_id"]), "subject"
        )
        SqliteRelationshipRepository._validate_node(
            connection, owner_email, str(values["object_type"]), str(values["object_id"]), "object"
        )
        created_at = _now()
        return {
            "id": _relationship_id(write),
            "owner_id": DEPLOYMENT_OWNER_ID,
            **values,
            "assertion_kind": assertion_kind,
            "assertion_status": write.assertion_status,
            "evidence_level": write.evidence_level,
            "evidence_ids_json": _canonical(evidence_ids),
            "source_class": source_class,
            "input_hash": write.input_hash,
            "valid_time_kind": time_kind,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "confidence_label": confidence_label,
            "confidence_score": score,
            "confidence_method": method,
            "confidence_calibration_version": calibration,
            "generated_at": generated_at,
            "created_at": created_at,
        }

    def add(self, write: RelationshipWrite) -> RelationshipEdge:
        with self._scope() as (connection, _):
            row = self._validated_row(connection, write)
            columns = tuple(row)
            placeholders = ",".join("?" for _ in columns)
            connection.execute(
                f"INSERT OR IGNORE INTO entity_relationships ({','.join(columns)}) VALUES ({placeholders})",
                tuple(row[column] for column in columns),
            )
            stored = connection.execute(
                "SELECT * FROM entity_relationships WHERE id=?",
                (row["id"],),
            ).fetchone()
            if stored is None:
                raise RelationshipConflictError("projection identity conflicts with an existing relationship")
            ignored = {"created_at", "generated_at"}
            if any(stored[key] != value for key, value in row.items() if key not in ignored):
                raise RelationshipConflictError("immutable relationship projection content changed")
            return _edge(stored)

    def for_entity(
        self,
        owner_email: str,
        entity_type: str,
        entity_id: str,
        *,
        predicate: str | None = None,
        valid_at: str | None = None,
        min_confidence: float | None = None,
        limit: int | None = None,
    ) -> list[RelationshipEdge]:
        owner_email = _required(owner_email, "owner_email")
        entity_type = _required(entity_type, "entity_type")
        entity_id = _required(entity_id, "entity_id")
        where = ["owner_id=?", "owner_email=?", "subject_type=?", "subject_id=?"]
        parameters: list[object] = [DEPLOYMENT_OWNER_ID, owner_email, entity_type, entity_id]
        if predicate is not None:
            where.append("predicate=?")
            parameters.append(_required(predicate, "predicate"))
        if valid_at is not None:
            instant = _instant(valid_at, "valid_at")
            where.append(
                "((valid_time_kind='point' AND valid_from=?) OR "
                "(valid_time_kind='open_ended' AND valid_from<=?) OR "
                "(valid_time_kind='interval' AND valid_from<=? AND valid_to>=?))"
            )
            parameters.extend((instant, instant, instant, instant))
        if min_confidence is not None:
            if not math.isfinite(min_confidence) or not 0 <= min_confidence <= 1:
                raise RelationshipValidationError("min_confidence must be between 0 and 1")
            where.append("confidence_score>=?")
            parameters.append(min_confidence)
        limit_clause = ""
        if limit is not None:
            if not isinstance(limit, int) or not 1 <= limit <= 1001:
                raise RelationshipValidationError("limit must be between 1 and 1001")
            limit_clause = " LIMIT ?"
            parameters.append(limit)
        with self._scope() as (connection, _):
            rows = connection.execute(
                "SELECT edge.* FROM entity_relationships edge WHERE "
                + " AND ".join(where)
                + " AND ("
                + "NOT EXISTS (SELECT 1 FROM relationship_projection_run_edges managed "
                + "WHERE managed.relationship_id=edge.id) OR "
                + "EXISTS (SELECT 1 FROM relationship_projection_active_edges active "
                + "WHERE active.relationship_id=edge.id))"
                + " ORDER BY predicate, object_type, object_id, id"
                + limit_clause,
                parameters,
            ).fetchall()
        return [_edge(row) for row in rows]

    def reverse_for_entity(
        self,
        owner_email: str,
        entity_type: str,
        entity_id: str,
        *,
        predicate: str | None = None,
        limit: int | None = None,
    ) -> list[RelationshipEdge]:
        """Return visible incoming edges in deterministic order."""
        owner_email = _required(owner_email, "owner_email")
        entity_type = _required(entity_type, "entity_type")
        entity_id = _required(entity_id, "entity_id")
        where = ["owner_id=?", "owner_email=?", "object_type=?", "object_id=?"]
        parameters: list[object] = [DEPLOYMENT_OWNER_ID, owner_email, entity_type, entity_id]
        if predicate is not None:
            where.append("predicate=?")
            parameters.append(_required(predicate, "predicate"))
        limit_clause = ""
        if limit is not None:
            if not isinstance(limit, int) or not 1 <= limit <= 1001:
                raise RelationshipValidationError("limit must be between 1 and 1001")
            limit_clause = " LIMIT ?"
            parameters.append(limit)
        with self._scope() as (connection, _):
            rows = connection.execute(
                "SELECT edge.* FROM entity_relationships edge WHERE "
                + " AND ".join(where)
                + " AND ("
                + "NOT EXISTS (SELECT 1 FROM relationship_projection_run_edges managed "
                + "WHERE managed.relationship_id=edge.id) OR "
                + "EXISTS (SELECT 1 FROM relationship_projection_active_edges active "
                + "WHERE active.relationship_id=edge.id))"
                + " ORDER BY predicate, subject_type, subject_id, id"
                + limit_clause,
                parameters,
            ).fetchall()
        return [_edge(row) for row in rows]


class RelationshipCompatibilityRepository:
    """Keep legacy relationship reads until the explicit graph cutover."""

    def __init__(
        self,
        legacy: RelationshipRepository,
        typed: SqliteRelationshipRepository,
    ) -> None:
        self.legacy = legacy
        self.typed = typed

    def for_entity(
        self,
        owner_email: str,
        entity_type: str,
        entity_id: str,
    ) -> list[RelationshipEdge]:
        if relationship_reads_enabled():
            return self.typed.for_entity(owner_email, entity_type, entity_id)
        return self.legacy.for_entity(owner_email, entity_type, entity_id)
