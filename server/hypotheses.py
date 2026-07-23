"""Guarded health-hypothesis ledger with attributable evidence and decisions.

Hypotheses are deliberately separate from confirmed Diagnosis entities. Patient,
algorithm, and clinician proposals all begin as tentative. Evidence revisions
are append-only, confidence is recalculated deterministically, and only an
admin-recorded clinician decision can confirm or rule against a hypothesis.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import db
from .auth import current_user, require_admin, require_login, session_role
from .config import OWNER_EMAIL
from .data_contracts import DEPLOYMENT_OWNER_ID


router = APIRouter()

CONFIDENCE_METHOD = "weighted-evidence-v1"
ORIGINS = {"patient", "algorithm", "clinician"}
STATUSES = {"proposed", "under_review", "confirmed", "ruled_against", "archived"}
TERMINAL_STATUSES = {"confirmed", "ruled_against", "archived"}
TRANSITIONS = {
    "proposed": {"under_review", "archived"},
    "under_review": {"confirmed", "ruled_against", "archived"},
    "confirmed": set(),
    "ruled_against": set(),
    "archived": set(),
}
SOURCE_KINDS = {
    "entity",
    "evidence_item",
    "evidence_set",
    "clinical_reference",
    "patient_report",
    "missing",
}
EVIDENCE_ROLES = {"supporting", "opposing", "missing"}
SAFE_LINK_PREFIXES = (
    "/api/evidence/",
    "/api/records/",
    "https://doi.org/",
    "https://pubmed.ncbi.nlm.nih.gov/",
)


class HypothesisError(RuntimeError):
    """Raised when a guarded hypothesis operation is invalid."""


class EvidenceBody(BaseModel):
    role: str
    source_kind: str
    source_type: str = ""
    source_id: str | None = None
    source_version: str = ""
    summary: str
    weight: float = Field(default=1.0, gt=0, le=1)
    source_link: dict[str, Any] = Field(default_factory=dict)


class CreateHypothesisBody(BaseModel):
    title: str
    description: str = ""
    origin_kind: str = "patient"
    origin_label: str = ""
    suggested_verification: str = ""
    review_at: str | None = None
    evidence: list[EvidenceBody] = Field(default_factory=list, max_length=100)


class ReviseEvidenceBody(BaseModel):
    reason: str
    suggested_verification: str | None = None
    review_at: str | None = None
    evidence: list[EvidenceBody] = Field(default_factory=list, max_length=100)


class TransitionBody(BaseModel):
    status: str
    reason: str
    decision_authority: str | None = None
    reviewer: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _text(value: Any, limit: int, *, required: bool = False, label: str = "value") -> str:
    normalized = " ".join(str(value or "").split())
    if required and not normalized:
        raise HypothesisError(f"{label} is required")
    if len(normalized) > limit:
        raise HypothesisError(f"{label} exceeds {limit} characters")
    return normalized


def _review_at(value: str | None) -> str | None:
    normalized = _text(value, 40) or None
    if normalized:
        try:
            datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError as error:
            raise HypothesisError("review time must be an ISO date or timestamp") from error
    return normalized


def _safe_link(value: dict[str, Any]) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    href = _text(value.get("href"), 500)
    if href and not href.startswith(SAFE_LINK_PREFIXES):
        raise HypothesisError("evidence links must use an approved source location")
    output = {}
    for key, limit in (("kind", 80), ("label", 240), ("href", 500)):
        normalized = _text(value.get(key), limit)
        if normalized:
            output[key] = normalized
    return output


def _normalize_evidence(values: list[EvidenceBody | dict[str, Any]]) -> list[dict[str, Any]]:
    if len(values) > 100:
        raise HypothesisError("a hypothesis may reference at most 100 evidence items")
    output = []
    for value in values:
        raw = value.model_dump() if isinstance(value, EvidenceBody) else dict(value)
        role = _text(raw.get("role"), 20, required=True, label="evidence role").lower()
        source_kind = _text(
            raw.get("source_kind"), 40, required=True, label="evidence source kind"
        ).lower()
        if role not in EVIDENCE_ROLES:
            raise HypothesisError("evidence role must be supporting, opposing, or missing")
        if source_kind not in SOURCE_KINDS:
            raise HypothesisError("unsupported evidence source kind")
        source_id = _text(raw.get("source_id"), 500) or None
        if role == "missing":
            if source_kind != "missing" or source_id is not None:
                raise HypothesisError("missing evidence must use the missing source kind")
        elif source_kind == "missing" or source_id is None:
            raise HypothesisError("supporting and opposing evidence require a source identity")
        try:
            weight = float(raw.get("weight", 1))
        except (TypeError, ValueError) as error:
            raise HypothesisError("evidence weight must be a number") from error
        if not math.isfinite(weight) or not 0 < weight <= 1:
            raise HypothesisError("evidence weight must be greater than 0 and at most 1")
        output.append(
            {
                "role": role,
                "source_kind": source_kind,
                "source_type": _text(raw.get("source_type"), 120),
                "source_id": source_id,
                "source_version": _text(raw.get("source_version"), 500),
                "summary": _text(
                    raw.get("summary"), 2_000, required=True, label="evidence summary"
                ),
                "weight": round(weight, 6),
                "source_link": _safe_link(raw.get("source_link") or {}),
            }
        )
    return output


def _confidence(evidence: list[dict[str, Any]]) -> tuple[float, str]:
    weights = {
        role: sum(item["weight"] for item in evidence if item["role"] == role)
        for role in EVIDENCE_ROLES
    }
    total = sum(weights.values())
    score = round(weights["supporting"] / total, 4) if total else 0.0
    rationale = (
        f"Weighted evidence: {weights['supporting']:.2f} supporting, "
        f"{weights['opposing']:.2f} opposing, {weights['missing']:.2f} missing. "
        "This is an evidence-balance score, not a diagnostic probability."
    )
    return score, rationale


def _input_version(evidence: list[dict[str, Any]]) -> str:
    return _hash(
        [
            {
                "role": item["role"],
                "source_kind": item["source_kind"],
                "source_type": item["source_type"],
                "source_id": item["source_id"],
                "source_version": item["source_version"],
                "summary": item["summary"],
                "weight": item["weight"],
            }
            for item in evidence
        ]
    )


def _actor(request: Request) -> dict[str, str]:
    role = session_role(request)
    user = current_user(role, request.session.get("provider_name", ""))
    return {
        "kind": "patient" if role == "admin" else "clinician",
        "role": role,
        "label": _text(user.get("full_name"), 240) or role,
    }


def _state(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": row["status"],
        "confidence_score": row["confidence_score"],
        "confidence_method": row["confidence_method"],
        "confidence_rationale": row["confidence_rationale"],
        "evidence_revision": row["evidence_revision"],
        "evidence_input_version": row["evidence_input_version"],
        "suggested_verification": row["suggested_verification"],
        "review_at": row["review_at"],
        "decided_by": row["decided_by"],
        "decided_at": row["decided_at"],
    }


class SqliteHypothesisRepository:
    """Typed repository over migration 15's guarded hypothesis ledger."""

    def __init__(self, connection: sqlite3.Connection | None = None) -> None:
        self._connection = connection

    @contextmanager
    def _scope(self) -> Iterator[sqlite3.Connection]:
        if self._connection is not None:
            yield self._connection
            return
        connection = db.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _insert_evidence(
        connection: sqlite3.Connection,
        hypothesis_id: str,
        revision: int,
        evidence: list[dict[str, Any]],
        created_at: str,
    ) -> None:
        for ordinal, item in enumerate(evidence):
            connection.execute(
                """
                INSERT INTO hypothesis_evidence (
                    id, hypothesis_id, evidence_revision, ordinal, evidence_role,
                    source_kind, source_type, source_id, source_version, summary,
                    weight, source_link_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"hyp_ev_{uuid.uuid4().hex}",
                    hypothesis_id,
                    revision,
                    ordinal,
                    item["role"],
                    item["source_kind"],
                    item["source_type"],
                    item["source_id"],
                    item["source_version"],
                    item["summary"],
                    item["weight"],
                    _canonical(item["source_link"]),
                    created_at,
                ),
            )

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        hypothesis_id: str,
        action: str,
        actor: dict[str, str],
        reason: str,
        before: dict[str, Any],
        after: dict[str, Any],
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO hypothesis_events (
                id, hypothesis_id, action, actor_kind, actor_role, actor_label,
                reason, before_json, after_json, evidence_input_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"hyp_event_{uuid.uuid4().hex}",
                hypothesis_id,
                action,
                actor["kind"],
                actor["role"],
                actor["label"],
                reason,
                _canonical(before),
                _canonical(after),
                after["evidence_input_version"],
                created_at,
            ),
        )

    @staticmethod
    def _evidence(connection: sqlite3.Connection, hypothesis_id: str, revision: int) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT evidence_role, source_kind, source_type, source_id, source_version,
                   summary, weight, source_link_json
            FROM hypothesis_evidence
            WHERE hypothesis_id=? AND evidence_revision=?
            ORDER BY ordinal
            """,
            (hypothesis_id, revision),
        ).fetchall()
        return [
            {
                "role": row["evidence_role"],
                "source_kind": row["source_kind"],
                "source_type": row["source_type"],
                "source_id": row["source_id"],
                "source_version": row["source_version"],
                "summary": row["summary"],
                "weight": row["weight"],
                "source_link": json.loads(row["source_link_json"]),
            }
            for row in rows
        ]

    @staticmethod
    def _events(connection: sqlite3.Connection, hypothesis_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT id, action, actor_kind, actor_role, actor_label, reason,
                   before_json, after_json, evidence_input_version, created_at
            FROM hypothesis_events WHERE hypothesis_id=?
            ORDER BY created_at, id
            """,
            (hypothesis_id,),
        ).fetchall()
        return [
            {
                **{key: row[key] for key in row.keys() if key not in {"before_json", "after_json"}},
                "before": json.loads(row["before_json"]),
                "after": json.loads(row["after_json"]),
            }
            for row in rows
        ]

    @classmethod
    def _public(
        cls,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        include_events: bool,
    ) -> dict[str, Any]:
        output = dict(row)
        output.pop("owner_id", None)
        output.pop("owner_email", None)
        evidence = cls._evidence(connection, output["id"], output["evidence_revision"])
        output["evidence"] = evidence
        output["evidence_by_role"] = {
            role: [item for item in evidence if item["role"] == role]
            for role in ("supporting", "opposing", "missing")
        }
        output["confidence_label"] = (
            "high"
            if output["confidence_score"] >= 0.75
            else "medium"
            if output["confidence_score"] >= 0.45
            else "low"
        )
        if include_events:
            output["events"] = cls._events(connection, output["id"])
        return output

    def create(
        self,
        body: CreateHypothesisBody,
        actor: dict[str, str],
    ) -> dict[str, Any]:
        title = _text(body.title, 240, required=True, label="hypothesis title")
        origin = _text(body.origin_kind, 20).lower()
        if origin not in ORIGINS:
            raise HypothesisError("origin must be patient, algorithm, or clinician")
        origin_label = _text(body.origin_label, 240) or actor["label"]
        evidence = _normalize_evidence(body.evidence)
        score, rationale = _confidence(evidence)
        input_version = _input_version(evidence)
        now = _now()
        hypothesis_id = f"hyp_{uuid.uuid4().hex}"
        revision = 1 if evidence else 0
        with self._scope() as connection:
            connection.execute(
                """
                INSERT INTO health_hypotheses (
                    id, owner_id, owner_email, title, description, origin_kind,
                    origin_label, status, confidence_score, confidence_method,
                    confidence_rationale, evidence_revision, evidence_input_version,
                    suggested_verification, review_at, decided_by, decided_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    hypothesis_id,
                    DEPLOYMENT_OWNER_ID,
                    OWNER_EMAIL,
                    title,
                    _text(body.description, 4_000),
                    origin,
                    origin_label,
                    score,
                    CONFIDENCE_METHOD,
                    rationale,
                    revision,
                    input_version,
                    _text(body.suggested_verification, 2_000),
                    _review_at(body.review_at),
                    now,
                    now,
                ),
            )
            self._insert_evidence(connection, hypothesis_id, revision, evidence, now)
            row = connection.execute(
                "SELECT * FROM health_hypotheses WHERE id=?", (hypothesis_id,)
            ).fetchone()
            after = _state(dict(row))
            self._insert_event(
                connection,
                hypothesis_id,
                "created",
                actor,
                "Hypothesis recorded as tentative; no diagnosis was created.",
                {},
                after,
                now,
            )
            return self._public(connection, row, include_events=True)

    def list(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        with self._scope() as connection:
            where = "owner_id=?"
            parameters: list[Any] = [DEPLOYMENT_OWNER_ID]
            if not include_archived:
                where += " AND status!='archived'"
            rows = connection.execute(
                f"""
                SELECT * FROM health_hypotheses WHERE {where}
                ORDER BY
                    CASE status
                        WHEN 'under_review' THEN 0 WHEN 'proposed' THEN 1
                        WHEN 'confirmed' THEN 2 WHEN 'ruled_against' THEN 3 ELSE 4
                    END,
                    updated_at DESC, id
                """,
                parameters,
            ).fetchall()
            return [self._public(connection, row, include_events=False) for row in rows]

    def get(self, hypothesis_id: str) -> dict[str, Any] | None:
        with self._scope() as connection:
            row = connection.execute(
                "SELECT * FROM health_hypotheses WHERE id=? AND owner_id=?",
                (hypothesis_id, DEPLOYMENT_OWNER_ID),
            ).fetchone()
            return self._public(connection, row, include_events=True) if row else None

    def revise_evidence(
        self,
        hypothesis_id: str,
        body: ReviseEvidenceBody,
        actor: dict[str, str],
    ) -> dict[str, Any]:
        reason = _text(body.reason, 2_000, required=True, label="evidence revision reason")
        evidence = _normalize_evidence(body.evidence)
        score, rationale = _confidence(evidence)
        input_version = _input_version(evidence)
        now = _now()
        with self._scope() as connection:
            row = connection.execute(
                "SELECT * FROM health_hypotheses WHERE id=? AND owner_id=?",
                (hypothesis_id, DEPLOYMENT_OWNER_ID),
            ).fetchone()
            if not row:
                raise HypothesisError("hypothesis not found")
            current = dict(row)
            if current["status"] in TERMINAL_STATUSES:
                raise HypothesisError("terminal hypotheses cannot receive new evidence")
            before = _state(current)
            revision = current["evidence_revision"] + 1
            suggested = (
                current["suggested_verification"]
                if body.suggested_verification is None
                else _text(body.suggested_verification, 2_000)
            )
            review_at = current["review_at"] if body.review_at is None else _review_at(body.review_at)
            connection.execute(
                """
                UPDATE health_hypotheses
                SET confidence_score=?, confidence_rationale=?, evidence_revision=?,
                    evidence_input_version=?, suggested_verification=?, review_at=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    score,
                    rationale,
                    revision,
                    input_version,
                    suggested,
                    review_at,
                    now,
                    hypothesis_id,
                ),
            )
            self._insert_evidence(connection, hypothesis_id, revision, evidence, now)
            updated = connection.execute(
                "SELECT * FROM health_hypotheses WHERE id=?", (hypothesis_id,)
            ).fetchone()
            self._insert_event(
                connection,
                hypothesis_id,
                "evidence_revised",
                actor,
                reason,
                before,
                _state(dict(updated)),
                now,
            )
            return self._public(connection, updated, include_events=True)

    def transition(
        self,
        hypothesis_id: str,
        body: TransitionBody,
        actor: dict[str, str],
    ) -> dict[str, Any]:
        target = _text(body.status, 30, required=True, label="status").lower()
        reason = _text(body.reason, 2_000, required=True, label="transition reason")
        if target not in STATUSES:
            raise HypothesisError("unsupported hypothesis status")
        now = _now()
        with self._scope() as connection:
            row = connection.execute(
                "SELECT * FROM health_hypotheses WHERE id=? AND owner_id=?",
                (hypothesis_id, DEPLOYMENT_OWNER_ID),
            ).fetchone()
            if not row:
                raise HypothesisError("hypothesis not found")
            current = dict(row)
            if target not in TRANSITIONS[current["status"]]:
                raise HypothesisError(
                    f"status transition {current['status']} -> {target} is not permitted"
                )
            event_actor = actor
            decided_by = None
            decided_at = None
            if target in {"confirmed", "ruled_against"}:
                authority = _text(body.decision_authority, 30).lower()
                reviewer = _text(body.reviewer, 240)
                if actor["role"] != "admin" or authority != "clinician" or not reviewer:
                    raise HypothesisError(
                        "confirmed and ruled-against decisions require an attributable clinician review"
                    )
                event_actor = {"kind": "clinician", "role": "admin", "label": reviewer}
                decided_by = reviewer
                decided_at = now
            before = _state(current)
            connection.execute(
                """
                UPDATE health_hypotheses
                SET status=?, decided_by=?, decided_at=?, updated_at=?
                WHERE id=?
                """,
                (target, decided_by, decided_at, now, hypothesis_id),
            )
            updated = connection.execute(
                "SELECT * FROM health_hypotheses WHERE id=?", (hypothesis_id,)
            ).fetchone()
            action = (
                "review_recorded"
                if target in {"confirmed", "ruled_against"}
                else "archived"
                if target == "archived"
                else "status_changed"
            )
            self._insert_event(
                connection,
                hypothesis_id,
                action,
                event_actor,
                reason,
                before,
                _state(dict(updated)),
                now,
            )
            return self._public(connection, updated, include_events=True)


def create_algorithm_hypothesis(
    *,
    title: str,
    algorithm_id: str,
    description: str = "",
    suggested_verification: str = "",
    evidence: list[dict[str, Any]] | None = None,
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Internal algorithm entrypoint; it can only create a proposed hypothesis."""
    body = CreateHypothesisBody(
        title=title,
        description=description,
        origin_kind="algorithm",
        origin_label=_text(algorithm_id, 240, required=True, label="algorithm id"),
        suggested_verification=suggested_verification,
        evidence=[EvidenceBody(**item) for item in evidence or []],
    )
    return SqliteHypothesisRepository(connection).create(
        body,
        {"kind": "algorithm", "role": "algorithm", "label": body.origin_label},
    )


def report_block() -> list[dict[str, Any]]:
    """Return active hypotheses plus safely separated legacy suspected conditions."""
    hypotheses = SqliteHypothesisRepository().list()
    legacy = db.query_entities(
        "Diagnosis", {"owner_email": OWNER_EMAIL, "status": "suspected"}, "-created_date", 100
    )
    for item in legacy:
        hypotheses.append(
            {
                "id": f"legacy-suspected:{item['id']}",
                "title": item.get("name") or "Legacy suspected condition",
                "description": item.get("notes") or "",
                "origin_kind": "patient",
                "origin_label": "Legacy suspected-condition entry",
                "status": "proposed",
                "confidence_score": 0,
                "confidence_label": "low",
                "confidence_method": CONFIDENCE_METHOD,
                "confidence_rationale": (
                    "No governed evidence revision exists for this legacy suspected entry."
                ),
                "suggested_verification": "Review and re-enter in the hypothesis ledger.",
                "review_at": None,
                "evidence_by_role": {"supporting": [], "opposing": [], "missing": [{
                    "role": "missing",
                    "source_kind": "missing",
                    "summary": "Governed supporting and opposing evidence has not been recorded.",
                    "weight": 1,
                    "source_link": {},
                }]},
                "legacy": True,
            }
        )
    return hypotheses


def _translate(error: HypothesisError) -> HTTPException:
    status = 404 if str(error) == "hypothesis not found" else 400
    return HTTPException(status_code=status, detail=str(error))


@router.get("/api/hypotheses", dependencies=[Depends(require_login)])
def list_hypotheses(request: Request, include_archived: bool = False):
    return {
        "hypotheses": SqliteHypothesisRepository().list(include_archived=include_archived),
        "can_edit": session_role(request) == "admin",
        "guardrail": (
            "Hypotheses are tentative and separate from diagnoses. "
            "Only an attributable clinician review can confirm or rule against one."
        ),
    }


@router.get("/api/hypotheses/{hypothesis_id}", dependencies=[Depends(require_login)])
def get_hypothesis(hypothesis_id: str):
    result = SqliteHypothesisRepository().get(hypothesis_id)
    if not result:
        raise HTTPException(status_code=404, detail="hypothesis not found")
    return result


@router.post("/api/hypotheses", dependencies=[Depends(require_admin)])
def create_hypothesis(request: Request, body: CreateHypothesisBody):
    try:
        return SqliteHypothesisRepository().create(body, _actor(request))
    except HypothesisError as error:
        raise _translate(error) from error


@router.put("/api/hypotheses/{hypothesis_id}/evidence", dependencies=[Depends(require_admin)])
def revise_hypothesis_evidence(
    request: Request,
    hypothesis_id: str,
    body: ReviseEvidenceBody,
):
    try:
        return SqliteHypothesisRepository().revise_evidence(
            hypothesis_id, body, _actor(request)
        )
    except HypothesisError as error:
        raise _translate(error) from error


@router.post("/api/hypotheses/{hypothesis_id}/transition", dependencies=[Depends(require_admin)])
def transition_hypothesis(
    request: Request,
    hypothesis_id: str,
    body: TransitionBody,
):
    try:
        return SqliteHypothesisRepository().transition(
            hypothesis_id, body, _actor(request)
        )
    except HypothesisError as error:
        raise _translate(error) from error
