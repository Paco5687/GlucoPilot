"""Auditable diabetes-management effort metrics.

The ledger distinguishes source observations, deterministic inferences, manual
events, and append-only corrections. Scores describe measured effort only;
missing source families lower confidence and are never interpreted as zero
burden. Outcome/effort comparisons are descriptive and noncausal.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from . import db
from .analytics_confidence import mean_confidence
from .auth import current_user, require_admin, require_login, session_role
from .config import APP_TIMEZONE, OWNER_EMAIL
from .data_contracts import DEPLOYMENT_OWNER_ID


router = APIRouter(dependencies=[Depends(require_login)])
ALGORITHM_VERSION = "management-burden/1.0.0"
LEDGER_VERSION = "management-burden-event/1.0.0"
CATEGORIES = {
    "bolus",
    "override",
    "temp_basal",
    "pump_interaction",
    "fingerstick",
    "ketone",
    "rescue_carbs",
    "awakening",
    "device_change",
    "activity_for_control",
    "other",
}
DEFAULT_MINUTES = {
    "bolus": 2,
    "override": 3,
    "temp_basal": 3,
    "pump_interaction": 2,
    "fingerstick": 3,
    "ketone": 5,
    "rescue_carbs": 5,
    "awakening": 10,
    "device_change": 15,
    "activity_for_control": 15,
    "other": 3,
}
WEIGHTS = {
    "bolus": 1,
    "override": 2,
    "temp_basal": 2,
    "pump_interaction": 1,
    "fingerstick": 1,
    "ketone": 2,
    "rescue_carbs": 2,
    "awakening": 3,
    "device_change": 2,
    "activity_for_control": 2,
    "other": 1,
}
SOURCE_FAMILIES = {
    "pump_treatments": {"bolus", "override", "temp_basal", "pump_interaction", "device_change"},
    "fingersticks": {"fingerstick"},
    "ketones": {"ketone"},
    "rescue_carbs": {"rescue_carbs"},
    "overnight_management": {"awakening"},
    "activity_for_control": {"activity_for_control"},
}


class BurdenError(ValueError):
    pass


class ManualEventBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    occurred_at: datetime
    category: str
    duration_minutes: float | None = Field(default=None, ge=0, le=1440)
    interaction_count: int = Field(default=1, ge=0, le=1000)
    notes: str = Field(default="", max_length=1000)


class CorrectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str | None = None
    duration_minutes: float | None = Field(default=None, ge=0, le=1440)
    interaction_count: int | None = Field(default=None, ge=0, le=1000)
    excluded: bool = False
    reason: str = Field(min_length=1, max_length=2000)
    notes: str = Field(default="", max_length=1000)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _instant(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if result.tzinfo is None or result.utcoffset() is None:
        return None
    return result.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _category(value: Any) -> str:
    result = str(value or "").strip().lower()
    if result not in CATEGORIES:
        raise BurdenError(f"category must be one of: {', '.join(sorted(CATEGORIES))}")
    return result


def _actor(request: Request) -> str:
    role = session_role(request)
    user = current_user(role, request.session.get("provider_name", ""))
    return " ".join(str(user.get("full_name") or role).split())[:240]


def _entity_rows(
    connection: sqlite3.Connection,
    entity_type: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT id,data FROM entities
        WHERE type=? AND json_extract(data,'$.owner_email')=?
          AND json_extract(data,'$.timestamp') >= ?
          AND json_extract(data,'$.timestamp') <= ?
        ORDER BY json_extract(data,'$.timestamp'),id
        """,
        (entity_type, OWNER_EMAIL, _iso(start), _iso(end)),
    ).fetchall()
    output = []
    for row in rows:
        try:
            data = json.loads(row["data"])
        except (TypeError, ValueError):
            continue
        output.append({"id": row["id"], **data})
    return output


def _confidence(score: float, basis: str) -> dict[str, Any]:
    return {
        "confidence_score": round(max(0.0, min(1.0, score)), 3),
        "confidence_label": "high" if score >= 0.85 else "medium" if score >= 0.60 else "low",
        "basis": basis,
        "causal_allowed": False,
    }


def _classify_treatment(row: dict[str, Any]) -> list[tuple[str, str, float]]:
    kind = str(row.get("type") or "").lower()
    event = str(row.get("event_type") or "").lower()
    notes = str(row.get("notes") or "").lower()
    reason = str(row.get("reason") or "").lower()
    text = " ".join((kind, event, notes, reason))
    result: list[tuple[str, str, float]] = []
    if kind == "insulin" or "bolus" in event:
        result.append(("bolus", "recorded insulin/bolus treatment", 0.95))
    if kind in {"tempbasal", "basal"} or "temp basal" in text:
        result.append(("temp_basal", "recorded temporary basal interaction", 0.95))
    if kind == "suspension" or any(token in text for token in ("suspend", "resume")):
        result.append(("pump_interaction", "recorded pump suspend/resume interaction", 0.90))
    if any(token in text for token in ("override", "manual correction", "user correction")):
        result.append(("override", "recorded override language", 0.80))
    if any(token in text for token in ("site change", "sensor change", "cartridge", "reservoir", "cannula")):
        result.append(("device_change", "recorded device-change language", 0.80))
    return result


def _insert(
    connection: sqlite3.Connection,
    *,
    occurred_at: str,
    category: str,
    origin_kind: str,
    duration_minutes: float,
    interaction_count: int,
    confidence: dict[str, Any],
    source_entity_type: str | None,
    source_entity_id: str | None,
    identity: dict[str, Any],
    correction_of_id: str | None = None,
    excluded: bool = False,
    notes: str = "",
    actor_role: str = "system",
    actor_label: str = "GlucoPilot burden derivation",
    reason: str = "Deterministic burden event derivation.",
) -> dict[str, Any]:
    input_hash = _hash(identity)
    event_id = "burden-" + input_hash.removeprefix("sha256:")[:32]
    if origin_kind in {"manual", "correction"}:
        event_id = f"burden-{uuid.uuid4()}"
        input_hash = _hash({**identity, "event_id": event_id})
    created = _now()
    connection.execute(
        """
        INSERT OR IGNORE INTO management_burden_events (
            id,owner_id,occurred_at,category,origin_kind,source_entity_type,
            source_entity_id,source_input_hash,duration_minutes,interaction_count,
            confidence_json,correction_of_id,excluded,notes,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            DEPLOYMENT_OWNER_ID,
            occurred_at,
            category,
            origin_kind,
            source_entity_type,
            source_entity_id,
            input_hash,
            duration_minutes,
            interaction_count,
            _canonical(confidence),
            correction_of_id,
            int(excluded),
            notes,
            created,
        ),
    )
    row = connection.execute(
        "SELECT * FROM management_burden_events WHERE owner_id=? AND origin_kind=? AND source_input_hash=?",
        (DEPLOYMENT_OWNER_ID, origin_kind, input_hash),
    ).fetchone()
    public = _public(row)
    if connection.execute(
        "SELECT 1 FROM management_burden_audit WHERE burden_event_id=? LIMIT 1", (public["id"],)
    ).fetchone() is None:
        connection.execute(
            """
            INSERT INTO management_burden_audit (
                id,burden_event_id,action,actor_role,actor_label,reason,
                before_json,after_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                f"burden-audit-{uuid.uuid4()}",
                public["id"],
                "corrected" if origin_kind == "correction" else "created" if origin_kind == "manual" else "derived",
                actor_role,
                actor_label,
                reason,
                "{}",
                _canonical(public),
                created,
            ),
        )
    return public


def _public(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["confidence"] = json.loads(value.pop("confidence_json"))
    value["excluded"] = bool(value["excluded"])
    return value


def _derive(connection: sqlite3.Connection, start: datetime, end: datetime) -> None:
    treatments = _entity_rows(connection, "Treatment", start, end)
    glucose = _entity_rows(connection, "GlucoseReading", start - timedelta(minutes=20), end)
    fingersticks = _entity_rows(connection, "FingerstickReading", start, end)
    ketones = _entity_rows(connection, "KetoneReading", start, end)
    glucose_points = [
        (_instant(row.get("timestamp")), _number(row.get("value")), row)
        for row in glucose
    ]
    for row in treatments:
        occurred = _instant(row.get("timestamp"))
        if occurred is None:
            continue
        for category, basis, score in _classify_treatment(row):
            _insert(
                connection,
                occurred_at=_iso(occurred),
                category=category,
                origin_kind="observed",
                duration_minutes=DEFAULT_MINUTES[category],
                interaction_count=1,
                confidence=_confidence(score, basis),
                source_entity_type="Treatment",
                source_entity_id=str(row["id"]),
                identity={"version": LEDGER_VERSION, "category": category, "source": ("Treatment", row["id"]), "data": row},
            )
        if str(row.get("type") or "").lower() == "carb":
            low = any(
                point_time is not None
                and point_value is not None
                and point_value < 70
                and abs((point_time - occurred).total_seconds()) <= 20 * 60
                for point_time, point_value, _ in glucose_points
            )
            if low:
                _insert(
                    connection,
                    occurred_at=_iso(occurred),
                    category="rescue_carbs",
                    origin_kind="inferred",
                    duration_minutes=DEFAULT_MINUTES["rescue_carbs"],
                    interaction_count=1,
                    confidence=_confidence(0.80, "carbohydrate record within 20 minutes of observed glucose below 70 mg/dL"),
                    source_entity_type="Treatment",
                    source_entity_id=str(row["id"]),
                    identity={"version": LEDGER_VERSION, "category": "rescue_carbs", "source": ("Treatment", row["id"]), "low_context": True},
                )
    for row in fingersticks:
        occurred = _instant(row.get("timestamp"))
        if occurred is not None:
            _insert(
                connection,
                occurred_at=_iso(occurred),
                category="fingerstick",
                origin_kind="observed",
                duration_minutes=DEFAULT_MINUTES["fingerstick"],
                interaction_count=1,
                confidence=_confidence(1.0, "direct fingerstick record"),
                source_entity_type="FingerstickReading",
                source_entity_id=str(row["id"]),
                identity={"version": LEDGER_VERSION, "category": "fingerstick", "source": ("FingerstickReading", row["id"]), "data": row},
            )
    for row in ketones:
        occurred = _instant(row.get("timestamp"))
        if occurred is not None:
            _insert(
                connection,
                occurred_at=_iso(occurred),
                category="ketone",
                origin_kind="observed",
                duration_minutes=DEFAULT_MINUTES["ketone"],
                interaction_count=1,
                confidence=_confidence(1.0, "direct ketone record"),
                source_entity_type="KetoneReading",
                source_entity_id=str(row["id"]),
                identity={"version": LEDGER_VERSION, "category": "ketone", "source": ("KetoneReading", row["id"]), "data": row},
            )
    # Overnight events indicate management occurred overnight; they do not prove
    # sleep or a distinct awakening, hence the deliberately lower confidence.
    tz = ZoneInfo(db.config_value("app_timezone", APP_TIMEZONE))
    source_rows = connection.execute(
        """
        SELECT * FROM management_burden_events
        WHERE owner_id=? AND occurred_at>=? AND occurred_at<=?
          AND origin_kind IN ('observed','inferred')
          AND category NOT IN ('awakening','device_change')
        """,
        (DEPLOYMENT_OWNER_ID, _iso(start), _iso(end)),
    ).fetchall()
    by_night: dict[str, sqlite3.Row] = {}
    for row in source_rows:
        instant = _instant(row["occurred_at"])
        if instant is not None and 0 <= instant.astimezone(tz).hour < 5:
            by_night.setdefault(instant.astimezone(tz).date().isoformat(), row)
    for day, row in by_night.items():
        _insert(
            connection,
            occurred_at=row["occurred_at"],
            category="awakening",
            origin_kind="inferred",
            duration_minutes=DEFAULT_MINUTES["awakening"],
            interaction_count=1,
            confidence=_confidence(0.55, "management record between midnight and 05:00 local time; sleep state unobserved"),
            source_entity_type="ManagementBurdenEvent",
            source_entity_id=row["id"],
            identity={"version": LEDGER_VERSION, "category": "awakening", "local_date": day, "source": row["id"]},
        )
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='activity_position_intervals'"
    ).fetchone():
        rows = connection.execute(
            """
            SELECT * FROM activity_position_intervals
            WHERE owner_id=? AND start_time>=? AND start_time<=?
              AND origin_kind='manual'
            """,
            (DEPLOYMENT_OWNER_ID, _iso(start), _iso(end)),
        ).fetchall()
        for row in rows:
            text = str(row["notes"] or "").lower()
            if any(token in text for token in ("glucose", "control", "correction", "bring down", "treat high")):
                duration = max(
                    0,
                    ((_instant(row["end_time"]) or start) - (_instant(row["start_time"]) or start)).total_seconds() / 60,
                )
                _insert(
                    connection,
                    occurred_at=row["start_time"],
                    category="activity_for_control",
                    origin_kind="inferred",
                    duration_minutes=min(duration, 1440),
                    interaction_count=1,
                    confidence=_confidence(0.65, "manual activity interval contains explicit glucose-management language"),
                    source_entity_type="ActivityPositionInterval",
                    source_entity_id=row["id"],
                    identity={"version": LEDGER_VERSION, "category": "activity_for_control", "source": row["id"], "notes": row["notes"]},
                )


def _resolve(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    corrections: dict[str, dict[str, Any]] = {}
    for row in rows:
        target = row.get("correction_of_id")
        if target and (
            target not in corrections
            or (row["created_at"], row["id"]) > (corrections[target]["created_at"], corrections[target]["id"])
        ):
            corrections[target] = row
    output = []
    for row in rows:
        if row["origin_kind"] == "correction":
            continue
        replacement = corrections.get(row["id"])
        effective = replacement or row
        output.append(
            {
                **effective,
                "original_event_id": row["id"],
                "corrected_by": replacement["id"] if replacement else None,
                "effective": not effective["excluded"],
            }
        )
    return output


def _available_families(connection: sqlite3.Connection, rows: list[dict[str, Any]]) -> dict[str, bool]:
    categories = {row["category"] for row in rows}
    entity_types = {
        row["type"]
        for row in connection.execute(
            "SELECT DISTINCT type FROM entities WHERE json_extract(data,'$.owner_email')=?",
            (OWNER_EMAIL,),
        ).fetchall()
    }
    return {
        "pump_treatments": "Treatment" in entity_types,
        "fingersticks": "FingerstickReading" in entity_types,
        "ketones": "KetoneReading" in entity_types or "ketone" in categories,
        "rescue_carbs": "Treatment" in entity_types and "GlucoseReading" in entity_types,
        "overnight_management": bool(categories - {"awakening"}),
        "activity_for_control": "ActivityPositionInterval" in {row.get("source_entity_type") for row in rows}
        or bool(connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='activity_position_intervals'").fetchone()),
    }


def analysis_for_range(
    start: datetime,
    end: datetime,
    *,
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    own = connection is None
    scope = db.connect() if own else None
    conn = scope.__enter__() if scope else connection
    try:
        if own:
            conn.execute("BEGIN IMMEDIATE")
        _derive(conn, start, end)
        stored = [
            _public(row)
            for row in conn.execute(
                """
                SELECT * FROM management_burden_events
                WHERE owner_id=? AND occurred_at>=? AND occurred_at<=?
                ORDER BY occurred_at,id
                """,
                (DEPLOYMENT_OWNER_ID, _iso(start), _iso(end)),
            ).fetchall()
        ]
        resolved = _resolve(stored)
        effective = [row for row in resolved if row["effective"]]
        timezone_name = db.config_value("app_timezone", APP_TIMEZONE)
        tz = ZoneInfo(timezone_name)
        expected_days = max(1, (end.astimezone(tz).date() - start.astimezone(tz).date()).days + 1)
        daily: dict[str, dict[str, float]] = defaultdict(lambda: {"minutes": 0, "weighted": 0, "interactions": 0})
        components: dict[str, dict[str, float]] = defaultdict(lambda: {"events": 0, "minutes": 0, "interactions": 0, "weighted_points": 0})
        for row in effective:
            instant = _instant(row["occurred_at"])
            if instant is None:
                continue
            day = instant.astimezone(tz).date().isoformat()
            category = row["category"]
            minutes = float(row["duration_minutes"])
            interactions = int(row["interaction_count"])
            points = WEIGHTS[category] * interactions
            daily[day]["minutes"] += minutes
            daily[day]["weighted"] += points
            daily[day]["interactions"] += interactions
            components[category]["events"] += 1
            components[category]["minutes"] += minutes
            components[category]["interactions"] += interactions
            components[category]["weighted_points"] += points
        available = _available_families(conn, stored)
        coverage = sum(available.values()) / len(available)
        daily_minutes = [item["minutes"] for item in daily.values()]
        confidence = mean_confidence(
            daily_minutes,
            valid_days=len(daily),
            expected_days=expected_days,
            temporal_direction="repeated-observation",
            unit="minutes/day",
        )
        event_confidence = (
            sum(float(row["confidence"]["confidence_score"]) for row in effective) / len(effective)
            if effective
            else 0
        )
        confidence["source_coverage"] = {
            "available": [name for name, value in available.items() if value],
            "missing": [name for name, value in available.items() if not value],
            "available_count": sum(available.values()),
            "expected_count": len(available),
            "coverage_rate": round(coverage, 3),
        }
        confidence["event_confidence_mean"] = round(event_confidence, 3)
        confidence["confidence_score"] = round(confidence["confidence_score"] * coverage * event_confidence, 3)
        confidence["confidence_label"] = "high" if confidence["confidence_score"] >= 0.85 else "medium" if confidence["confidence_score"] >= 0.60 else "low"
        glucose = _entity_rows(conn, "GlucoseReading", start, end)
        values = [value for row in glucose if (value := _number(row.get("value"))) is not None]
        tir = round(sum(70 <= value <= 180 for value in values) / len(values) * 100, 1) if values else None
        tbr = round(sum(value < 70 for value in values) / len(values) * 100, 1) if values else None
        tar = round(sum(value > 180 for value in values) / len(values) * 100, 1) if values else None
        average_minutes = round(sum(daily_minutes) / expected_days, 1)
        weighted_per_day = sum(item["weighted"] for item in daily.values()) / expected_days
        effort_index = round(min(100, weighted_per_day * 10), 1)
        sustainability = bool(tir is not None and tir >= 70 and (effort_index >= 60 or average_minutes >= 45))
        result = {
            "algorithm_version": ALGORITHM_VERSION,
            "semantic_class": "calculated_descriptive_management_effort",
            "window": {"start": _iso(start), "end": _iso(end), "expected_days": expected_days},
            "summary": {
                "measured_effort_index": effort_index,
                "average_active_management_minutes_per_day": average_minutes,
                "measured_interactions_per_day": round(sum(item["interactions"] for item in daily.values()) / expected_days, 1),
                "days_with_measured_effort": len(daily),
                "event_count": len(effective),
            },
            "components": [
                {"category": category, **{key: round(value, 1) for key, value in values.items()}, "weight": WEIGHTS[category]}
                for category, values in sorted(components.items())
            ],
            "source_coverage": confidence["source_coverage"],
            "analytics_confidence": confidence,
            "outcomes": {
                "time_in_range_pct": tir,
                "time_below_range_pct": tbr,
                "time_above_range_pct": tar,
                "glucose_reading_count": len(values),
            },
            "outcome_vs_effort": {
                "sustainability_review_flag": sustainability,
                "language": (
                    "Target-range outcomes coexist with high measured effort; sustainability may deserve review."
                    if sustainability
                    else "Control and measured effort are shown separately; neither explains or causes the other."
                ),
                "causal_allowed": False,
            },
            "events": list(reversed(resolved[-200:])),
            "event_count": len(resolved),
            "language": {
                "observed_only": "This score describes recorded and explicitly inferred work, not total lived burden.",
                "missing_sources": "Unavailable sources lower confidence and are not counted as zero effort.",
                "clinical": "This is descriptive context, not a treatment recommendation or causal conclusion.",
            },
        }
        if own:
            conn.commit()
        return result
    except Exception:
        if own:
            conn.rollback()
        raise
    finally:
        if scope:
            scope.__exit__(None, None, None)


def _bounded(days: int) -> tuple[datetime, datetime]:
    end = datetime.now(timezone.utc)
    return end - timedelta(days=max(1, min(int(days), 365))), end


@router.get("/api/management-burden")
def get_management_burden(request: Request, days: int = 90):
    start, end = _bounded(days)
    return {**analysis_for_range(start, end), "can_edit": session_role(request) == "admin"}


@router.post("/api/management-burden/events", dependencies=[Depends(require_admin)])
def create_manual_event(body: ManualEventBody, request: Request):
    occurred = _instant(body.occurred_at)
    if occurred is None:
        raise HTTPException(status_code=400, detail="occurred_at requires a timezone-aware timestamp")
    try:
        category = _category(body.category)
    except BurdenError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        event = _insert(
            connection,
            occurred_at=_iso(occurred),
            category=category,
            origin_kind="manual",
            duration_minutes=body.duration_minutes if body.duration_minutes is not None else DEFAULT_MINUTES[category],
            interaction_count=body.interaction_count,
            confidence=_confidence(1.0, "manual burden event"),
            source_entity_type=None,
            source_entity_id=None,
            identity={"version": LEDGER_VERSION, "manual": True, "occurred_at": _iso(occurred), "category": category},
            notes=" ".join(body.notes.split()),
            actor_role="admin",
            actor_label=_actor(request),
            reason="Manual burden event recorded.",
        )
        connection.commit()
    return event


@router.post(
    "/api/management-burden/events/{event_id}/corrections",
    dependencies=[Depends(require_admin)],
)
def correct_event(event_id: str, body: CorrectionBody, request: Request):
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM management_burden_events WHERE id=? AND owner_id=?",
            (event_id, DEPLOYMENT_OWNER_ID),
        ).fetchone()
        if row is None:
            connection.rollback()
            raise HTTPException(status_code=404, detail="Burden event not found")
        original = _public(row)
        try:
            category = _category(body.category or original["category"])
        except BurdenError as error:
            connection.rollback()
            raise HTTPException(status_code=400, detail=str(error)) from error
        event = _insert(
            connection,
            occurred_at=original["occurred_at"],
            category=category,
            origin_kind="correction",
            duration_minutes=body.duration_minutes if body.duration_minutes is not None else original["duration_minutes"],
            interaction_count=body.interaction_count if body.interaction_count is not None else original["interaction_count"],
            confidence=_confidence(1.0, "admin correction"),
            source_entity_type=None,
            source_entity_id=None,
            identity={"version": LEDGER_VERSION, "correction_of": event_id, "reason": body.reason},
            correction_of_id=event_id,
            excluded=body.excluded,
            notes=" ".join(body.notes.split()) or original["notes"],
            actor_role="admin",
            actor_label=_actor(request),
            reason=" ".join(body.reason.split()),
        )
        connection.commit()
    return event


def report_block(days: int = 90) -> dict[str, Any]:
    start, end = _bounded(days)
    result = analysis_for_range(start, end)
    result.pop("events", None)
    return result
