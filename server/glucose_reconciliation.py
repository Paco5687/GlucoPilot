"""Deterministic CGM/fingerstick reconciliation without replacing either source.

Fingerstick and CGM values are separate observations with different measurement
characteristics.  A pair is a time-bounded comparison, not a declaration that
either source is clinical truth.  The functions in this module are pure so the
capture API, reports, tests, and future projections share the same semantics.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from .analytics_confidence import ANALYTICS_CONFIDENCE_VERSION


RECONCILIATION_VERSION = "glucose-reconciliation/1.0.0"
PERSISTENT_BIAS_MINIMUM_PAIRS = 5
LOW_THRESHOLD_MG_DL = 70
SEVERE_LOW_THRESHOLD_MG_DL = 54

TIMING_CONTEXTS = frozenset(
    {"unknown", "waking", "pre_meal", "post_meal", "overnight", "exercise", "symptoms", "other"}
)
ACTIVITY_LEVELS = frozenset({"unknown", "resting", "light", "moderate", "vigorous"})
POSITIONS = frozenset({"unknown", "upright", "seated", "lying"})
HYDRATION_LEVELS = frozenset({"unknown", "low", "usual", "high"})
SENSOR_SITES = frozenset({"unknown", "arm", "abdomen", "other"})


class ReconciliationInputError(ValueError):
    """A reconciliation context field is invalid or outside its safe bound."""


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _number(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _choice(body: dict[str, Any], name: str, allowed: frozenset[str]) -> str:
    value = str(body.get(name) or "unknown").strip().lower()
    if value not in allowed:
        raise ReconciliationInputError(
            f"{name} must be one of: {', '.join(sorted(allowed))}."
        )
    return value


def capture_context(body: dict[str, Any]) -> dict[str, Any]:
    """Validate optional capture circumstances while keeping entry fast."""
    sensor_day = body.get("sensor_day")
    if sensor_day in (None, ""):
        normalized_sensor_day = None
    else:
        try:
            normalized_sensor_day = int(sensor_day)
        except (TypeError, ValueError) as error:
            raise ReconciliationInputError("sensor_day must be a whole number.") from error
        if not 1 <= normalized_sensor_day <= 30:
            raise ReconciliationInputError("sensor_day must be between 1 and 30.")

    compression = body.get("compression_possible")
    if compression in (None, ""):
        normalized_compression = None
    elif isinstance(compression, bool):
        normalized_compression = compression
    else:
        raise ReconciliationInputError("compression_possible must be true, false, or omitted.")

    note = str(body.get("context_note") or "").strip()
    if len(note) > 500:
        raise ReconciliationInputError("context_note cannot exceed 500 characters.")

    return {
        "timing_context": _choice(body, "timing_context", TIMING_CONTEXTS),
        "sensor_day": normalized_sensor_day,
        "sensor_site": _choice(body, "sensor_site", SENSOR_SITES),
        "activity": _choice(body, "activity", ACTIVITY_LEVELS),
        "position": _choice(body, "position", POSITIONS),
        "hydration": _choice(body, "hydration", HYDRATION_LEVELS),
        "compression_possible": normalized_compression,
        "context_note": note,
    }


def _low_classification(
    meter: float,
    cgm: float,
    threshold: float = LOW_THRESHOLD_MG_DL,
) -> str:
    meter_low = meter < threshold
    cgm_low = cgm < threshold
    if meter_low and cgm_low:
        return "confirmed_low"
    if cgm_low:
        return "cgm_only_low"
    if meter_low:
        return "meter_only_low"
    return "neither_low"


def pair_fields(
    fingerstick_value: float,
    fingerstick_timestamp: str,
    cgm: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return a versioned, fixed comparison snapshot for a new reading."""
    if not cgm:
        return {
            "reconciliation_version": RECONCILIATION_VERSION,
            "pair_status": "unpaired",
        }
    cgm_value = _number(cgm.get("value"))
    if cgm_value is None:
        return {
            "reconciliation_version": RECONCILIATION_VERSION,
            "pair_status": "unpaired",
        }

    delta = round(cgm_value - fingerstick_value, 1)
    absolute = round(abs(delta), 1)
    relative = round(absolute / fingerstick_value * 100, 1)
    tolerance = max(15.0, fingerstick_value * 0.15)
    fingerstick_time = _parse_time(fingerstick_timestamp)
    cgm_time = _parse_time(cgm.get("timestamp"))
    offset = (
        round((cgm_time - fingerstick_time).total_seconds())
        if fingerstick_time is not None and cgm_time is not None
        else None
    )
    return {
        "reconciliation_version": RECONCILIATION_VERSION,
        "pair_status": "paired",
        "cgm_value": cgm_value,
        "cgm_timestamp": cgm.get("timestamp"),
        "cgm_source": cgm.get("source"),
        "cgm_reading_id": cgm.get("id"),
        "cgm_trend": cgm.get("trend"),
        "pair_offset_seconds": offset,
        "delta": delta,
        "absolute_difference_mg_dl": absolute,
        "relative_difference_percent": relative,
        "directional_difference": (
            "within_comparison_band"
            if absolute <= tolerance
            else "cgm_high"
            if delta > 0
            else "cgm_low"
        ),
        "comparison_band": "15_mg_dl_or_15_percent",
        "low_classification": _low_classification(fingerstick_value, cgm_value),
        "severe_low_classification": _low_classification(
            fingerstick_value, cgm_value, SEVERE_LOW_THRESHOLD_MG_DL
        ),
    }


def _paired_fields(row: dict[str, Any]) -> dict[str, Any] | None:
    meter = _number(row.get("value"))
    cgm = _number(row.get("cgm_value"))
    if meter is None or cgm is None:
        return None
    derived = pair_fields(
        meter,
        str(row.get("timestamp") or ""),
        {
            "value": cgm,
            "timestamp": row.get("cgm_timestamp"),
            "source": row.get("cgm_source"),
            "id": row.get("cgm_reading_id"),
            "trend": row.get("cgm_trend"),
        },
    )
    return {**{key: value for key, value in row.items() if value is not None}, **derived}


def _mean_interval(values: list[float]) -> tuple[float, float] | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    margin = 1.959963984540054 * math.sqrt(variance / len(values))
    return mean - margin, mean + margin


def _trend_bucket(value: Any) -> str:
    trend = str(value or "").lower()
    if "up" in trend or "rise" in trend:
        return "rising"
    if "down" in trend or "fall" in trend:
        return "falling"
    if "flat" in trend or "steady" in trend:
        return "steady"
    return "unknown"


def _sensor_day_bucket(value: Any) -> str:
    try:
        day = int(value)
    except (TypeError, ValueError):
        return "unknown"
    if day <= 3:
        return "days_1_3"
    if day <= 7:
        return "days_4_7"
    return "day_8_plus"


def _stratum(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    groups: dict[str, list[float]] = {}
    for row in rows:
        if field == "trend":
            key = _trend_bucket(row.get("cgm_trend"))
        elif field == "sensor_day":
            key = _sensor_day_bucket(row.get("sensor_day"))
        else:
            key = str(row.get(field) or "unknown")
        groups.setdefault(key, []).append(float(row["delta"]))
    return [
        {
            "value": key,
            "sample_count": len(values),
            "mean_signed_difference_mg_dl": round(sum(values) / len(values), 1),
            "mean_absolute_difference_mg_dl": round(
                sum(abs(value) for value in values) / len(values), 1
            ),
        }
        for key, values in sorted(groups.items())
    ]


def summarize(readings: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize fixed pairs, directional bias, strata, and checked lows."""
    paired = [fields for row in readings if (fields := _paired_fields(row)) is not None]
    deltas = [float(row["delta"]) for row in paired]
    interval = _mean_interval(deltas)
    mean_delta = sum(deltas) / len(deltas) if deltas else None
    persistent_bias = "insufficient_sample"
    if len(deltas) >= PERSISTENT_BIAS_MINIMUM_PAIRS and interval is not None:
        if interval[0] > 0:
            persistent_bias = "cgm_high"
        elif interval[1] < 0:
            persistent_bias = "cgm_low"
        else:
            persistent_bias = "not_detected"

    days = {
        str(row.get("timestamp"))[:10]
        for row in paired
        if row.get("timestamp")
    }
    low_counts = Counter(row.get("low_classification") for row in paired)
    severe_counts = Counter(row.get("severe_low_classification") for row in paired)
    confidence_label = (
        "not_assessed" if not paired else "low" if len(paired) < 14 else "medium"
    )
    return {
        "version": RECONCILIATION_VERSION,
        "semantics": (
            "CGM and meter values remain separate observations. Pairing is temporal "
            "and does not establish either source as clinical truth."
        ),
        "count": len(readings),
        "paired": len(paired),
        "unpaired": len(readings) - len(paired),
        "mean_delta": round(mean_delta, 1) if mean_delta is not None else None,
        "mean_abs_delta": (
            round(sum(abs(value) for value in deltas) / len(deltas), 1) if deltas else None
        ),
        "max_abs_delta": max((abs(value) for value in deltas), default=None),
        "persistent_bias": {
            "classification": persistent_bias,
            "sample_count": len(paired),
            "minimum_sample_count": PERSISTENT_BIAS_MINIMUM_PAIRS,
            "mean_signed_difference_mg_dl": (
                round(mean_delta, 1) if mean_delta is not None else None
            ),
            "confidence_interval_95_mg_dl": (
                {"lower": round(interval[0], 1), "upper": round(interval[1], 1)}
                if interval
                else None
            ),
        },
        "confidence": {
            "version": ANALYTICS_CONFIDENCE_VERSION,
            "sample_count": len(paired),
            "valid_days": len(days),
            "confidence_label": confidence_label,
            "discovery_status": (
                "invalid" if not paired else "exploratory" if len(paired) < 30 else "emerging"
            ),
            "language": {
                "definitive_allowed": False,
                "causal_allowed": False,
            },
        },
        "low_reconciliation": {
            "paired_checks": len(paired),
            "confirmed_low": low_counts["confirmed_low"],
            "cgm_only_low": low_counts["cgm_only_low"],
            "meter_only_low": low_counts["meter_only_low"],
            "neither_low": low_counts["neither_low"],
            "confirmed_severe_low": severe_counts["confirmed_low"],
            "cgm_only_severe_low": severe_counts["cgm_only_low"],
            "meter_only_severe_low": severe_counts["meter_only_low"],
            "caveat": (
                "These counts describe only meter-checked moments. They do not correct, "
                "replace, or reweight CGM time-below-range."
            ),
        },
        "strata": {
            field: _stratum(paired, field)
            for field in ("trend", "sensor_day", "sensor_site", "position", "activity")
        },
    }
