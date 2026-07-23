"""Canonical health episodes and medication exposure intervals.

Episode membership is temporal context only. It never encodes or implies
causation. Manual, rule, and model proposals share one guarded lifecycle;
corrections and decisions remain attributable in append-only event ledgers.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any, Iterator

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import db
from .analytics_confidence import ANALYTICS_CONFIDENCE_VERSION
from .auth import current_user, require_admin, require_login, session_role
from .config import OWNER_EMAIL
from .data_contracts import DEPLOYMENT_OWNER_ID


router = APIRouter()
ORIGINS = {"manual", "rule", "model"}
STATUSES = {"proposed", "confirmed", "dismissed"}
MEMBER_TYPES = {
    "SymptomLog": ("symptom", "entry_date"),
    "GlucoseReading": ("glucose_event", "timestamp"),
    "PeriodLog": ("cycle_day", "date"),
    "Treatment": ("treatment", "timestamp"),
    "HistoryEntry": ("history_event", "entry_date"),
}
RELATIONSHIPS = {"temporal_overlap", "within_episode", "near_episode"}
MEMBER_ROLES = {
    "symptom",
    "glucose_event",
    "cycle_day",
    "treatment",
    "history_event",
    "medication_exposure",
    "context",
}


class EpisodeError(RuntimeError):
    pass


class MemberBody(BaseModel):
    entity_type: str
    entity_id: str
    role: str | None = None
    relationship_kind: str = "temporal_overlap"
    observed_start: str
    observed_end: str | None = None
    source_version: str = "manual"
    summary: str = ""


class EpisodeCreateBody(BaseModel):
    episode_type: str
    title: str
    description: str = ""
    origin_kind: str = "manual"
    origin_label: str = ""
    start_time: str
    end_time: str
    confidence_score: float | None = Field(default=None, ge=0, le=1)
    members: list[MemberBody] = Field(default_factory=list, max_length=100)


class EpisodeCorrectionBody(BaseModel):
    reason: str
    episode_type: str | None = None
    title: str | None = None
    description: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    confidence_score: float | None = Field(default=None, ge=0, le=1)
    members: list[MemberBody] | None = Field(default=None, max_length=100)


class DecisionBody(BaseModel):
    status: str
    reason: str


class ExposureCreateBody(BaseModel):
    medication_entity_id: str | None = None
    medication_name: str
    dose: str = ""
    formulation: str = ""
    frequency: str = ""
    start_time: str
    end_time: str | None = None
    origin_kind: str = "manual"
    origin_label: str = ""
    confidence_score: float | None = Field(default=None, ge=0, le=1)


class ExposureCorrectionBody(BaseModel):
    reason: str
    medication_name: str | None = None
    dose: str | None = None
    formulation: str | None = None
    frequency: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    confidence_score: float | None = Field(default=None, ge=0, le=1)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _text(value: Any, limit: int, *, required: bool = False, label: str = "value") -> str:
    result = " ".join(str(value or "").split())
    if required and not result:
        raise EpisodeError(f"{label} is required")
    if len(result) > limit:
        raise EpisodeError(f"{label} exceeds {limit} characters")
    return result


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _time_value(value: str, label: str) -> tuple[str, str]:
    raw = _text(value, 40, required=True, label=label)
    if len(raw) == 10:
        try:
            date.fromisoformat(raw)
        except ValueError as error:
            raise EpisodeError(f"{label} must be an ISO date or timezone-aware timestamp") from error
        return raw, "date"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise EpisodeError(f"{label} must be an ISO date or timezone-aware timestamp") from error
    if parsed.tzinfo is None:
        raise EpisodeError(f"{label} timestamps require an explicit timezone")
    precision = "second" if parsed.second or parsed.microsecond else "minute"
    normalized = parsed.astimezone(timezone.utc).isoformat(
        timespec="seconds" if precision == "second" else "minutes"
    ).replace("+00:00", "Z")
    return normalized, precision


def _interval(start: str, end: str | None, *, open_end: bool = False) -> tuple[str, str | None, str]:
    start_value, precision = _time_value(start, "start time")
    if end in (None, "") and open_end:
        return start_value, None, precision
    end_value, end_precision = _time_value(end or "", "end time")
    if precision != end_precision:
        raise EpisodeError("interval endpoints must use the same time precision")
    if start_value > end_value:
        raise EpisodeError("interval start must not be after its end")
    return start_value, end_value, precision


def _confidence(origin: str, score: float | None, sample_count: int) -> dict[str, Any]:
    if score is not None and (not math.isfinite(score) or not 0 <= score <= 1):
        raise EpisodeError("confidence score must be between 0 and 1")
    if origin == "manual" and score is None:
        label = "not_assessed"
    else:
        score = 0.0 if score is None else round(score, 4)
        label = "high" if score >= 0.85 else "medium" if score >= 0.60 else "low"
    return {
        "version": ANALYTICS_CONFIDENCE_VERSION,
        "confidence_score": score,
        "confidence_label": label,
        "sample_count": sample_count,
        "method": "reported" if origin == "manual" else "episode-detector",
        "language": {
            "definitive_allowed": False,
            "causal_allowed": False,
            "lead": "Temporal co-occurrence only; this episode does not establish causation.",
        },
    }


def _origin(kind: str, label: str, actor: dict[str, str]) -> tuple[str, str]:
    origin = _text(kind, 20).lower()
    if origin not in ORIGINS:
        raise EpisodeError("origin must be manual, rule, or model")
    origin_label = _text(label, 240)
    if origin != "manual" and not origin_label:
        raise EpisodeError("rule and model origins require an attributable version label")
    return origin, origin_label or actor["label"]


def _actor(request: Request) -> dict[str, str]:
    role = session_role(request)
    user = current_user(role, request.session.get("provider_name", ""))
    return {"role": role, "label": _text(user.get("full_name"), 240) or role}


def _members(values: list[MemberBody | dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    seen = set()
    for value in values:
        raw = value.model_dump() if isinstance(value, MemberBody) else dict(value)
        entity_type = _text(raw.get("entity_type"), 80, required=True, label="member type")
        if entity_type not in MEMBER_TYPES and entity_type != "MedicationExposure":
            raise EpisodeError("unsupported episode member type")
        entity_id = _text(raw.get("entity_id"), 500, required=True, label="member identity")
        key = (entity_type, entity_id)
        if key in seen:
            raise EpisodeError("episode members must be unique")
        seen.add(key)
        start, end, _ = _interval(
            str(raw.get("observed_start") or ""),
            str(raw.get("observed_end") or raw.get("observed_start") or ""),
        )
        relationship = _text(raw.get("relationship_kind"), 40).lower()
        if relationship not in RELATIONSHIPS:
            raise EpisodeError("unsupported temporal relationship")
        default_role = MEMBER_TYPES.get(entity_type, ("medication_exposure", ""))[0]
        role = _text(raw.get("role"), 40).lower() or default_role
        if role not in MEMBER_ROLES:
            raise EpisodeError("unsupported episode member role")
        output.append({
            "entity_type": entity_type,
            "entity_id": entity_id,
            "role": role,
            "relationship_kind": relationship,
            "observed_start": start,
            "observed_end": end,
            "source_version": _text(
                raw.get("source_version"), 500, required=True, label="source version"
            ),
            "summary": _text(raw.get("summary"), 1_000),
        })
    return output


def _state(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row[key]
        for key in (
            "episode_type", "title", "description", "status", "start_time", "end_time",
            "time_precision", "confidence_json", "membership_revision", "input_hash",
            "decided_by", "decided_at", "decision_reason",
        )
    }


class SqliteEpisodeRepository:
    def __init__(self, connection: sqlite3.Connection | None = None) -> None:
        self.connection = connection

    @contextmanager
    def _scope(self) -> Iterator[sqlite3.Connection]:
        if self.connection is not None:
            yield self.connection
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
    def _insert_members(connection, episode_id, revision, members, now):
        for ordinal, member in enumerate(members):
            connection.execute(
                """
                INSERT INTO episode_members (
                    id, episode_id, membership_revision, ordinal, entity_type, entity_id,
                    member_role, relationship_kind, observed_start, observed_end,
                    source_version, summary, causation_asserted, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    f"episode_member_{uuid.uuid4().hex}", episode_id, revision, ordinal,
                    member["entity_type"], member["entity_id"], member["role"],
                    member["relationship_kind"], member["observed_start"],
                    member["observed_end"], member["source_version"], member["summary"], now,
                ),
            )

    @staticmethod
    def _validate_members(connection, members):
        for member in members:
            if member["entity_type"] == "MedicationExposure":
                row = connection.execute(
                    "SELECT 1 FROM medication_exposures WHERE id=? AND owner_id=?",
                    (member["entity_id"], DEPLOYMENT_OWNER_ID),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT 1 FROM entities
                    WHERE id=? AND type=? AND json_extract(data, '$.owner_email')=?
                    """,
                    (member["entity_id"], member["entity_type"], OWNER_EMAIL),
                ).fetchone()
            if not row:
                raise EpisodeError(
                    f"{member['entity_type']} member {member['entity_id']} was not found"
                )

    @staticmethod
    def _event(connection, episode_id, action, actor, reason, before, after, now):
        connection.execute(
            """
            INSERT INTO episode_events (
                id, episode_id, action, actor_role, actor_label, reason,
                before_json, after_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"episode_event_{uuid.uuid4().hex}", episode_id, action, actor["role"],
                actor["label"], reason, _canonical(before), _canonical(after), now,
            ),
        )

    @staticmethod
    def _public(connection, row, events=False):
        result = dict(row)
        result.pop("owner_id", None)
        result.pop("owner_email", None)
        result["confidence"] = json.loads(result.pop("confidence_json"))
        member_rows = connection.execute(
            """
            SELECT entity_type, entity_id, member_role, relationship_kind,
                   observed_start, observed_end, source_version, summary, causation_asserted
            FROM episode_members WHERE episode_id=? AND membership_revision=? ORDER BY ordinal
            """,
            (result["id"], result["membership_revision"]),
        ).fetchall()
        result["members"] = [
            {
                "entity_type": item["entity_type"],
                "entity_id": item["entity_id"],
                "role": item["member_role"],
                "relationship_kind": item["relationship_kind"],
                "observed_start": item["observed_start"],
                "observed_end": item["observed_end"],
                "source_version": item["source_version"],
                "summary": item["summary"],
                "causation_asserted": bool(item["causation_asserted"]),
            }
            for item in member_rows
        ]
        if events:
            result["events"] = [
                {
                    **{key: event[key] for key in event.keys() if key not in {"before_json", "after_json"}},
                    "before": json.loads(event["before_json"]),
                    "after": json.loads(event["after_json"]),
                }
                for event in connection.execute(
                    "SELECT * FROM episode_events WHERE episode_id=? ORDER BY created_at,id",
                    (result["id"],),
                ).fetchall()
            ]
        return result

    def create(self, body: EpisodeCreateBody, actor: dict[str, str]) -> dict[str, Any]:
        origin, origin_label = _origin(body.origin_kind, body.origin_label, actor)
        start, end, precision = _interval(body.start_time, body.end_time)
        members = _members(body.members)
        confidence = _confidence(origin, body.confidence_score, len(members))
        now = _now()
        episode_id = f"episode_{uuid.uuid4().hex}"
        revision = 1 if members else 0
        input_hash = _hash({"start": start, "end": end, "members": members})
        with self._scope() as connection:
            self._validate_members(connection, members)
            connection.execute(
                """
                INSERT INTO health_episodes (
                    id, owner_id, owner_email, episode_type, title, description,
                    origin_kind, origin_label, status, start_time, end_time,
                    time_precision, confidence_json, association_only,
                    membership_revision, input_hash, created_at, updated_at,
                    decided_by, decided_at, decision_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?, ?, 1, ?, ?, ?, ?, NULL, NULL, '')
                """,
                (
                    episode_id, DEPLOYMENT_OWNER_ID, OWNER_EMAIL,
                    _text(body.episode_type, 120, required=True, label="episode type"),
                    _text(body.title, 240, required=True, label="episode title"),
                    _text(body.description, 4_000), origin, origin_label, start, end, precision,
                    _canonical(confidence), revision, input_hash, now, now,
                ),
            )
            self._insert_members(connection, episode_id, revision, members, now)
            row = connection.execute("SELECT * FROM health_episodes WHERE id=?", (episode_id,)).fetchone()
            self._event(
                connection, episode_id, "created", actor,
                "Episode recorded as a temporal association; causation is not asserted.",
                {}, _state(dict(row)), now,
            )
            return self._public(connection, row, events=True)

    def list(self, include_dismissed=False):
        with self._scope() as connection:
            where = "owner_id=?" + ("" if include_dismissed else " AND status!='dismissed'")
            rows = connection.execute(
                f"SELECT * FROM health_episodes WHERE {where} ORDER BY start_time DESC,id",
                (DEPLOYMENT_OWNER_ID,),
            ).fetchall()
            return [self._public(connection, row) for row in rows]

    def correct(self, episode_id, body: EpisodeCorrectionBody, actor):
        reason = _text(body.reason, 2_000, required=True, label="correction reason")
        now = _now()
        with self._scope() as connection:
            row = connection.execute(
                "SELECT * FROM health_episodes WHERE id=? AND owner_id=?",
                (episode_id, DEPLOYMENT_OWNER_ID),
            ).fetchone()
            if not row:
                raise EpisodeError("episode not found")
            current = dict(row)
            if current["status"] != "proposed":
                raise EpisodeError("decided episodes cannot be corrected")
            start, end, precision = _interval(
                body.start_time or current["start_time"], body.end_time or current["end_time"]
            )
            members = (
                _members(body.members)
                if body.members is not None
                else _members(self._public(connection, row)["members"])
            )
            revision = current["membership_revision"] + 1 if body.members is not None else current["membership_revision"]
            confidence = _confidence(
                current["origin_kind"], body.confidence_score, len(members)
            ) if body.confidence_score is not None or body.members is not None else json.loads(current["confidence_json"])
            before = _state(current)
            input_hash = (
                _hash({"start": start, "end": end, "members": members})
                if body.members is not None
                or start != current["start_time"]
                or end != current["end_time"]
                else current["input_hash"]
            )
            if body.members is not None:
                self._validate_members(connection, members)
            connection.execute(
                """
                UPDATE health_episodes SET episode_type=?,title=?,description=?,
                    start_time=?,end_time=?,time_precision=?,confidence_json=?,
                    membership_revision=?,input_hash=?,updated_at=? WHERE id=?
                """,
                (
                    _text(body.episode_type, 120) or current["episode_type"],
                    _text(body.title, 240) or current["title"],
                    current["description"] if body.description is None else _text(body.description, 4_000),
                    start, end, precision, _canonical(confidence), revision, input_hash, now, episode_id,
                ),
            )
            if body.members is not None:
                self._insert_members(connection, episode_id, revision, members, now)
            updated = connection.execute("SELECT * FROM health_episodes WHERE id=?", (episode_id,)).fetchone()
            action = "members_revised" if body.members is not None else "corrected"
            self._event(
                connection, episode_id, action, actor, reason, before, _state(dict(updated)), now
            )
            return self._public(connection, updated, events=True)

    def get(self, episode_id):
        with self._scope() as connection:
            row = connection.execute(
                "SELECT * FROM health_episodes WHERE id=? AND owner_id=?",
                (episode_id, DEPLOYMENT_OWNER_ID),
            ).fetchone()
            if not row:
                raise EpisodeError("episode not found")
            return self._public(connection, row, events=True)

    def decide(self, episode_id, body: DecisionBody, actor):
        target = _text(body.status, 20).lower()
        reason = _text(body.reason, 2_000, required=True, label="decision reason")
        if target not in {"confirmed", "dismissed"}:
            raise EpisodeError("episode decision must be confirmed or dismissed")
        now = _now()
        with self._scope() as connection:
            row = connection.execute(
                "SELECT * FROM health_episodes WHERE id=? AND owner_id=?",
                (episode_id, DEPLOYMENT_OWNER_ID),
            ).fetchone()
            if not row:
                raise EpisodeError("episode not found")
            if row["status"] != "proposed":
                raise EpisodeError("episode already has a terminal decision")
            before = _state(dict(row))
            connection.execute(
                "UPDATE health_episodes SET status=?,decided_by=?,decided_at=?,decision_reason=?,updated_at=? WHERE id=?",
                (target, actor["label"], now, reason, now, episode_id),
            )
            updated = connection.execute("SELECT * FROM health_episodes WHERE id=?", (episode_id,)).fetchone()
            self._event(connection, episode_id, target, actor, reason, before, _state(dict(updated)), now)
            return self._public(connection, updated, events=True)


class SqliteExposureRepository:
    @staticmethod
    def _public(connection, row, events=False):
        result = dict(row)
        result.pop("owner_id", None)
        result.pop("owner_email", None)
        result["confidence"] = json.loads(result.pop("confidence_json"))
        if events:
            result["events"] = [
                {
                    **{key: event[key] for key in event.keys() if key not in {"before_json", "after_json"}},
                    "before": json.loads(event["before_json"]),
                    "after": json.loads(event["after_json"]),
                }
                for event in connection.execute(
                    "SELECT * FROM medication_exposure_events WHERE exposure_id=? ORDER BY created_at,id",
                    (result["id"],),
                ).fetchall()
            ]
        return result

    @staticmethod
    def _state(row):
        return {key: row[key] for key in (
            "medication_name", "dose", "formulation", "frequency", "start_time", "end_time",
            "time_precision", "status", "confidence_json", "decided_by", "decided_at", "decision_reason",
        )}

    @staticmethod
    def _event(connection, exposure_id, action, actor, reason, before, after, now):
        connection.execute(
            """
            INSERT INTO medication_exposure_events (
                id,exposure_id,action,actor_role,actor_label,reason,before_json,after_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                f"exposure_event_{uuid.uuid4().hex}", exposure_id, action, actor["role"],
                actor["label"], reason, _canonical(before), _canonical(after), now,
            ),
        )

    def create(self, body: ExposureCreateBody, actor):
        origin, origin_label = _origin(body.origin_kind, body.origin_label, actor)
        start, end, precision = _interval(body.start_time, body.end_time, open_end=True)
        now = _now()
        exposure_id = f"exposure_{uuid.uuid4().hex}"
        with db.connect() as connection:
            medication_entity_id = _text(body.medication_entity_id, 500) or None
            if medication_entity_id and not connection.execute(
                """
                SELECT 1 FROM entities
                WHERE id=? AND type='Medication' AND json_extract(data, '$.owner_email')=?
                """,
                (medication_entity_id, OWNER_EMAIL),
            ).fetchone():
                raise EpisodeError("linked medication was not found")
            connection.execute(
                """
                INSERT INTO medication_exposures (
                    id,owner_id,owner_email,medication_entity_id,medication_name,dose,
                    formulation,frequency,start_time,end_time,time_precision,origin_kind,
                    origin_label,status,confidence_json,created_at,updated_at,
                    decided_by,decided_at,decision_reason
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'proposed',?,?,?,NULL,NULL,'')
                """,
                (
                    exposure_id, DEPLOYMENT_OWNER_ID, OWNER_EMAIL,
                    medication_entity_id,
                    _text(body.medication_name, 240, required=True, label="medication name"),
                    _text(body.dose, 240), _text(body.formulation, 240),
                    _text(body.frequency, 240), start, end, precision, origin, origin_label,
                    _canonical(_confidence(origin, body.confidence_score, 1)), now, now,
                ),
            )
            row = connection.execute("SELECT * FROM medication_exposures WHERE id=?", (exposure_id,)).fetchone()
            self._event(
                connection, exposure_id, "created", actor,
                "Medication exposure interval recorded as proposed.", {}, self._state(row), now,
            )
            return self._public(connection, row, events=True)

    def get(self, exposure_id):
        with db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM medication_exposures WHERE id=? AND owner_id=?",
                (exposure_id, DEPLOYMENT_OWNER_ID),
            ).fetchone()
            if not row:
                raise EpisodeError("medication exposure not found")
            return self._public(connection, row, events=True)

    def list(self, include_dismissed=False):
        with db.connect() as connection:
            where = "owner_id=?" + ("" if include_dismissed else " AND status!='dismissed'")
            rows = connection.execute(
                f"SELECT * FROM medication_exposures WHERE {where} ORDER BY start_time DESC,id",
                (DEPLOYMENT_OWNER_ID,),
            ).fetchall()
            return [self._public(connection, row) for row in rows]

    def correct(self, exposure_id, body: ExposureCorrectionBody, actor):
        reason = _text(body.reason, 2_000, required=True, label="correction reason")
        now = _now()
        with db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM medication_exposures WHERE id=? AND owner_id=?",
                (exposure_id, DEPLOYMENT_OWNER_ID),
            ).fetchone()
            if not row:
                raise EpisodeError("medication exposure not found")
            if row["status"] != "proposed":
                raise EpisodeError("decided medication exposures cannot be corrected")
            start, end, precision = _interval(
                body.start_time or row["start_time"],
                body.end_time if "end_time" in body.model_fields_set else row["end_time"],
                open_end=True,
            )
            before = self._state(row)
            confidence = (
                _confidence(row["origin_kind"], body.confidence_score, 1)
                if body.confidence_score is not None
                else json.loads(row["confidence_json"])
            )
            connection.execute(
                """
                UPDATE medication_exposures SET medication_name=?,dose=?,formulation=?,
                    frequency=?,start_time=?,end_time=?,time_precision=?,confidence_json=?,
                    updated_at=? WHERE id=?
                """,
                (
                    _text(body.medication_name, 240) or row["medication_name"],
                    row["dose"] if body.dose is None else _text(body.dose, 240),
                    row["formulation"] if body.formulation is None else _text(body.formulation, 240),
                    row["frequency"] if body.frequency is None else _text(body.frequency, 240),
                    start, end, precision, _canonical(confidence), now, exposure_id,
                ),
            )
            updated = connection.execute("SELECT * FROM medication_exposures WHERE id=?", (exposure_id,)).fetchone()
            self._event(connection, exposure_id, "corrected", actor, reason, before, self._state(updated), now)
            return self._public(connection, updated, events=True)

    def decide(self, exposure_id, body: DecisionBody, actor):
        target = _text(body.status, 20).lower()
        reason = _text(body.reason, 2_000, required=True, label="decision reason")
        if target not in {"confirmed", "dismissed"}:
            raise EpisodeError("exposure decision must be confirmed or dismissed")
        now = _now()
        with db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM medication_exposures WHERE id=? AND owner_id=?",
                (exposure_id, DEPLOYMENT_OWNER_ID),
            ).fetchone()
            if not row:
                raise EpisodeError("medication exposure not found")
            if row["status"] != "proposed":
                raise EpisodeError("medication exposure already has a terminal decision")
            before = self._state(row)
            connection.execute(
                "UPDATE medication_exposures SET status=?,decided_by=?,decided_at=?,decision_reason=?,updated_at=? WHERE id=?",
                (target, actor["label"], now, reason, now, exposure_id),
            )
            updated = connection.execute("SELECT * FROM medication_exposures WHERE id=?", (exposure_id,)).fetchone()
            self._event(connection, exposure_id, target, actor, reason, before, self._state(updated), now)
            return self._public(connection, updated, events=True)


def temporal_candidates(start: str, end: str, limit: int = 200) -> list[dict[str, Any]]:
    start_value, end_value, precision = _interval(start, end)
    output = []
    for entity_type, (role, field) in MEMBER_TYPES.items():
        rows = db.query_entities(entity_type, {"owner_email": OWNER_EMAIL}, "-created_date", 5_000)
        for row in rows:
            value = str(row.get(field) or "")
            if not value:
                continue
            if precision == "date":
                comparable = value[:10]
            else:
                try:
                    comparable, candidate_precision = _time_value(value, "candidate time")
                except EpisodeError:
                    continue
                if candidate_precision == "date":
                    continue
            if start_value <= comparable <= end_value:
                output.append({
                    "entity_type": entity_type,
                    "entity_id": row["id"],
                    "role": role,
                    "relationship_kind": "within_episode",
                    "observed_start": comparable,
                    "observed_end": comparable,
                    "source_version": str(row.get("updated_date") or row.get("created_date") or "legacy"),
                    "summary": _text(
                        row.get("title") or row.get("type") or row.get("event_type")
                        or row.get("phase") or entity_type,
                        1_000,
                    ),
                    "causation_asserted": 0,
                })
    with db.connect() as connection:
        exposure_rows = connection.execute(
            """
            SELECT id,medication_name,dose,start_time,end_time,updated_at
            FROM medication_exposures
            WHERE owner_id=? AND status!='dismissed'
            ORDER BY start_time,id
            """,
            (DEPLOYMENT_OWNER_ID,),
        ).fetchall()
    for row in exposure_rows:
        if precision == "date":
            exposure_start = row["start_time"][:10]
            exposure_end = row["end_time"][:10] if row["end_time"] else None
            if exposure_start > end_value or (
                exposure_end is not None and exposure_end < start_value
            ):
                continue
            observed_start = max(start_value, exposure_start)
            observed_end = min(end_value, exposure_end or end_value)
        else:
            try:
                exposure_start, exposure_precision = _time_value(
                    row["start_time"], "exposure start"
                )
                if row["end_time"]:
                    exposure_end, end_precision = _time_value(
                        row["end_time"], "exposure end"
                    )
                else:
                    exposure_end, end_precision = None, exposure_precision
            except EpisodeError:
                continue
            if exposure_precision == "date" or end_precision == "date":
                continue
            range_start = datetime.fromisoformat(start_value.replace("Z", "+00:00"))
            range_end = datetime.fromisoformat(end_value.replace("Z", "+00:00"))
            candidate_start = datetime.fromisoformat(exposure_start.replace("Z", "+00:00"))
            candidate_end = (
                datetime.fromisoformat(exposure_end.replace("Z", "+00:00"))
                if exposure_end
                else None
            )
            if candidate_start > range_end or (
                candidate_end is not None and candidate_end < range_start
            ):
                continue
            timespec = "seconds" if precision == "second" else "minutes"
            observed_start = max(range_start, candidate_start).isoformat(
                timespec=timespec
            ).replace("+00:00", "Z")
            observed_end = min(range_end, candidate_end or range_end).isoformat(
                timespec=timespec
            ).replace("+00:00", "Z")
        output.append({
            "entity_type": "MedicationExposure",
            "entity_id": row["id"],
            "role": "medication_exposure",
            "relationship_kind": "temporal_overlap",
            "observed_start": observed_start,
            "observed_end": observed_end,
            "source_version": row["updated_at"],
            "summary": _text(
                f"{row['medication_name']} {row['dose'] or ''}",
                1_000,
            ),
            "causation_asserted": 0,
        })
    output.sort(key=lambda item: (item["observed_start"], item["entity_type"], item["entity_id"]))
    if len(output) > limit:
        raise EpisodeError("temporal candidate count exceeds the bounded limit; narrow the interval")
    return output


def report_block() -> dict[str, Any]:
    return {
        "episodes": SqliteEpisodeRepository().list(),
        "medication_exposures": SqliteExposureRepository().list(),
        "semantics": "Temporal membership and co-occurrence do not establish causation.",
    }


def _http(error: EpisodeError):
    return HTTPException(
        status_code=404 if "not found" in str(error) else 400,
        detail=str(error),
    )


@router.get("/api/episodes", dependencies=[Depends(require_login)])
def list_episodes(request: Request, include_dismissed: bool = False):
    return {
        "episodes": SqliteEpisodeRepository().list(include_dismissed),
        "can_edit": session_role(request) == "admin",
        "causation_asserted": False,
    }


@router.get("/api/episodes/candidates", dependencies=[Depends(require_login)])
def episode_candidates(start: str, end: str, limit: int = 200):
    try:
        return {"candidates": temporal_candidates(start, end, min(200, max(1, limit)))}
    except EpisodeError as error:
        raise _http(error) from error


@router.get("/api/episodes/{episode_id}", dependencies=[Depends(require_login)])
def get_episode(episode_id: str):
    try:
        return SqliteEpisodeRepository().get(episode_id)
    except EpisodeError as error:
        raise _http(error) from error


@router.post("/api/episodes", dependencies=[Depends(require_admin)])
def create_episode(request: Request, body: EpisodeCreateBody):
    try:
        return SqliteEpisodeRepository().create(body, _actor(request))
    except EpisodeError as error:
        raise _http(error) from error


@router.put("/api/episodes/{episode_id}", dependencies=[Depends(require_admin)])
def correct_episode(request: Request, episode_id: str, body: EpisodeCorrectionBody):
    try:
        return SqliteEpisodeRepository().correct(episode_id, body, _actor(request))
    except EpisodeError as error:
        raise _http(error) from error


@router.post("/api/episodes/{episode_id}/decision", dependencies=[Depends(require_admin)])
def decide_episode(request: Request, episode_id: str, body: DecisionBody):
    try:
        return SqliteEpisodeRepository().decide(episode_id, body, _actor(request))
    except EpisodeError as error:
        raise _http(error) from error


@router.get("/api/medication-exposures", dependencies=[Depends(require_login)])
def list_exposures(request: Request, include_dismissed: bool = False):
    return {
        "medication_exposures": SqliteExposureRepository().list(include_dismissed),
        "can_edit": session_role(request) == "admin",
    }


@router.get("/api/medication-exposures/{exposure_id}", dependencies=[Depends(require_login)])
def get_exposure(exposure_id: str):
    try:
        return SqliteExposureRepository().get(exposure_id)
    except EpisodeError as error:
        raise _http(error) from error


@router.post("/api/medication-exposures", dependencies=[Depends(require_admin)])
def create_exposure(request: Request, body: ExposureCreateBody):
    try:
        return SqliteExposureRepository().create(body, _actor(request))
    except EpisodeError as error:
        raise _http(error) from error


@router.put("/api/medication-exposures/{exposure_id}", dependencies=[Depends(require_admin)])
def correct_exposure(request: Request, exposure_id: str, body: ExposureCorrectionBody):
    try:
        return SqliteExposureRepository().correct(exposure_id, body, _actor(request))
    except EpisodeError as error:
        raise _http(error) from error


@router.post("/api/medication-exposures/{exposure_id}/decision", dependencies=[Depends(require_admin)])
def decide_exposure(request: Request, exposure_id: str, body: DecisionBody):
    try:
        return SqliteExposureRepository().decide(exposure_id, body, _actor(request))
    except EpisodeError as error:
        raise _http(error) from error
