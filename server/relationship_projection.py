"""Deterministic, transactional jobs for the rebuildable relationship graph."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import db
from .data_contracts import DEPLOYMENT_OWNER_ID
from .relationships import RelationshipWrite, SqliteRelationshipRepository


_TRUE = {"1", "true", "yes", "on"}
_ALGORITHM_ID = "legacy-reference-projection"
_ALGORITHM_VERSION = "1.0.0"
MAX_RELATIONSHIPS_PER_RUN = 200_000
_SOURCE_RELATIONSHIPS = {
    "LabResult": ("record_id", "extracted_from", "MedicalRecord", "has_lab_result"),
    "ChatMessage": ("thread_id", "member_of_thread", "CompanionThread", "has_message"),
}


class RelationshipProjectionError(RuntimeError):
    """Raised when a graph build cannot be safely published."""


def relationship_projection_writes_enabled() -> bool:
    return os.getenv("RELATIONSHIP_PROJECTION_WRITES_ENABLED", "false").strip().lower() in _TRUE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _checksum(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _required(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise RelationshipProjectionError(f"{field} is required")
    if len(normalized) > 500:
        raise RelationshipProjectionError(f"{field} is too long")
    return normalized


@dataclass(frozen=True)
class ProjectionScope:
    kind: str
    entity_type: str | None = None
    entity_id: str | None = None

    @classmethod
    def full(cls) -> ProjectionScope:
        return cls("full")

    @classmethod
    def entity(cls, entity_type: str, entity_id: str) -> ProjectionScope:
        return cls("entity", _required(entity_type, "entity_type"), _required(entity_id, "entity_id"))

    def definition(self) -> dict[str, str]:
        if self.kind == "full" and self.entity_type is None and self.entity_id is None:
            return {"kind": "full"}
        if self.kind == "entity" and self.entity_type and self.entity_id:
            return {"entity_id": self.entity_id, "entity_type": self.entity_type, "kind": "entity"}
        raise RelationshipProjectionError("invalid projection scope")

    @property
    def checksum(self) -> str:
        return _checksum(self.definition())

    @property
    def scope_key(self) -> str | None:
        if self.kind == "full":
            return None
        return f"{self.entity_type}:{self.entity_id}"


@dataclass(frozen=True)
class ScopedRelationshipWrite:
    scope_key: str
    relationship: RelationshipWrite


@dataclass(frozen=True)
class ProjectionPlan:
    owner_email: str
    generator_id: str
    generator_version: str
    scope: ProjectionScope
    input_data_version: str
    input_hash: str
    watermark: str | None
    relationships: tuple[ScopedRelationshipWrite, ...]


@dataclass(frozen=True)
class ProjectionRunResult:
    run_id: str
    status: str
    scope: ProjectionScope
    relationship_count: int
    relationship_checksum: str
    graph_relationship_count: int
    graph_checksum: str
    watermark: str | None
    published_at: str


@dataclass(frozen=True)
class ProjectionFreshness:
    owner_email: str
    generator_id: str
    generator_version: str
    graph_checksum: str | None
    relationship_count: int
    watermark: str | None
    published_at: str | None
    age_seconds: float | None
    last_successful_run_id: str | None
    latest_run_id: str | None
    latest_run_status: str | None
    latest_run_started_at: str | None
    latest_run_completed_at: str | None


class LegacyReferenceProjector:
    """Project the four governed inverse edges from authoritative entity references."""

    generator_id = _ALGORITHM_ID
    generator_version = _ALGORITHM_VERSION

    def __init__(self, database: Path | str | None = None) -> None:
        self._database = Path(database) if database is not None else None

    def _connect(self) -> sqlite3.Connection:
        if self._database is None:
            return db.connect()
        connection = sqlite3.connect(self._database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def plan(self, owner_email: str, scope: ProjectionScope | None = None) -> ProjectionPlan:
        owner_email = _required(owner_email, "owner_email")
        scope = scope or ProjectionScope.full()
        scope.definition()
        if scope.kind == "entity" and scope.entity_type not in _SOURCE_RELATIONSHIPS:
            raise RelationshipProjectionError(
                "entity-scoped relationship rebuilds require LabResult or ChatMessage anchors"
            )

        conditions = ["type IN ('LabResult', 'ChatMessage')", "json_extract(data, '$.owner_email')=?"]
        parameters: list[object] = [owner_email]
        if scope.kind == "entity":
            conditions.extend(("type=?", "id=?"))
            parameters.extend((scope.entity_type, scope.entity_id))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, type, data, updated_date FROM entities WHERE "
                + " AND ".join(conditions)
                + " ORDER BY type, id",
                parameters,
            ).fetchall()

        inputs: list[dict[str, str]] = []
        relation_specs: list[tuple[str, str, str, str, str, str]] = []
        watermarks: list[str] = []
        for row in rows:
            try:
                payload = json.loads(row["data"])
            except (TypeError, ValueError) as error:
                raise RelationshipProjectionError(
                    f"invalid authoritative payload for {row['type']}:{row['id']}"
                ) from error
            reference_field, predicate, target_type, inverse = _SOURCE_RELATIONSHIPS[row["type"]]
            target_id = str(payload.get(reference_field) or "").strip()
            if not target_id:
                continue
            scope_key = f"{row['type']}:{row['id']}"
            inputs.append(
                {
                    "reference_field": reference_field,
                    "source_id": row["id"],
                    "source_type": row["type"],
                    "target_id": target_id,
                    "target_type": target_type,
                }
            )
            relation_specs.append((scope_key, row["type"], row["id"], predicate, target_type, target_id))
            relation_specs.append((scope_key, target_type, target_id, inverse, row["type"], row["id"]))
            if row["updated_date"]:
                watermarks.append(str(row["updated_date"]))

        input_hash = _checksum(inputs)
        input_data_version = f"relationship-input:{input_hash.removeprefix('sha256:')}"
        generated_at = _now()
        relationships = tuple(
            ScopedRelationshipWrite(
                scope_key,
                RelationshipWrite(
                    owner_email=owner_email,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    predicate=predicate,
                    object_type=object_type,
                    object_id=object_id,
                    source_id=f"urn:glucopilot:entity:{scope_key}",
                    input_data_version=input_data_version,
                    input_hash=input_hash,
                    projection_key=f"{scope_key}:{predicate}",
                    generated_at=generated_at,
                ),
            )
            for scope_key, subject_type, subject_id, predicate, object_type, object_id in sorted(relation_specs)
        )
        return ProjectionPlan(
            owner_email=owner_email,
            generator_id=self.generator_id,
            generator_version=self.generator_version,
            scope=scope,
            input_data_version=input_data_version,
            input_hash=input_hash,
            watermark=max(watermarks) if watermarks else None,
            relationships=relationships,
        )


class RelationshipProjectionRepository:
    """Record build outcomes and atomically publish complete graph generations."""

    def __init__(self, database: Path | str | None = None) -> None:
        self._database = Path(database) if database is not None else None

    def _connect(self) -> sqlite3.Connection:
        if self._database is None:
            return db.connect()
        connection = sqlite3.connect(self._database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @staticmethod
    def _edge_payload(edge: Any) -> dict[str, Any]:
        payload = asdict(edge)
        payload.pop("created_at", None)
        payload.pop("generated_at", None)
        return payload

    @staticmethod
    def _active_graph(connection: sqlite3.Connection, plan: ProjectionPlan) -> tuple[int, str, str | None]:
        rows = connection.execute(
            """
            SELECT e.*
            FROM relationship_projection_active_edges active
            JOIN entity_relationships e ON e.id=active.relationship_id
            WHERE active.owner_id=? AND active.generator_id=? AND active.generator_version=?
            ORDER BY e.id
            """,
            (DEPLOYMENT_OWNER_ID, plan.generator_id, plan.generator_version),
        ).fetchall()
        payloads = []
        for row in rows:
            payload = dict(row)
            payload.pop("created_at", None)
            payload.pop("generated_at", None)
            payloads.append(payload)
        watermark = connection.execute(
            """
            SELECT MAX(run.watermark)
            FROM relationship_projection_active_edges active
            JOIN relationship_projection_runs run ON run.id=active.run_id
            WHERE active.owner_id=? AND active.generator_id=? AND active.generator_version=?
            """,
            (DEPLOYMENT_OWNER_ID, plan.generator_id, plan.generator_version),
        ).fetchone()[0]
        return len(rows), _checksum(payloads), watermark

    @staticmethod
    def _validate_plan(plan: ProjectionPlan) -> None:
        _required(plan.owner_email, "owner_email")
        _required(plan.generator_id, "generator_id")
        _required(plan.generator_version, "generator_version")
        _required(plan.input_data_version, "input_data_version")
        if not plan.input_hash.startswith("sha256:") or len(plan.input_hash) != 71:
            raise RelationshipProjectionError("input_hash must be a sha256 digest")
        plan.scope.definition()
        if plan.scope.kind == "entity":
            expected = plan.scope.scope_key
            if any(item.scope_key != expected for item in plan.relationships):
                raise RelationshipProjectionError("entity-scoped output escaped its requested scope")
        identities = [
            (item.scope_key, item.relationship.projection_key)
            for item in plan.relationships
        ]
        if len(identities) != len(set(identities)):
            raise RelationshipProjectionError("projection output contains duplicate identities")
        if len(plan.relationships) > MAX_RELATIONSHIPS_PER_RUN:
            raise RelationshipProjectionError("projection output exceeds the per-run relationship limit")
        for item in plan.relationships:
            write = item.relationship
            _required(item.scope_key, "scope_key")
            if write.owner_email != plan.owner_email:
                raise RelationshipProjectionError("projection output escaped its owner scope")
            if (write.generator_id, write.generator_version) != (
                plan.generator_id,
                plan.generator_version,
            ):
                raise RelationshipProjectionError("projection output has inconsistent generator metadata")
            if (write.input_data_version, write.input_hash) != (
                plan.input_data_version,
                plan.input_hash,
            ):
                raise RelationshipProjectionError("projection output has inconsistent input metadata")

    def run(self, plan: ProjectionPlan) -> ProjectionRunResult:
        self._validate_plan(plan)
        run_id = f"urn:glucopilot:relationship-projection-run:{uuid.uuid4().hex}"
        started_at = _now()
        scope_json = _canonical(plan.scope.definition())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO relationship_projection_runs (
                    id, owner_id, owner_email, generator_id, generator_version,
                    scope_kind, scope_json, scope_checksum, input_data_version,
                    input_hash, watermark, status, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    run_id,
                    DEPLOYMENT_OWNER_ID,
                    plan.owner_email,
                    plan.generator_id,
                    plan.generator_version,
                    plan.scope.kind,
                    scope_json,
                    plan.scope.checksum,
                    plan.input_data_version,
                    plan.input_hash,
                    plan.watermark,
                    started_at,
                ),
            )
            connection.commit()

            try:
                connection.execute("BEGIN IMMEDIATE")
                repository = SqliteRelationshipRepository(connection)
                projected = []
                ordered = sorted(
                    plan.relationships,
                    key=lambda item: (item.scope_key, item.relationship.projection_key),
                )
                for ordinal, item in enumerate(ordered):
                    edge = repository.add(item.relationship)
                    projected.append(edge)
                    connection.execute(
                        """
                        INSERT INTO relationship_projection_run_edges (
                            run_id, relationship_id, scope_key, ordinal
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (run_id, edge.id, item.scope_key, ordinal),
                    )

                if plan.scope.kind == "full":
                    connection.execute(
                        """
                        DELETE FROM relationship_projection_active_edges
                        WHERE owner_id=? AND generator_id=? AND generator_version=?
                        """,
                        (DEPLOYMENT_OWNER_ID, plan.generator_id, plan.generator_version),
                    )
                else:
                    connection.execute(
                        """
                        DELETE FROM relationship_projection_active_edges
                        WHERE owner_id=? AND generator_id=? AND generator_version=? AND scope_key=?
                        """,
                        (
                            DEPLOYMENT_OWNER_ID,
                            plan.generator_id,
                            plan.generator_version,
                            plan.scope.scope_key,
                        ),
                    )
                for item, edge in zip(ordered, projected, strict=True):
                    connection.execute(
                        """
                        INSERT INTO relationship_projection_active_edges (
                            owner_id, generator_id, generator_version,
                            scope_key, relationship_id, run_id
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            DEPLOYMENT_OWNER_ID,
                            plan.generator_id,
                            plan.generator_version,
                            item.scope_key,
                            edge.id,
                            run_id,
                        ),
                    )

                relationship_checksum = _checksum(
                    [self._edge_payload(edge) for edge in projected]
                )
                graph_count, graph_checksum, graph_watermark = self._active_graph(connection, plan)
                published_at = _now()
                connection.execute(
                    """
                    INSERT INTO relationship_projection_state (
                        owner_id, owner_email, generator_id, generator_version,
                        graph_checksum, relationship_count, watermark,
                        last_successful_run_id, published_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(owner_id, generator_id, generator_version) DO UPDATE SET
                        owner_email=excluded.owner_email,
                        graph_checksum=excluded.graph_checksum,
                        relationship_count=excluded.relationship_count,
                        watermark=excluded.watermark,
                        last_successful_run_id=excluded.last_successful_run_id,
                        published_at=excluded.published_at
                    """,
                    (
                        DEPLOYMENT_OWNER_ID,
                        plan.owner_email,
                        plan.generator_id,
                        plan.generator_version,
                        graph_checksum,
                        graph_count,
                        graph_watermark,
                        run_id,
                        published_at,
                    ),
                )
                connection.execute(
                    """
                    UPDATE relationship_projection_runs
                    SET status='succeeded', relationship_count=?, relationship_checksum=?,
                        completed_at=?, published_at=?
                    WHERE id=? AND status='running'
                    """,
                    (len(projected), relationship_checksum, published_at, published_at, run_id),
                )
                connection.commit()
                return ProjectionRunResult(
                    run_id=run_id,
                    status="succeeded",
                    scope=plan.scope,
                    relationship_count=len(projected),
                    relationship_checksum=relationship_checksum,
                    graph_relationship_count=graph_count,
                    graph_checksum=graph_checksum,
                    watermark=graph_watermark,
                    published_at=published_at,
                )
            except Exception as error:
                connection.rollback()
                completed_at = _now()
                error_payload = _canonical(
                    {
                        "message": str(error)[:1000] or error.__class__.__name__,
                        "type": error.__class__.__name__,
                    }
                )
                connection.execute(
                    """
                    UPDATE relationship_projection_runs
                    SET status='failed', error_json=?, completed_at=?
                    WHERE id=? AND status='running'
                    """,
                    (error_payload, completed_at, run_id),
                )
                connection.commit()
                raise RelationshipProjectionError(f"relationship projection run {run_id} failed") from error

    def freshness(
        self,
        owner_email: str,
        generator_id: str = _ALGORITHM_ID,
        generator_version: str = _ALGORITHM_VERSION,
    ) -> ProjectionFreshness:
        owner_email = _required(owner_email, "owner_email")
        with self._connect() as connection:
            state = connection.execute(
                """
                SELECT * FROM relationship_projection_state
                WHERE owner_id=? AND owner_email=? AND generator_id=? AND generator_version=?
                """,
                (DEPLOYMENT_OWNER_ID, owner_email, generator_id, generator_version),
            ).fetchone()
            latest = connection.execute(
                """
                SELECT id, status, started_at, completed_at
                FROM relationship_projection_runs
                WHERE owner_id=? AND owner_email=? AND generator_id=? AND generator_version=?
                ORDER BY started_at DESC, rowid DESC LIMIT 1
                """,
                (DEPLOYMENT_OWNER_ID, owner_email, generator_id, generator_version),
            ).fetchone()
        published_at = state["published_at"] if state else None
        age_seconds = None
        if published_at:
            published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            age_seconds = max(0.0, (datetime.now(timezone.utc) - published).total_seconds())
        return ProjectionFreshness(
            owner_email=owner_email,
            generator_id=generator_id,
            generator_version=generator_version,
            graph_checksum=state["graph_checksum"] if state else None,
            relationship_count=state["relationship_count"] if state else 0,
            watermark=state["watermark"] if state else None,
            published_at=published_at,
            age_seconds=age_seconds,
            last_successful_run_id=state["last_successful_run_id"] if state else None,
            latest_run_id=latest["id"] if latest else None,
            latest_run_status=latest["status"] if latest else None,
            latest_run_started_at=latest["started_at"] if latest else None,
            latest_run_completed_at=latest["completed_at"] if latest else None,
        )


def rebuild_legacy_relationships(
    owner_email: str,
    scope: ProjectionScope | None = None,
    *,
    database: Path | str | None = None,
) -> ProjectionRunResult:
    """Run the governed projector when the explicit write gate is enabled."""
    if not relationship_projection_writes_enabled():
        raise RelationshipProjectionError("relationship projection writes are disabled")
    projector = LegacyReferenceProjector(database)
    return RelationshipProjectionRepository(database).run(projector.plan(owner_email, scope))


def _main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild the governed relationship projection")
    parser.add_argument("--owner-email", required=True)
    parser.add_argument("--entity-type")
    parser.add_argument("--entity-id")
    arguments = parser.parse_args()
    if bool(arguments.entity_type) != bool(arguments.entity_id):
        parser.error("--entity-type and --entity-id must be supplied together")
    scope = (
        ProjectionScope.entity(arguments.entity_type, arguments.entity_id)
        if arguments.entity_type
        else ProjectionScope.full()
    )
    print(_canonical(asdict(rebuild_legacy_relationships(arguments.owner_email, scope))))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
