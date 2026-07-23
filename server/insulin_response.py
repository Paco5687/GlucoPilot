"""Versioned, reproducible insulin response events.

The builder keeps observed source facts separate from calculations and from
interpretation.  It never claims that a bolus caused a glucose change or that a
calculated response proves insulin resistance or absorption behavior.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timezone
from statistics import mean, median, pstdev
from typing import Any
from zoneinfo import ZoneInfo


ALGORITHM_VERSION = "insulin-response/1.0.0"
RESPONSE_MINUTES = 120
START_TOLERANCE_MINUTES = 10
END_TOLERANCE_MINUTES = 10
MIN_RESPONSE_POINTS = 18
MIN_BOLUS_UNITS = 0.5
MIN_START_GLUCOSE_MG_DL = 100
CARB_GUARD_PRE_MINUTES = 20
IOB_ACTION_MINUTES = 240
CONTEXT_TOLERANCE_MINUTES = 15

ASSUMPTIONS = {
    "response_window_minutes": RESPONSE_MINUTES,
    "start_glucose_tolerance_minutes": START_TOLERANCE_MINUTES,
    "end_glucose_tolerance_minutes": END_TOLERANCE_MINUTES,
    "minimum_response_points": MIN_RESPONSE_POINTS,
    "minimum_bolus_units": MIN_BOLUS_UNITS,
    "minimum_start_glucose_mg_dl": MIN_START_GLUCOSE_MG_DL,
    "carb_window": {
        "minutes_before_bolus": CARB_GUARD_PRE_MINUTES,
        "minutes_after_bolus": RESPONSE_MINUTES,
    },
    "iob": {
        "method": "linear_decay",
        "action_duration_minutes": IOB_ACTION_MINUTES,
        "includes": "prior recorded boluses only",
        "limitations": (
            "This is a comparison assumption, not pump-reported IOB. It does not "
            "model basal insulin, insulin type, personal action curves, or dose absorption."
        ),
    },
}

_HARD_EXCLUSIONS = {
    "invalid_bolus_amount",
    "dose_below_minimum",
    "missing_start_glucose",
    "start_glucose_below_analysis_minimum",
    "missing_end_glucose",
    "insufficient_cgm_coverage",
}


def _instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _checksum(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _canonical_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make input snapshot hashes independent of repository return order."""
    return sorted(rows, key=lambda row: (_checksum(row), str(row.get("id") or "")))


def _source_snapshot(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in ("id", *fields)
        if row.get(key) not in (None, "")
    }


def _nearest(
    points: list[tuple[float, float, dict[str, Any]]],
    times: list[float],
    target: float,
    tolerance_minutes: int,
) -> tuple[float, float, dict[str, Any]] | None:
    index = bisect.bisect_left(times, target)
    best = None
    best_distance = None
    for candidate in (index - 1, index):
        if not 0 <= candidate < len(points):
            continue
        distance = abs(points[candidate][0] - target)
        if distance <= tolerance_minutes * 60 and (
            best_distance is None or distance < best_distance
        ):
            best = points[candidate]
            best_distance = distance
    return best


def _time_bucket(local: datetime) -> str:
    if local.hour < 6:
        return "overnight"
    if local.hour < 12:
        return "morning"
    if local.hour < 18:
        return "afternoon"
    return "evening"


def _day_activity(rows: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {"providers": []}
    step_values = []
    for row in rows:
        steps = _number(row.get("activity_steps", row.get("steps")))
        provider = str(row.get("source") or row.get("provider") or "").strip() or "unknown"
        item = {"provider": provider, "entity_id": row.get("id"), "steps": steps}
        output["providers"].append(item)
        if steps is not None:
            step_values.append(steps)
    output["providers"].sort(
        key=lambda item: (
            str(item["provider"]),
            str(item.get("entity_id") or ""),
            item["steps"] is None,
            item["steps"] or 0,
        )
    )
    output["step_range"] = (
        {"minimum": round(min(step_values)), "maximum": round(max(step_values))}
        if step_values
        else None
    )
    output["semantic_class"] = "daily_context_not_event_time_activity"
    return output


def _context_near(
    bolus_time: float,
    fingersticks: list[tuple[float, dict[str, Any]]],
) -> dict[str, Any] | None:
    candidates = [
        (abs(instant - bolus_time), row)
        for instant, row in fingersticks
        if abs(instant - bolus_time) <= CONTEXT_TOLERANCE_MINUTES * 60
        and any(
            row.get(field) not in (None, "", "unknown")
            for field in ("activity", "position", "compression_possible", "context_note")
        )
    ]
    if not candidates:
        return None
    _, row = min(candidates, key=lambda item: (item[0], str(item[1].get("id") or "")))
    return _source_snapshot(
        row,
        (
            "timestamp",
            "activity",
            "position",
            "hydration",
            "compression_possible",
            "context_note",
        ),
    )


def _cycle_context(
    local_date: str,
    periods_by_date: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    rows = periods_by_date.get(local_date) or []
    if not rows:
        return None
    row = sorted(
        rows,
        key=lambda item: (
            "inferred" in str(item.get("source") or "").lower(),
            str(item.get("id") or ""),
        ),
    )[0]
    source = str(row.get("source") or "unknown")
    return {
        **_source_snapshot(row, ("date", "phase", "cycle_day", "source")),
        "provenance": "inferred" if "inferred" in source.lower() else "recorded_or_imported",
    }


def _iob(
    bolus_time: float,
    prior_boluses: list[tuple[float, float, dict[str, Any]]],
) -> tuple[float, list[dict[str, Any]]]:
    contributors = []
    total = 0.0
    for timestamp, units, row in prior_boluses:
        age_minutes = (bolus_time - timestamp) / 60
        if not 0 < age_minutes < IOB_ACTION_MINUTES:
            continue
        remaining_fraction = 1 - age_minutes / IOB_ACTION_MINUTES
        remaining_units = units * remaining_fraction
        total += remaining_units
        contributors.append(
            {
                "entity_id": row.get("id"),
                "timestamp": row.get("timestamp"),
                "dose_units": units,
                "age_minutes": round(age_minutes, 1),
                "estimated_remaining_units": round(remaining_units, 3),
            }
        )
    return round(total, 3), contributors


def _strata(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    output = {}
    extractors = {
        "time_of_day": lambda event: event["context"]["time_of_day"],
        "cycle_phase": lambda event: (
            event["context"].get("cycle") or {}
        ).get("phase", "unknown"),
        "activity": lambda event: event["context"].get("activity", "unknown"),
        "position": lambda event: event["context"].get("position", "unknown"),
    }
    for name, extractor in extractors.items():
        groups: dict[str, list[float]] = {}
        for event in events:
            value = event["calculations"].get("nadir_drop_per_unit_mg_dl")
            if value is None:
                continue
            groups.setdefault(str(extractor(event) or "unknown"), []).append(float(value))
        output[name] = [
            {
                "value": key,
                "sample_count": len(values),
                "mean_nadir_drop_per_unit_mg_dl": round(mean(values), 1),
                "median_nadir_drop_per_unit_mg_dl": round(median(values), 1),
            }
            for key, values in sorted(groups.items())
        ]
    return output


def build_response_events(
    treatments: list[dict[str, Any]],
    glucose_readings: list[dict[str, Any]],
    *,
    period_logs: list[dict[str, Any]] | None = None,
    wearable_days: list[dict[str, Any]] | None = None,
    fingersticks: list[dict[str, Any]] | None = None,
    timezone_name: str = "UTC",
    event_start: datetime | str | None = None,
    event_end: datetime | str | None = None,
) -> dict[str, Any]:
    """Build deterministic events from bounded canonical source snapshots."""
    tz = ZoneInfo(timezone_name)
    lower_event_time = _instant(event_start) if event_start is not None else None
    upper_event_time = _instant(event_end) if event_end is not None else None
    points = []
    for row in glucose_readings:
        instant = _instant(row.get("timestamp"))
        value = _number(row.get("value"))
        if instant is not None and value is not None:
            points.append((instant.timestamp(), value, row))
    points.sort(key=lambda item: (item[0], str(item[2].get("id") or "")))
    # A duplicate timestamp contributes one deterministic source observation.
    unique_points: list[tuple[float, float, dict[str, Any]]] = []
    for point in points:
        if unique_points and point[0] == unique_points[-1][0]:
            continue
        unique_points.append(point)
    points = unique_points
    point_times = [point[0] for point in points]

    boluses: list[tuple[float, float | None, dict[str, Any]]] = []
    carbs: list[tuple[float, float | None, dict[str, Any]]] = []
    for row in treatments:
        instant = _instant(row.get("timestamp"))
        amount = _number(row.get("amount"))
        if instant is None:
            continue
        kind = str(row.get("type") or "").lower()
        if kind == "insulin" and str(row.get("event_type") or "").lower() != "daily total":
            boluses.append(
                (instant.timestamp(), amount if amount is not None and amount > 0 else None, row)
            )
        elif kind == "carb":
            carbs.append(
                (instant.timestamp(), amount if amount is not None and amount > 0 else None, row)
            )
    boluses.sort(key=lambda item: (item[0], str(item[2].get("id") or "")))
    carbs.sort(key=lambda item: (item[0], str(item[2].get("id") or "")))

    fingerstick_context = []
    for row in fingersticks or []:
        instant = _instant(row.get("timestamp"))
        if instant is not None:
            fingerstick_context.append((instant.timestamp(), row))
    periods_by_date: dict[str, list[dict[str, Any]]] = {}
    for row in period_logs or []:
        if row.get("date"):
            periods_by_date.setdefault(str(row["date"]), []).append(row)
    wearables_by_date: dict[str, list[dict[str, Any]]] = {}
    for row in wearable_days or []:
        if row.get("date"):
            wearables_by_date.setdefault(str(row["date"]), []).append(row)

    events = []
    for index, (bolus_time, units, bolus) in enumerate(boluses):
        if lower_event_time is not None and bolus_time < lower_event_time.timestamp():
            continue
        if upper_event_time is not None and bolus_time > upper_event_time.timestamp():
            continue
        bolus_dt = datetime.fromtimestamp(bolus_time, timezone.utc)
        local = bolus_dt.astimezone(tz)
        target_end = bolus_time + RESPONSE_MINUTES * 60
        start = _nearest(points, point_times, bolus_time, START_TOLERANCE_MINUTES)
        end = _nearest(points, point_times, target_end, END_TOLERANCE_MINUTES)
        left = bisect.bisect_left(point_times, bolus_time)
        right = bisect.bisect_right(point_times, target_end)
        segment = points[left:right]
        nadir = min(
            segment,
            key=lambda item: (item[1], item[0], str(item[2].get("id") or "")),
            default=None,
        )

        related_carbs = [
            item
            for item in carbs
            if bolus_time - CARB_GUARD_PRE_MINUTES * 60
            <= item[0]
            <= target_end
        ]
        later_boluses = [
            item for item in boluses[index + 1 :] if item[0] <= target_end
        ]
        valid_prior_boluses = [
            (timestamp, amount, row)
            for timestamp, amount, row in boluses[:index]
            if amount is not None
        ]
        estimated_iob, iob_contributors = _iob(bolus_time, valid_prior_boluses)
        context_source = _context_near(bolus_time, fingerstick_context)
        activity = str(
            bolus.get("activity")
            or (context_source or {}).get("activity")
            or "unknown"
        ).lower()
        position = str(
            bolus.get("position")
            or (context_source or {}).get("position")
            or "unknown"
        ).lower()

        reasons = []
        if units is None:
            reasons.append("invalid_bolus_amount")
        elif units < MIN_BOLUS_UNITS:
            reasons.append("dose_below_minimum")
        if start is None:
            reasons.append("missing_start_glucose")
        elif start[1] < MIN_START_GLUCOSE_MG_DL:
            reasons.append("start_glucose_below_analysis_minimum")
        if end is None:
            reasons.append("missing_end_glucose")
        if len(segment) < MIN_RESPONSE_POINTS:
            reasons.append("insufficient_cgm_coverage")
        if related_carbs:
            reasons.append("carbohydrate_in_response_window")
        if any(amount is None for _, amount, _ in related_carbs):
            reasons.append("uninterpretable_carbohydrate_in_response_window")
        if estimated_iob > 0:
            reasons.append("prior_estimated_iob")
        if later_boluses:
            reasons.append("subsequent_bolus_in_response_window")
        if activity in {"moderate", "vigorous"}:
            reasons.append("activity_may_affect_response")
        if (context_source or {}).get("compression_possible") is True:
            reasons.append("possible_cgm_compression")

        hard = [reason for reason in reasons if reason in _HARD_EXCLUSIONS]
        confounders = [reason for reason in reasons if reason not in _HARD_EXCLUSIONS]
        classification = "excluded" if hard else "confounded" if confounders else "clean"
        start_value = start[1] if start else None
        end_value = end[1] if end else None
        nadir_value = nadir[1] if nadir else None
        drop = (
            round(start_value - nadir_value, 1)
            if start_value is not None and nadir_value is not None
            else None
        )
        input_identity = {
            "algorithm_version": ALGORITHM_VERSION,
            "bolus": _source_snapshot(
                bolus, ("timestamp", "amount", "source", "event_type")
            ),
        }
        event_id = "insulin-response-" + _checksum(input_identity).removeprefix("sha256:")[:32]
        events.append(
            {
                "id": event_id,
                "algorithm_version": ALGORITHM_VERSION,
                "semantic_class": "derived_observational_response",
                "classification": classification,
                "included_by_default": classification == "clean",
                "exclusion_reasons": reasons,
                "hard_exclusion_reasons": hard,
                "confounder_reasons": confounders,
                "observed": {
                    "bolus": _source_snapshot(
                        bolus, ("timestamp", "amount", "source", "event_type", "insulin_type")
                    ),
                    "start_glucose": _source_snapshot(
                        start[2], ("timestamp", "value", "source", "trend")
                    )
                    if start
                    else None,
                    "end_glucose": _source_snapshot(
                        end[2], ("timestamp", "value", "source", "trend")
                    )
                    if end
                    else None,
                    "nadir_glucose": _source_snapshot(
                        nadir[2], ("timestamp", "value", "source", "trend")
                    )
                    if nadir
                    else None,
                    "carbohydrates": [
                        _source_snapshot(row, ("timestamp", "amount", "source"))
                        for _, _, row in related_carbs
                    ],
                    "subsequent_boluses": [
                        _source_snapshot(row, ("timestamp", "amount", "source"))
                        for _, _, row in later_boluses
                    ],
                    "context_source": context_source,
                },
                "calculations": {
                    "response_window_end": _iso(
                        datetime.fromtimestamp(target_end, timezone.utc)
                    ),
                    "cgm_points_in_window": len(segment),
                    "start_glucose_mg_dl": start_value,
                    "end_glucose_mg_dl": end_value,
                    "nadir_glucose_mg_dl": nadir_value,
                    "time_to_nadir_minutes": (
                        round((nadir[0] - bolus_time) / 60, 1) if nadir else None
                    ),
                    "nadir_drop_mg_dl": drop,
                    "end_drop_mg_dl": (
                        round(start_value - end_value, 1)
                        if start_value is not None and end_value is not None
                        else None
                    ),
                    "nadir_drop_per_unit_mg_dl": (
                        round(drop / units, 1)
                        if drop is not None and units is not None
                        else None
                    ),
                    "estimated_iob_units": estimated_iob,
                    "iob_contributors": iob_contributors,
                    "carbohydrates_g": round(
                        sum(amount for _, amount, _ in related_carbs if amount is not None),
                        1,
                    ),
                },
                "context": {
                    "local_date": local.date().isoformat(),
                    "time_of_day": _time_bucket(local),
                    "cycle": _cycle_context(local.date().isoformat(), periods_by_date),
                    "activity": activity,
                    "position": position,
                    "daily_activity": _day_activity(
                        wearables_by_date.get(local.date().isoformat()) or []
                    ),
                },
                "assumptions": ASSUMPTIONS,
                "source_input_hash": _checksum(
                    {
                        "bolus": input_identity["bolus"],
                        "start": start[2] if start else None,
                        "end": end[2] if end else None,
                        "nadir": nadir[2] if nadir else None,
                        "carbs": _canonical_rows([item[2] for item in related_carbs]),
                        "later_boluses": _canonical_rows(
                            [item[2] for item in later_boluses]
                        ),
                        "iob_contributors": iob_contributors,
                        "context": context_source,
                    }
                ),
            }
        )

    clean = [event for event in events if event["classification"] == "clean"]
    response_values = [
        float(event["calculations"]["nadir_drop_per_unit_mg_dl"])
        for event in clean
        if event["calculations"].get("nadir_drop_per_unit_mg_dl") is not None
    ]
    event_counts = Counter(event["classification"] for event in events)
    reason_counts = Counter(
        reason for event in events for reason in event["exclusion_reasons"]
    )
    average = mean(response_values) if response_values else None
    cv = (
        round(pstdev(response_values) / average * 100)
        if len(response_values) >= 2 and average
        else None
    )
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "input_data_version": _checksum(
            {
                "treatments": _canonical_rows(treatments),
                "glucose": _canonical_rows(glucose_readings),
                "period_logs": _canonical_rows(period_logs or []),
                "wearable_days": _canonical_rows(wearable_days or []),
                "fingersticks": _canonical_rows(fingersticks or []),
                "event_start": _iso(lower_event_time) if lower_event_time else None,
                "event_end": _iso(upper_event_time) if upper_event_time else None,
                "assumptions": ASSUMPTIONS,
            }
        ),
        "semantics": {
            "observed": "Source values and source entity identities.",
            "calculated": "Deterministic calculations under the declared assumptions.",
            "association": "A time-bounded observation; causation is not established.",
            "interpretation": (
                "Insulin resistance or absorption is not inferred by the event builder."
            ),
        },
        "assumptions": ASSUMPTIONS,
        "counts": {
            "total": len(events),
            "clean": event_counts["clean"],
            "confounded": event_counts["confounded"],
            "excluded": event_counts["excluded"],
            "included_by_default": len(clean),
        },
        "reason_counts": dict(sorted(reason_counts.items())),
        "analysis": {
            "sample_count": len(response_values),
            "mean_nadir_drop_per_unit_mg_dl": (
                round(average, 1) if average is not None else None
            ),
            "median_nadir_drop_per_unit_mg_dl": (
                round(median(response_values), 1) if response_values else None
            ),
            "cv_pct": cv,
            "strata": _strata(clean),
            "default_filter": "classification=clean",
        },
        "events": events,
    }
