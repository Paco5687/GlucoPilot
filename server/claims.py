"""Versioned Pattern and Insight claim lineage over bounded EvidenceSets."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from . import db
from .data_contracts import DEPLOYMENT_OWNER_ID

if TYPE_CHECKING:
    from .repositories import RepositoryCatalog


CLAIM_CONTRACT_VERSION = "evidence-backed-claim/1.0.0"
ALGORITHMS = {
    "Pattern": ("glucose-pattern-analysis", "2.0.0"),
    "Insight": ("cross-domain-insight-analysis", "2.0.0"),
}


class ClaimVersionError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ClaimVersionError(f"claim value is not canonical JSON: {error}") from error


def checksum(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode()).hexdigest()


def semantic_claim_key(claim_type: str, payload: dict[str, Any]) -> str:
    """Return a stable key for the same logical claim across analysis runs."""
    if claim_type == "Pattern":
        raw = payload.get("supporting_evidence") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = {}
        details = {
            key: raw.get(key)
            for key in ("fromHour", "toHour", "direction")
            if isinstance(raw, dict) and raw.get(key) is not None
        }
        identity = {
            "claim_type": claim_type,
            "pattern_type": payload.get("pattern_type"),
            "time_of_day": payload.get("time_of_day"),
            "details": details,
        }
    elif claim_type == "Insight":
        raw = payload.get("supporting_data") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = {}
        identity = {
            "claim_type": claim_type,
            "kind": raw.get("kind"),
            "x": raw.get("x"),
            "y": raw.get("y"),
            "best_phase": raw.get("best_phase"),
            "worst_phase": raw.get("worst_phase"),
            "highest_phase": raw.get("highest_phase"),
            "lowest_phase": raw.get("lowest_phase"),
            "category": payload.get("category"),
        }
    else:
        raise ClaimVersionError("only Pattern and Insight claims are versioned")
    return f"urn:glucopilot:claim-key:{claim_type.lower()}:" + checksum(identity).removeprefix(
        "sha256:"
    )


def evidence_input_version(windows: list[dict[str, Any]]) -> str:
    return checksum(
        [
            {
                "id": window["id"],
                "entity_type": window["entity_type"],
                "observation_checksum": window["observation_checksum"],
                "summary_checksum": window["summary_checksum"],
            }
            for window in sorted(windows, key=lambda item: (item["entity_type"], item["id"]))
        ]
    )


def claim_limitations(
    analytics_confidence: dict[str, Any],
    data_quality: dict[str, Any],
) -> list[dict[str, Any]]:
    limitations: list[dict[str, Any]] = []
    interval = analytics_confidence.get("confidence_interval")
    missingness = analytics_confidence.get("missingness")
    if interval or missingness:
        limitations.append(
            {
                "kind": "analytics_uncertainty",
                "confidence_interval": interval,
                "missingness": missingness,
                "discovery_status": analytics_confidence.get("discovery_status"),
            }
        )
    for domain, quality in sorted(data_quality.items()):
        if not isinstance(quality, dict):
            continue
        messages = quality.get("limitations") or []
        if quality.get("coverage_status") not in (None, "complete") or messages:
            limitations.append(
                {
                    "kind": "data_quality",
                    "domain": domain,
                    "coverage_status": quality.get("coverage_status"),
                    "freshness_status": quality.get("freshness_status"),
                    "limitations": messages,
                }
            )
    return limitations


class SqliteClaimVersionRepository:
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

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["analytics_confidence"] = json.loads(value.pop("analytics_confidence_json"))
        return value

    def create_version(
        self,
        *,
        owner_email: str,
        claim_type: str,
        claim_entity_id: str,
        claim_key: str,
        content: dict[str, Any],
        input_data_version: str,
        analytics_confidence: dict[str, Any],
        assertion_kind: str = "derived_statistic",
    ) -> tuple[dict[str, Any], list[str]]:
        if claim_type not in ALGORITHMS:
            raise ClaimVersionError("only Pattern and Insight claims are versioned")
        entity = self._catalog().entity(claim_type).get(claim_entity_id)
        if not entity or entity.get("owner_email") != owner_email:
            raise ClaimVersionError("claim entity is missing or outside the owner scope")
        if assertion_kind not in {"derived_statistic", "hypothesis"}:
            raise ClaimVersionError("unsupported claim assertion kind")
        algorithm_id, algorithm_version = ALGORITHMS[claim_type]
        now = _now()
        with self._scope() as connection:
            previous = connection.execute(
                """
                SELECT * FROM claim_versions
                WHERE owner_id=? AND owner_email=? AND claim_type=? AND claim_key=?
                  AND assertion_status!='superseded'
                ORDER BY version_number DESC, id
                """,
                (DEPLOYMENT_OWNER_ID, owner_email, claim_type, claim_key),
            ).fetchall()
            version_number = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(version_number), 0) FROM claim_versions
                    WHERE owner_id=? AND claim_type=? AND claim_key=?
                    """,
                    (DEPLOYMENT_OWNER_ID, claim_type, claim_key),
                ).fetchone()[0]
            ) + 1
            content_checksum = checksum(content)
            claim_version_id = "urn:glucopilot:claim-version:" + checksum(
                {
                    "owner_id": DEPLOYMENT_OWNER_ID,
                    "claim_type": claim_type,
                    "claim_entity_id": claim_entity_id,
                    "claim_key": claim_key,
                    "version_number": version_number,
                    "content_checksum": content_checksum,
                    "input_data_version": input_data_version,
                    "algorithm": [algorithm_id, algorithm_version],
                }
            ).removeprefix("sha256:")
            predecessor = previous[0]["id"] if previous else None
            connection.execute(
                """
                INSERT INTO claim_versions (
                    id, owner_id, owner_email, claim_type, claim_entity_id, claim_key,
                    version_number, content_checksum, assertion_kind, assertion_status,
                    algorithm_id, algorithm_version, input_data_version,
                    analytics_confidence_json, evidence_set_id,
                    supersedes_claim_version_id, superseded_by_claim_version_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?)
                """,
                (
                    claim_version_id,
                    DEPLOYMENT_OWNER_ID,
                    owner_email,
                    claim_type,
                    claim_entity_id,
                    claim_key,
                    version_number,
                    content_checksum,
                    assertion_kind,
                    "provisional",
                    algorithm_id,
                    algorithm_version,
                    input_data_version,
                    canonical_json(analytics_confidence),
                    predecessor,
                    now,
                    now,
                ),
            )
            previous_entity_ids = [str(row["claim_entity_id"]) for row in previous]
            if previous:
                connection.execute(
                    """
                    UPDATE claim_versions SET assertion_status='superseded',
                        superseded_by_claim_version_id=?, updated_at=?
                    WHERE owner_id=? AND owner_email=? AND claim_type=? AND claim_key=?
                      AND id!=? AND assertion_status!='superseded'
                    """,
                    (
                        claim_version_id,
                        now,
                        DEPLOYMENT_OWNER_ID,
                        owner_email,
                        claim_type,
                        claim_key,
                        claim_version_id,
                    ),
                )
            stored = connection.execute(
                "SELECT * FROM claim_versions WHERE id=?", (claim_version_id,)
            ).fetchone()
        return self._row(stored), previous_entity_ids

    def attach_evidence(self, claim_version_id: str, evidence_set_id: str) -> dict[str, Any]:
        now = _now()
        with self._scope() as connection:
            changed = connection.execute(
                """
                UPDATE claim_versions SET evidence_set_id=?, updated_at=?
                WHERE id=? AND evidence_set_id IS NULL
                """,
                (evidence_set_id, now, claim_version_id),
            ).rowcount
            if changed != 1:
                raise ClaimVersionError("claim version is missing or already linked to evidence")
            stored = connection.execute(
                "SELECT * FROM claim_versions WHERE id=?", (claim_version_id,)
            ).fetchone()
        return self._row(stored)

    def supersede_except(
        self,
        *,
        owner_email: str,
        claim_type: str,
        current_claim_version_ids: list[str],
    ) -> list[str]:
        with self._scope() as connection:
            parameters: list[Any] = [DEPLOYMENT_OWNER_ID, owner_email, claim_type]
            where = (
                "owner_id=? AND owner_email=? AND claim_type=? "
                "AND assertion_status!='superseded'"
            )
            if current_claim_version_ids:
                where += f" AND id NOT IN ({','.join('?' for _ in current_claim_version_ids)})"
                parameters.extend(current_claim_version_ids)
            rows = connection.execute(
                f"SELECT claim_entity_id FROM claim_versions WHERE {where}", parameters
            ).fetchall()
            if rows:
                connection.execute(
                    f"UPDATE claim_versions SET assertion_status='superseded', updated_at=? WHERE {where}",
                    (_now(), *parameters),
                )
        return [str(row[0]) for row in rows]

    def for_entity(
        self, owner_email: str, claim_type: str, claim_entity_id: str
    ) -> dict[str, Any] | None:
        with self._scope() as connection:
            row = connection.execute(
                """
                SELECT * FROM claim_versions
                WHERE owner_id=? AND owner_email=? AND claim_type=? AND claim_entity_id=?
                """,
                (DEPLOYMENT_OWNER_ID, owner_email, claim_type, claim_entity_id),
            ).fetchone()
        return self._row(row) if row else None

    def history(self, owner_email: str, claim_type: str, claim_key: str) -> list[dict[str, Any]]:
        with self._scope() as connection:
            rows = connection.execute(
                """
                SELECT * FROM claim_versions
                WHERE owner_id=? AND owner_email=? AND claim_type=? AND claim_key=?
                ORDER BY version_number DESC, id
                """,
                (DEPLOYMENT_OWNER_ID, owner_email, claim_type, claim_key),
            ).fetchall()
        return [self._row(row) for row in rows]
