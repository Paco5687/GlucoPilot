"""Deterministic bounded evidence sets for time-series claims."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from . import db
from .data_contracts import DEPLOYMENT_OWNER_ID

if TYPE_CHECKING:
    from .repositories import EvidenceRepository, RepositoryCatalog


MAX_OBSERVATIONS = 100_000
MAX_WINDOWS_PER_SET = 16
GENERATOR_ID = "bounded-observation-window"
GENERATOR_VERSION = "1.0.0"
_TRUE = {"1", "true", "yes", "on"}


class EvidenceSetError(ValueError):
    pass


class StaleEvidenceError(RuntimeError):
    pass


def evidence_set_writes_enabled() -> bool:
    return os.getenv("EVIDENCE_SET_WRITES_ENABLED", "false").strip().lower() in _TRUE


def evidence_set_reads_enabled() -> bool:
    return os.getenv("EVIDENCE_SET_READS_ENABLED", "false").strip().lower() in _TRUE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _instant(value: str, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise EvidenceSetError(f"{field} must be a UTC instant") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise EvidenceSetError(f"{field} must be a UTC instant")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise EvidenceSetError(f"value is not canonical JSON: {error}") from error


def _checksum(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _required(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise EvidenceSetError(f"{field} is required")
    return result


def _observation(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if not key.startswith("_")}


class SqliteEvidenceSetRepository:
    def __init__(
        self,
        connection: sqlite3.Connection | None = None,
        *,
        database=None,
        repositories: RepositoryCatalog | None = None,
    ) -> None:
        self._connection = connection
        self._database = database
        self._repositories = repositories

    @contextmanager
    def _scope(self) -> Iterator[sqlite3.Connection]:
        if self._connection is not None:
            yield self._connection
            return
        connection = db.connect() if self._database is None else sqlite3.connect(self._database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _catalog(self):
        if self._repositories is not None:
            return self._repositories
        from .repositories import get_repositories

        return get_repositories()

    def capture_window(
        self,
        *,
        owner_email: str,
        entity_type: str,
        time_field: str,
        window_start: str,
        window_end: str,
        observations: list[dict[str, Any]],
        filters: dict[str, Any] | None = None,
        summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        owner_email = _required(owner_email, "owner_email")
        entity_type = _required(entity_type, "entity_type")
        time_field = _required(time_field, "time_field")
        start, end = _instant(window_start, "window_start"), _instant(window_end, "window_end")
        if end < start:
            raise EvidenceSetError("window_end cannot precede window_start")
        selected = []
        for record in observations:
            if record.get("owner_email") != owner_email or not record.get("id"):
                continue
            observed = _instant(record.get(time_field), time_field)
            if start <= observed <= end:
                selected.append((observed, str(record["id"]), _observation(record)))
        selected.sort(key=lambda item: (item[0], item[1]))
        if not selected:
            raise EvidenceSetError("observation window must contain at least one observation")
        if len(selected) > MAX_OBSERVATIONS:
            raise EvidenceSetError("observation window exceeds the bounded membership limit")
        member_ids = [item[1] for item in selected]
        if len(set(member_ids)) != len(member_ids):
            raise EvidenceSetError("observation IDs must be unique")
        canonical_observations = [item[2] for item in selected]
        query = {
            "entity_type": entity_type,
            "time_field": time_field,
            "filters": filters or {},
            "window_start": start,
            "window_end": end,
        }
        query_checksum = _checksum(query)
        observation_checksum = _checksum(canonical_observations)
        summary_value = summary or {}
        summary_checksum = _checksum(summary_value)
        window_id = "urn:glucopilot:observation-window:" + hashlib.sha256(
            f"{DEPLOYMENT_OWNER_ID}\0{query_checksum}\0{observation_checksum}\0{summary_checksum}".encode()
        ).hexdigest()
        row = {
            "id": window_id,
            "owner_id": DEPLOYMENT_OWNER_ID,
            "owner_email": owner_email,
            "entity_type": entity_type,
            "query_definition_json": _canonical(query),
            "query_checksum": query_checksum,
            "window_start": start,
            "window_end": end,
            "observation_count": len(member_ids),
            "observation_checksum": observation_checksum,
            "member_ids_json": _canonical(member_ids),
            "summary_json": _canonical(summary_value),
            "summary_checksum": summary_checksum,
            "generator_id": GENERATOR_ID,
            "generator_version": GENERATOR_VERSION,
            "status": "valid",
            "invalidated_at": None,
            "invalidation_reason": None,
            "created_at": _now(),
        }
        with self._scope() as connection:
            if not connection.execute(
                "SELECT 1 FROM entity_schema_registry WHERE entity_type=?", (entity_type,)
            ).fetchone():
                raise EvidenceSetError("unknown observation entity type")
            columns = tuple(row)
            connection.execute(
                f"INSERT OR IGNORE INTO observation_windows ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                tuple(row[column] for column in columns),
            )
            stored = connection.execute(
                "SELECT * FROM observation_windows WHERE id=?", (window_id,)
            ).fetchone()
        return self._window(stored)

    @staticmethod
    def _window(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for source, target in (
            ("query_definition_json", "query_definition"),
            ("member_ids_json", "member_ids"),
            ("summary_json", "summary"),
        ):
            result[target] = json.loads(result.pop(source))
        return result

    def get_window(self, window_id: str) -> dict[str, Any] | None:
        with self._scope() as connection:
            row = connection.execute(
                "SELECT * FROM observation_windows WHERE id=?", (window_id,)
            ).fetchone()
        return self._window(row) if row else None

    def _members(self, window: dict[str, Any]) -> list[dict[str, Any]]:
        repository = self._catalog().entity(window["entity_type"])
        found: dict[str, dict[str, Any]] = {}
        ids = window["member_ids"]
        for offset in range(0, len(ids), 400):
            for record in repository.query(
                {"owner_email": window["owner_email"], "id": {"$in": ids[offset : offset + 400]}},
                limit=400,
            ):
                found[str(record["id"])] = record
        return [found[member_id] for member_id in ids if member_id in found]

    def validate_window(self, window_id: str) -> bool:
        window = self.get_window(window_id)
        if not window:
            raise EvidenceSetError("observation window not found")
        if window["status"] != "valid":
            return False
        current = self._members(window)
        fresh = len(current) == window["observation_count"] and _checksum(
            [_observation(record) for record in current]
        ) == window["observation_checksum"]
        if not fresh and window["status"] == "valid":
            with self._scope() as connection:
                connection.execute(
                    """
                    UPDATE observation_windows
                    SET status='invalidated', invalidated_at=?, invalidation_reason='source_data_changed'
                    WHERE id=? AND status='valid'
                    """,
                    (_now(), window_id),
                )
                connection.execute(
                    """
                    UPDATE evidence_sets
                    SET status='invalidated', invalidated_at=?,
                        invalidation_reason='observation_window_invalidated'
                    WHERE status='valid' AND id IN (
                        SELECT evidence_set_id FROM evidence_set_windows
                        WHERE observation_window_id=?
                    )
                    """,
                    (_now(), window_id),
                )
        return fresh

    def drill_down(self, window_id: str) -> list[dict[str, Any]]:
        if not self.validate_window(window_id):
            raise StaleEvidenceError("source observations changed; evidence window is stale")
        window = self.get_window(window_id)
        return self._members(window)

    def create_set(
        self,
        *,
        owner_email: str,
        claim_type: str,
        claim_id: str,
        window_ids: list[str],
        summary: dict[str, Any],
        input_data_version: str,
    ) -> dict[str, Any]:
        owner_email = _required(owner_email, "owner_email")
        if not 1 <= len(window_ids) <= MAX_WINDOWS_PER_SET or len(set(window_ids)) != len(window_ids):
            raise EvidenceSetError("evidence set requires 1-16 unique windows")
        claim = self._catalog().entity(claim_type).get(claim_id)
        if not claim or claim.get("owner_email") != owner_email:
            raise EvidenceSetError("claim is missing or outside the owner scope")
        with self._scope() as connection:
            placeholders = ",".join("?" for _ in window_ids)
            windows = connection.execute(
                f"SELECT * FROM observation_windows WHERE id IN ({placeholders}) "
                "AND owner_id=? AND owner_email=? AND status='valid'",
                (*window_ids, DEPLOYMENT_OWNER_ID, owner_email),
            ).fetchall()
            by_id = {row["id"]: row for row in windows}
            if set(by_id) != set(window_ids):
                raise EvidenceSetError("evidence set contains a missing, stale, or foreign window")
            payload = {
                "claim_type": claim_type,
                "claim_id": claim_id,
                "windows": [(item, by_id[item]["observation_checksum"]) for item in window_ids],
                "summary": summary,
                "generator_id": GENERATOR_ID,
                "generator_version": GENERATOR_VERSION,
                "input_data_version": _required(input_data_version, "input_data_version"),
            }
            set_checksum = _checksum(payload)
            set_id = "urn:glucopilot:evidence-set:" + set_checksum.removeprefix("sha256:")
            connection.execute(
                """
                INSERT OR IGNORE INTO evidence_sets (
                    id, owner_id, owner_email, claim_type, claim_id, set_checksum,
                    summary_json, generator_id, generator_version, input_data_version,
                    status, invalidated_at, invalidation_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    set_id, DEPLOYMENT_OWNER_ID, owner_email, claim_type, claim_id,
                    set_checksum, _canonical(summary), GENERATOR_ID, GENERATOR_VERSION,
                    payload["input_data_version"], "valid", None, None, _now(),
                ),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO evidence_set_windows "
                "(evidence_set_id, observation_window_id, ordinal) VALUES (?, ?, ?)",
                [(set_id, window_id, index) for index, window_id in enumerate(window_ids)],
            )
            stored = connection.execute("SELECT * FROM evidence_sets WHERE id=?", (set_id,)).fetchone()
        return {**dict(stored), "summary": json.loads(stored["summary_json"]), "window_ids": window_ids}

    def for_claim(self, owner_email: str, claim_type: str, claim_id: str):
        from .repositories import EvidenceReference

        with self._scope() as connection:
            rows = connection.execute(
                """
                SELECT evidence_sets.* FROM evidence_sets
                WHERE owner_id=? AND owner_email=? AND claim_type=? AND claim_id=?
                ORDER BY evidence_sets.created_at, evidence_sets.id
                """,
                (DEPLOYMENT_OWNER_ID, owner_email, claim_type, claim_id),
            ).fetchall()
            references = []
            for row in rows:
                window_ids = [
                    item[0]
                    for item in connection.execute(
                        "SELECT observation_window_id FROM evidence_set_windows "
                        "WHERE evidence_set_id=? ORDER BY ordinal",
                        (row["id"],),
                    )
                ]
                references.append(
                    EvidenceReference(
                        claim_type,
                        claim_id,
                        "evidence_set",
                        row["id"],
                        {
                            "checksum": row["set_checksum"],
                            "status": row["status"],
                            "summary": json.loads(row["summary_json"]),
                            "window_ids": window_ids,
                        },
                    )
                )
        return references


class EvidenceCompatibilityRepository:
    def __init__(self, legacy: EvidenceRepository, typed: SqliteEvidenceSetRepository) -> None:
        self.legacy = legacy
        self.typed = typed

    def for_claim(self, owner_email: str, claim_type: str, claim_id: str):
        if evidence_set_reads_enabled():
            return self.typed.for_claim(owner_email, claim_type, claim_id)
        return self.legacy.for_claim(owner_email, claim_type, claim_id)
