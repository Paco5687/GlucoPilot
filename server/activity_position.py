"""Activity/position intervals and observational response analysis.

Manual and imported wearable intervals remain separate source observations.
At query time a manual interval takes precedence over an overlapping inferred
interval, but the inferred row is never updated or deleted.  All comparisons
are temporal associations and must not be interpreted as causal effects.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from . import db
from .analytics_confidence import mean_confidence
from .auth import current_user, require_admin, require_login, session_role
from .config import APP_TIMEZONE, OWNER_EMAIL
from .data_contracts import DEPLOYMENT_OWNER_ID
from .insulin_response import build_response_events
from .repositories import get_repositories


router = APIRouter(dependencies=[Depends(require_login)])

ALGORITHM_VERSION = "activity-position-analysis/1.0.0"
INTERVAL_MODEL_VERSION = "activity-position-interval/1.0.0"
ACTIVITY_STATES = {"resting", "walking", "other", "unknown"}
POSITION_STATES = {"sitting", "standing", "lying", "upright", "unknown"}
ORIGIN_KINDS = {"manual", "wearable"}
MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_DAYS = 7
COMPANION_MINIMUM_SAMPLES = 14
COMPANION_MINIMUM_CONFIDENCE = 0.50


class ActivityPositionError(ValueError):
    """Raised when an interval or correction is not safe to persist."""


class IntervalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_time: datetime
    end_time: datetime
    activity: str = "unknown"
    position: str = "unknown"
    notes: str = Field(default="", max_length=1_000)


class CorrectionBody(IntervalBody):
    reason: str = Field(min_length=1, max_length=2_000)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _number(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _choice(value: Any, allowed: set[str], label: str) -> str:
    normalized = str(value or "unknown").strip().lower()
    if normalized not in allowed:
        raise ActivityPositionError(
            f"{label} must be one of: {', '.join(sorted(allowed))}"
        )
    return normalized


def _validated_interval(
    start: datetime | str,
    end: datetime | str,
) -> tuple[str, str]:
    first = _instant(start)
    last = _instant(end)
    if first is None or last is None:
        raise ActivityPositionError(
            "interval endpoints require timezone-aware ISO timestamps"
        )
    duration = last - first
    if duration < timedelta(minutes=MIN_INTERVAL_MINUTES):
        raise ActivityPositionError("interval must be at least one minute")
    if duration > timedelta(days=MAX_INTERVAL_DAYS):
        raise ActivityPositionError("interval cannot exceed seven days")
    return _iso(first), _iso(last)


def _actor(request: Request) -> str:
    role = session_role(request)
    user = current_user(role, request.session.get("provider_name", ""))
    return " ".join(str(user.get("full_name") or role).split())[:240]


def _overlap(first: dict[str, Any], second: dict[str, Any]) -> bool:
    return (
        str(first["start_time"]) < str(second["end_time"])
        and str(second["start_time"]) < str(first["end_time"])
    )


def resolve_intervals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Annotate precedence without destroying or mutating any source interval."""
    ordered = sorted(
        rows,
        key=lambda row: (
            str(row.get("start_time") or ""),
            str(row.get("end_time") or ""),
            str(row.get("created_at") or ""),
            str(row.get("id") or ""),
        ),
    )
    corrected_ids = {
        str(row.get("correction_of_id"))
        for row in ordered
        if row.get("correction_of_id")
    }
    all_manual = [
        row for row in ordered if row.get("origin_kind") == "manual"
    ]
    manual = [
        row
        for row in all_manual
        if str(row.get("id")) not in corrected_ids
    ]
    output = []
    for row in ordered:
        overridden_by = []
        start = _instant(row.get("start_time"))
        end = _instant(row.get("end_time"))
        segments = [(start, end)] if start is not None and end is not None else []
        if row.get("origin_kind") == "wearable":
            overlapping_manual = [
                candidate for candidate in manual if _overlap(row, candidate)
            ]
            overridden_by = [
                str(candidate["id"]) for candidate in overlapping_manual
            ]
            for candidate in overlapping_manual:
                candidate_start = _instant(candidate.get("start_time"))
                candidate_end = _instant(candidate.get("end_time"))
                if candidate_start is None or candidate_end is None:
                    continue
                remaining = []
                for segment_start, segment_end in segments:
                    if (
                        candidate_end <= segment_start
                        or candidate_start >= segment_end
                    ):
                        remaining.append((segment_start, segment_end))
                        continue
                    if segment_start < candidate_start:
                        remaining.append((segment_start, candidate_start))
                    if candidate_end < segment_end:
                        remaining.append((candidate_end, segment_end))
                segments = remaining
        if str(row.get("id")) in corrected_ids:
            corrections = [
                candidate
                for candidate in all_manual
                if str(candidate.get("correction_of_id") or "") == str(row.get("id"))
            ]
            overridden_by.extend(str(candidate["id"]) for candidate in corrections)
            segments = []
        coverage_status = (
            "overridden"
            if not segments
            else "partially_overridden"
            if overridden_by
            else "active"
        )
        output.append(
            {
                **row,
                "effective": bool(segments),
                "effective_segments": [
                    {"start_time": _iso(segment_start), "end_time": _iso(segment_end)}
                    for segment_start, segment_end in segments
                ],
                "overridden_by": sorted(set(overridden_by)),
                "coverage_status": coverage_status,
                "precedence": "manual_over_wearable",
            }
        )
    return output


def _interval_for(
    timestamp: datetime,
    intervals: list[dict[str, Any]],
) -> dict[str, Any] | None:
    candidates = []
    for row in intervals:
        start = _instant(row.get("start_time"))
        end = _instant(row.get("end_time"))
        if start is None or end is None or not start <= timestamp < end:
            continue
        confidence = (row.get("confidence") or {}).get("confidence_score")
        score = _number(confidence) or 0
        candidates.append(
            (
                row.get("origin_kind") == "manual",
                score,
                -((end - start).total_seconds()),
                str(row.get("created_at") or ""),
                str(row.get("id") or ""),
                row,
            )
        )
    return max(candidates, default=(None, None, None, None, None, None))[-1]


def _glucose_slopes(
    intervals: list[dict[str, Any]],
    glucose: list[dict[str, Any]],
    *,
    timezone_name: str,
) -> list[dict[str, Any]]:
    points = []
    for row in glucose:
        timestamp = _instant(row.get("timestamp"))
        value = _number(row.get("value"))
        if timestamp is not None and value is not None:
            points.append((timestamp, value, row))
    points.sort(key=lambda item: (item[0], str(item[2].get("id") or "")))
    point_times = [item[0] for item in points]
    tz = ZoneInfo(timezone_name)
    output = []
    for interval in intervals:
        start = _instant(interval.get("start_time"))
        end = _instant(interval.get("end_time"))
        if start is None or end is None:
            continue
        left = bisect.bisect_left(point_times, start)
        right = bisect.bisect_right(point_times, end)
        selected = points[left:right]
        if len(selected) >= 2:
            elapsed_hours = (selected[-1][0] - selected[0][0]).total_seconds() / 3600
            if elapsed_hours > 0:
                output.append(
                    {
                        "metric": "glucose_slope_mg_dl_per_hour",
                        "value": (selected[-1][1] - selected[0][1]) / elapsed_hours,
                        "date": selected[0][0].astimezone(tz).date().isoformat(),
                        "interval_id": interval["id"],
                        "source_refs": [
                            ("GlucoseReading", str(selected[0][2].get("id") or "")),
                            ("GlucoseReading", str(selected[-1][2].get("id") or "")),
                        ],
                    }
                )
        morning = [
            point
            for point in selected
            if 4 <= point[0].astimezone(tz).hour < 12
        ]
        if len(morning) >= 2:
            elapsed_hours = (morning[-1][0] - morning[0][0]).total_seconds() / 3600
            if elapsed_hours > 0:
                output.append(
                    {
                        "metric": "morning_glucose_slope_mg_dl_per_hour",
                        "value": (morning[-1][1] - morning[0][1]) / elapsed_hours,
                        "date": morning[0][0].astimezone(tz).date().isoformat(),
                        "interval_id": interval["id"],
                        "source_refs": [
                            ("GlucoseReading", str(morning[0][2].get("id") or "")),
                            ("GlucoseReading", str(morning[-1][2].get("id") or "")),
                        ],
                    }
                )
    return output


def _event_measurements(
    intervals: list[dict[str, Any]],
    response_events: list[dict[str, Any]],
    fingersticks: list[dict[str, Any]],
    *,
    timezone_name: str,
) -> list[dict[str, Any]]:
    tz = ZoneInfo(timezone_name)
    output = []
    for event in response_events:
        if event.get("classification") != "clean":
            continue
        timestamp = _instant((event.get("observed") or {}).get("bolus", {}).get("timestamp"))
        value = _number(
            (event.get("calculations") or {}).get("nadir_drop_per_unit_mg_dl")
        )
        interval = _interval_for(timestamp, intervals) if timestamp else None
        if timestamp is None or value is None or interval is None:
            continue
        observed = event.get("observed") or {}
        response_sources = []
        for entity_type, source in (
            ("Treatment", observed.get("bolus")),
            ("GlucoseReading", observed.get("start_glucose")),
            ("GlucoseReading", observed.get("end_glucose")),
            ("GlucoseReading", observed.get("nadir_glucose")),
        ):
            if isinstance(source, dict) and source.get("id"):
                response_sources.append((entity_type, str(source["id"])))
        output.append(
            {
                "metric": "bolus_response_mg_dl_per_unit",
                "value": value,
                "date": timestamp.astimezone(tz).date().isoformat(),
                "interval_id": interval["id"],
                "source_refs": response_sources,
            }
        )
    for row in fingersticks:
        timestamp = _instant(row.get("timestamp"))
        value = _number(row.get("delta", row.get("paired_delta_mg_dl")))
        interval = _interval_for(timestamp, intervals) if timestamp else None
        if timestamp is None or value is None or interval is None:
            continue
        output.append(
            {
                "metric": "cgm_minus_fingerstick_mg_dl",
                "value": value,
                "date": timestamp.astimezone(tz).date().isoformat(),
                "interval_id": interval["id"],
                "source_refs": [
                    ("FingerstickReading", str(row.get("id") or "")),
                ],
            }
        )
    return output


_METRIC_UNITS = {
    "glucose_slope_mg_dl_per_hour": "mg/dL/hour",
    "morning_glucose_slope_mg_dl_per_hour": "mg/dL/hour",
    "bolus_response_mg_dl_per_unit": "mg/dL/unit",
    "cgm_minus_fingerstick_mg_dl": "mg/dL",
}


def build_analysis(
    interval_rows: list[dict[str, Any]],
    glucose: list[dict[str, Any]],
    fingersticks: list[dict[str, Any]],
    response_events: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
    timezone_name: str = "UTC",
) -> dict[str, Any]:
    resolved = resolve_intervals(interval_rows)
    # Corrected rows are retained in `intervals`. Wearable rows are split around
    # manual overlap so their non-overlapping source time can still contribute.
    active = [
        {
            **row,
            "start_time": segment["start_time"],
            "end_time": segment["end_time"],
        }
        for row in resolved
        for segment in row["effective_segments"]
    ]
    measurements = [
        *_glucose_slopes(active, glucose, timezone_name=timezone_name),
        *_event_measurements(
            active,
            response_events,
            fingersticks,
            timezone_name=timezone_name,
        ),
    ]
    by_id = {str(row["id"]): row for row in active}
    expected_days = max(1, (end.date() - start.date()).days + 1)
    effects = []
    for dimension in ("activity", "position"):
        states = sorted(
            {
                str(row.get(dimension) or "unknown")
                for row in active
                if str(row.get(dimension) or "unknown") != "unknown"
            }
        )
        for state in states:
            state_intervals = [
                row for row in active if str(row.get(dimension) or "unknown") == state
            ]
            state_interval_ids = {
                str(row["id"]) for row in state_intervals
            }
            for metric in _METRIC_UNITS:
                samples = [
                    item
                    for item in measurements
                    if item["metric"] == metric
                    and str((by_id.get(str(item["interval_id"])) or {}).get(dimension))
                    == state
                ]
                if not samples:
                    continue
                valid_days = len({item["date"] for item in samples})
                confidence = mean_confidence(
                    [item["value"] for item in samples],
                    valid_days=valid_days,
                    expected_days=expected_days,
                    temporal_direction="within-recorded-interval",
                    unit=_METRIC_UNITS[metric],
                )
                qualifies = (
                    confidence["sample_count"] >= COMPANION_MINIMUM_SAMPLES
                    and confidence["discovery_status"] in {"emerging", "reproduced"}
                    and confidence["confidence_score"] >= COMPANION_MINIMUM_CONFIDENCE
                )
                source_refs = sorted(
                    {
                        reference
                        for sample in samples
                        for reference in [
                            ("ActivityPositionInterval", str(sample["interval_id"])),
                            *sample["source_refs"],
                        ]
                        if reference[1]
                    }
                )
                identity = {
                    "algorithm_version": ALGORITHM_VERSION,
                    "dimension": dimension,
                    "state": state,
                    "metric": metric,
                    "interval_ids": sorted(
                        {str(sample["interval_id"]) for sample in samples}
                    ),
                    "samples": sorted(
                        (
                            sample["date"],
                            round(float(sample["value"]), 6),
                            str(sample["interval_id"]),
                            tuple(sorted(sample["source_refs"])),
                        )
                        for sample in samples
                    ),
                    "source_refs": source_refs,
                }
                effects.append(
                    {
                        "id": "activity-position-effect-"
                        + _hash(identity).removeprefix("sha256:")[:32],
                        "algorithm_version": ALGORITHM_VERSION,
                        "semantic_class": "derived_observational_association",
                        "dimension": dimension,
                        "state": state,
                        "metric": metric,
                        "observed_mean": confidence["effect_size"]["value"],
                        "unit": _METRIC_UNITS[metric],
                        "sample_count": confidence["sample_count"],
                        "interval_count": len(state_interval_ids),
                        "measured_interval_count": len(
                            {str(sample["interval_id"]) for sample in samples}
                        ),
                        "interval_missingness": {
                            "available_intervals": len(state_interval_ids),
                            "measured_intervals": len(
                                {str(sample["interval_id"]) for sample in samples}
                            ),
                            "missing_intervals": max(
                                0,
                                len(state_interval_ids)
                                - len(
                                    {
                                        str(sample["interval_id"])
                                        for sample in samples
                                    }
                                ),
                            ),
                            "missing_rate": round(
                                max(
                                    0,
                                    len(state_interval_ids)
                                    - len(
                                        {
                                            str(sample["interval_id"])
                                            for sample in samples
                                        }
                                    ),
                                )
                                / len(state_interval_ids),
                                4,
                            )
                            if state_interval_ids
                            else None,
                        },
                        "analytics_confidence": confidence,
                        "replication_status": confidence["replication"]["status"],
                        "qualifies_for_companion": qualifies,
                        "language": {
                            "lead": confidence["language"]["lead"],
                            "association_only": True,
                            "causal_allowed": False,
                            "definitive_allowed": False,
                            "statement": (
                                f"During recorded {dimension}={state} intervals, the "
                                f"observed mean {metric.replace('_', ' ')} was "
                                f"{confidence['effect_size']['value']} {_METRIC_UNITS[metric]}. "
                                "This temporal association does not establish that activity or "
                                "position caused the observed response."
                            ),
                        },
                        "source_refs": [
                            {"entity_type": kind, "entity_id": entity_id}
                            for kind, entity_id in source_refs
                        ],
                        "input_hash": _hash(identity),
                    }
                )
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "interval_model_version": INTERVAL_MODEL_VERSION,
        "input_data_version": _hash(
            {
                "intervals": interval_rows,
                "glucose": glucose,
                "fingersticks": fingersticks,
                "response_events": response_events,
                "start": _iso(start),
                "end": _iso(end),
            }
        ),
        "semantics": {
            "observed": "Manual and wearable source intervals remain separate observations.",
            "calculated": "Metrics are deterministic calculations within recorded intervals.",
            "association": "Temporal association only; causation is not established.",
            "correction": (
                "Manual intervals take precedence over overlapping inferred intervals; "
                "the inferred source interval remains retained and visible."
            ),
        },
        "range": {"start": _iso(start), "end": _iso(end)},
        "counts": {
            "intervals": len(resolved),
            "effective_intervals": len(
                {str(row["id"]) for row in active}
            ),
            "manual_intervals": sum(
                row.get("origin_kind") == "manual" for row in resolved
            ),
            "wearable_intervals": sum(
                row.get("origin_kind") == "wearable" for row in resolved
            ),
            "overridden_intervals": sum(
                row["coverage_status"] != "active" for row in resolved
            ),
            "effects": len(effects),
            "companion_qualifying_effects": sum(
                effect["qualifies_for_companion"] for effect in effects
            ),
        },
        "intervals": resolved,
        "effects": effects,
        "qualifying_effects": [
            effect for effect in effects if effect["qualifies_for_companion"]
        ],
    }


class SqliteActivityPositionRepository:
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
    def _public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        value.pop("owner_id", None)
        value.pop("owner_email", None)
        value["confidence"] = json.loads(value.pop("confidence_json"))
        return value

    def list(
        self,
        start: datetime | str,
        end: datetime | str,
    ) -> list[dict[str, Any]]:
        lower = _iso(start if isinstance(start, datetime) else _instant(start) or datetime.min.replace(tzinfo=timezone.utc))
        upper = _iso(end if isinstance(end, datetime) else _instant(end) or datetime.max.replace(tzinfo=timezone.utc))
        with self._scope() as connection:
            rows = connection.execute(
                """
                SELECT * FROM activity_position_intervals
                WHERE owner_id=? AND start_time < ? AND end_time > ?
                ORDER BY start_time,end_time,created_at,id
                """,
                (DEPLOYMENT_OWNER_ID, upper, lower),
            ).fetchall()
        return [self._public(row) for row in rows]

    def get(self, interval_id: str) -> dict[str, Any] | None:
        with self._scope() as connection:
            row = connection.execute(
                """
                SELECT * FROM activity_position_intervals
                WHERE id=? AND owner_id=?
                """,
                (interval_id, DEPLOYMENT_OWNER_ID),
            ).fetchone()
        return self._public(row) if row else None

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        interval_id: str,
        action: str,
        actor_label: str,
        reason: str,
        before: dict[str, Any],
        after: dict[str, Any],
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO activity_position_events (
                id,interval_id,action,actor_role,actor_label,reason,
                before_json,after_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                f"activity_position_event_{uuid.uuid4().hex}",
                interval_id,
                action,
                "admin" if action in {"created", "corrected"} else "system",
                actor_label,
                reason,
                _canonical(before),
                _canonical(after),
                created_at,
            ),
        )

    def create_manual(
        self,
        body: IntervalBody,
        *,
        actor_label: str,
        correction_of_id: str | None = None,
        reason: str = "Manual interval recorded",
    ) -> dict[str, Any]:
        start, end = _validated_interval(body.start_time, body.end_time)
        activity = _choice(body.activity, ACTIVITY_STATES, "activity")
        position = _choice(body.position, POSITION_STATES, "position")
        if activity == "unknown" and position == "unknown":
            raise ActivityPositionError(
                "record at least one activity or position state"
            )
        now = _now()
        interval_id = f"activity_position_{uuid.uuid4().hex}"
        confidence = {
            "version": INTERVAL_MODEL_VERSION,
            "confidence_score": None,
            "confidence_label": "not_assessed",
            "method": "manual_report",
            "limitations": [
                "This is a patient-recorded interval and has not been independently verified."
            ],
        }
        row = {
            "id": interval_id,
            "owner_id": DEPLOYMENT_OWNER_ID,
            "owner_email": OWNER_EMAIL,
            "start_time": start,
            "end_time": end,
            "activity": activity,
            "position": position,
            "origin_kind": "manual",
            "origin_label": actor_label,
            "source_entity_type": None,
            "source_entity_id": None,
            "source_input_hash": _hash(
                {
                    "start": start,
                    "end": end,
                    "activity": activity,
                    "position": position,
                    "actor": actor_label,
                    "created_at": now,
                    "interval_id": interval_id,
                }
            ),
            "confidence_json": _canonical(confidence),
            "correction_of_id": correction_of_id,
            "notes": " ".join(body.notes.split()),
            "created_at": now,
            "updated_at": now,
        }
        with self._scope() as connection:
            before: dict[str, Any] = {}
            if correction_of_id:
                original = connection.execute(
                    """
                    SELECT * FROM activity_position_intervals
                    WHERE id=? AND owner_id=?
                    """,
                    (correction_of_id, DEPLOYMENT_OWNER_ID),
                ).fetchone()
                if not original:
                    raise ActivityPositionError("interval to correct was not found")
                before = self._public(original)
            connection.execute(
                """
                INSERT INTO activity_position_intervals (
                    id,owner_id,owner_email,start_time,end_time,activity,position,
                    origin_kind,origin_label,source_entity_type,source_entity_id,
                    source_input_hash,confidence_json,correction_of_id,notes,
                    created_at,updated_at
                ) VALUES (
                    :id,:owner_id,:owner_email,:start_time,:end_time,:activity,:position,
                    :origin_kind,:origin_label,:source_entity_type,:source_entity_id,
                    :source_input_hash,:confidence_json,:correction_of_id,:notes,
                    :created_at,:updated_at
                )
                """,
                row,
            )
            public = self._public(row)
            self._event(
                connection,
                interval_id,
                "corrected" if correction_of_id else "created",
                actor_label,
                reason,
                before,
                public,
                now,
            )
        return public

    def upsert_wearable(
        self,
        *,
        start_time: datetime | str,
        end_time: datetime | str,
        activity: str,
        position: str = "unknown",
        source_label: str,
        source_entity_type: str,
        source_entity_id: str,
        source_payload: dict[str, Any],
        confidence_score: float,
        limitations: list[str],
    ) -> dict[str, Any]:
        start, end = _validated_interval(start_time, end_time)
        normalized_activity = _choice(activity, ACTIVITY_STATES, "activity")
        normalized_position = _choice(position, POSITION_STATES, "position")
        score = max(0.0, min(1.0, float(confidence_score)))
        source_hash = _hash(
            {
                "source": source_label,
                "source_entity_type": source_entity_type,
                "source_entity_id": source_entity_id,
                "payload": source_payload,
                "model_version": INTERVAL_MODEL_VERSION,
            }
        )
        confidence = {
            "version": INTERVAL_MODEL_VERSION,
            "confidence_score": round(score, 4),
            "confidence_label": "high" if score >= 0.85 else "medium" if score >= 0.60 else "low",
            "method": "wearable_interval_inference",
            "limitations": limitations,
        }
        now = _now()
        with self._scope() as connection:
            existing = connection.execute(
                """
                SELECT * FROM activity_position_intervals
                WHERE owner_id=? AND origin_kind='wearable'
                  AND source_input_hash=?
                """,
                (DEPLOYMENT_OWNER_ID, source_hash),
            ).fetchone()
            if existing:
                return self._public(existing)
            interval_id = f"activity_position_{uuid.uuid4().hex}"
            row = {
                "id": interval_id,
                "owner_id": DEPLOYMENT_OWNER_ID,
                "owner_email": OWNER_EMAIL,
                "start_time": start,
                "end_time": end,
                "activity": normalized_activity,
                "position": normalized_position,
                "origin_kind": "wearable",
                "origin_label": source_label[:240],
                "source_entity_type": source_entity_type[:120],
                "source_entity_id": source_entity_id[:500],
                "source_input_hash": source_hash,
                "confidence_json": _canonical(confidence),
                "correction_of_id": None,
                "notes": "",
                "created_at": now,
                "updated_at": now,
            }
            connection.execute(
                """
                INSERT INTO activity_position_intervals (
                    id,owner_id,owner_email,start_time,end_time,activity,position,
                    origin_kind,origin_label,source_entity_type,source_entity_id,
                    source_input_hash,confidence_json,correction_of_id,notes,
                    created_at,updated_at
                ) VALUES (
                    :id,:owner_id,:owner_email,:start_time,:end_time,:activity,:position,
                    :origin_kind,:origin_label,:source_entity_type,:source_entity_id,
                    :source_input_hash,:confidence_json,:correction_of_id,:notes,
                    :created_at,:updated_at
                )
                """,
                row,
            )
            public = self._public(row)
            self._event(
                connection,
                interval_id,
                "inferred",
                "Google Health sync",
                "Timestamped step interval inferred as walking under the declared limitation.",
                {},
                public,
                now,
            )
            return public

    def history(self, interval_id: str) -> list[dict[str, Any]]:
        with self._scope() as connection:
            rows = connection.execute(
                """
                SELECT * FROM activity_position_events
                WHERE interval_id=?
                   OR interval_id IN (
                       SELECT id FROM activity_position_intervals
                       WHERE correction_of_id=?
                   )
                ORDER BY created_at,id
                """,
                (interval_id, interval_id),
            ).fetchall()
        return [
            self._public_event(row)
            for row in rows
        ]

    @staticmethod
    def _public_event(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["before"] = json.loads(value.pop("before_json"))
        value["after"] = json.loads(value.pop("after_json"))
        return value


def _paged_entity(
    entity_type: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    repository = get_repositories().entity(entity_type)
    output = []
    skip = 0
    lower = _iso(start)
    upper = _iso(end)
    while True:
        page = repository.query(
            {
                "owner_email": OWNER_EMAIL,
                "timestamp": {"$gte": lower, "$lte": upper},
            },
            "timestamp",
            5_000,
            skip,
        )
        output.extend(page)
        if len(page) < 5_000:
            return output
        skip += 5_000


def _connection_entity_rows(
    connection: sqlite3.Connection,
    entity_type: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT id,data FROM entities
        WHERE type=?
          AND json_extract(data, '$.owner_email')=?
          AND json_extract(data, '$.timestamp') >= ?
          AND json_extract(data, '$.timestamp') <= ?
        ORDER BY json_extract(data, '$.timestamp'),id
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


def analysis_for_range(
    start: datetime,
    end: datetime,
    *,
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    timezone_name = db.config_value("app_timezone", APP_TIMEZONE)
    repository = SqliteActivityPositionRepository(connection)
    intervals = repository.list(start, end)
    if not intervals:
        return build_analysis(
            [],
            [],
            [],
            [],
            start=start,
            end=end,
            timezone_name=timezone_name,
        )
    if connection is None:
        glucose = _paged_entity("GlucoseReading", start, end)
        fingersticks = _paged_entity("FingerstickReading", start, end)
        treatments = _paged_entity(
            "Treatment", start - timedelta(hours=4), end + timedelta(hours=3)
        )
    else:
        glucose = _connection_entity_rows(
            connection, "GlucoseReading", start, end
        )
        fingersticks = _connection_entity_rows(
            connection, "FingerstickReading", start, end
        )
        treatments = _connection_entity_rows(
            connection,
            "Treatment",
            start - timedelta(hours=4),
            end + timedelta(hours=3),
        )
    response = build_response_events(
        treatments,
        glucose,
        fingersticks=fingersticks,
        timezone_name=timezone_name,
        event_start=start,
        event_end=end,
    )
    return build_analysis(
        intervals,
        glucose,
        fingersticks,
        response["events"],
        start=start,
        end=end,
        timezone_name=timezone_name,
    )


@router.get("/api/activity-position")
def get_activity_position(request: Request, days: int = 90):
    bounded_days = max(1, min(int(days), 365))
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=bounded_days)
    result = analysis_for_range(start, end)
    return {
        **result,
        "can_edit": session_role(request) == "admin",
    }


@router.get("/api/activity-position/intervals/{interval_id}")
def get_activity_position_interval(interval_id: str):
    repository = SqliteActivityPositionRepository()
    item = repository.get(interval_id)
    if not item:
        raise HTTPException(status_code=404, detail="Activity/position interval not found")
    return {
        **item,
        "history": repository.history(interval_id),
    }


@router.post("/api/activity-position/intervals", dependencies=[Depends(require_admin)])
def create_activity_position_interval(body: IntervalBody, request: Request):
    try:
        return SqliteActivityPositionRepository().create_manual(
            body,
            actor_label=_actor(request),
        )
    except ActivityPositionError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post(
    "/api/activity-position/intervals/{interval_id}/corrections",
    dependencies=[Depends(require_admin)],
)
def correct_activity_position_interval(
    interval_id: str,
    body: CorrectionBody,
    request: Request,
):
    try:
        return SqliteActivityPositionRepository().create_manual(
            body,
            actor_label=_actor(request),
            correction_of_id=interval_id,
            reason=" ".join(body.reason.split()),
        )
    except ActivityPositionError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def report_block(days: int = 90) -> dict[str, Any]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=max(1, min(days, 365)))
    result = analysis_for_range(start, end)
    return {
        "algorithm_version": result["algorithm_version"],
        "semantics": result["semantics"],
        "counts": result["counts"],
        "effects": result["effects"],
        "qualifying_effects": result["qualifying_effects"],
    }
