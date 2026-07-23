"""Shared, deterministic confidence metadata for observational analytics.

This module describes statistical support; it never assigns clinical truth or
causality.  Derived rows remain ordinary rebuildable entities in SQLite.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any


ANALYTICS_CONFIDENCE_VERSION = "analytics-confidence/1.0.0"
Z_95 = 1.959963984540054
DISCOVERY_STATUSES = (
    "exploratory",
    "emerging",
    "reproduced",
    "not-reproduced",
    "invalid",
)
PROHIBITED_CLAIMS = (
    "causes",
    "clinically confirmed",
    "definitive",
    "proves",
)


def _finite(values: Iterable[Any]) -> list[float]:
    output = []
    for value in values:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric):
            output.append(numeric)
    return output


def _pearson(pairs: Sequence[tuple[float, float]]) -> float | None:
    if len(pairs) < 4:
        return None
    xs = [pair[0] for pair in pairs]
    ys = [pair[1] for pair in pairs]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    spread_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    spread_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if spread_x == 0 or spread_y == 0:
        return None
    return max(-1.0, min(1.0, covariance / (spread_x * spread_y)))


def _correlation_interval(value: float, sample_count: int) -> tuple[float, float] | None:
    if sample_count <= 3 or abs(value) >= 1:
        if abs(value) == 1 and sample_count > 3:
            return value, value
        return None
    transformed = math.atanh(value)
    margin = Z_95 / math.sqrt(sample_count - 3)
    return math.tanh(transformed - margin), math.tanh(transformed + margin)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _sample_variance(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    return sum((value - mean) ** 2 for value in values) / (len(values) - 1)


def _magnitude(value: float, *, metric: str) -> str:
    absolute = abs(value)
    if metric == "pearson_r":
        boundaries = (0.1, 0.3, 0.5)
    elif metric == "cohen_d":
        boundaries = (0.2, 0.5, 0.8)
    else:
        boundaries = (0.05, 0.15, 0.30)
    if absolute < boundaries[0]:
        return "negligible"
    if absolute < boundaries[1]:
        return "small"
    if absolute < boundaries[2]:
        return "moderate"
    return "large"


def _missingness(valid_days: int, expected_days: int) -> dict[str, Any]:
    expected = max(0, int(expected_days))
    valid = min(expected, max(0, int(valid_days))) if expected else max(0, int(valid_days))
    missing = max(0, expected - valid)
    return {
        "expected_days": expected,
        "valid_days": valid,
        "missing_days": missing,
        "missing_rate": round(missing / expected, 4) if expected else None,
    }


def _language(status: str) -> dict[str, Any]:
    leads = {
        "exploratory": "An exploratory signal was observed in this limited sample.",
        "emerging": "An emerging observational association was detected.",
        "reproduced": "The association repeated in a later temporal holdout.",
        "not-reproduced": "An initial signal was not reproduced in the later temporal holdout.",
        "invalid": "The available data could not support a valid estimate.",
    }
    return {
        "strength": status,
        "lead": leads[status],
        "definitive_allowed": False,
        "causal_allowed": False,
        "prohibited_claims": list(PROHIBITED_CLAIMS),
    }


def _score(
    *,
    sample_count: int,
    valid_days: int,
    expected_days: int,
    precision: float,
    status: str,
) -> float:
    if status == "invalid":
        return 0.0
    coverage = min(1.0, valid_days / expected_days) if expected_days else 0.0
    sample = min(1.0, sample_count / 30)
    replication = 0.10 if status == "reproduced" else 0.0
    penalty = 0.20 if status == "not-reproduced" else 0.0
    return round(max(0.0, min(1.0, 0.35 * coverage + 0.35 * sample + 0.30 * precision + replication - penalty)), 3)


def _label(score: float) -> str:
    if score >= 0.85:
        return "high"
    if score >= 0.60:
        return "medium"
    return "low"


def _base_envelope(
    *,
    sample_count: int,
    valid_days: int,
    expected_days: int,
    effect_size: dict[str, Any],
    confidence_interval: dict[str, Any] | None,
    temporal_direction: str,
    status: str,
    replication: dict[str, Any],
    precision: float,
    phase_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    score = _score(
        sample_count=sample_count,
        valid_days=valid_days,
        expected_days=expected_days,
        precision=precision,
        status=status,
    )
    output = {
        "version": ANALYTICS_CONFIDENCE_VERSION,
        "sample_count": sample_count,
        "valid_days": valid_days,
        "effect_size": effect_size,
        "confidence_interval": confidence_interval,
        "missingness": _missingness(valid_days, expected_days),
        "temporal_direction": temporal_direction,
        "discovery_status": status,
        "replication": replication,
        "confidence_score": score,
        "confidence_label": _label(score),
        "language": _language(status),
    }
    if phase_provenance is not None:
        output["phase_provenance"] = phase_provenance
    return output


def correlation_confidence(
    dated_pairs: Sequence[tuple[str, float, float]],
    *,
    expected_days: int,
    temporal_direction: str = "contemporaneous",
    effect_threshold: float = 0.30,
) -> tuple[float | None, dict[str, Any]]:
    """Return Pearson r and its confidence envelope.

    Inputs are sorted by date before the optional first-half/later-half
    temporal holdout. A seven-day result is always exploratory.
    """
    pairs = []
    for day, x, y in sorted(dated_pairs, key=lambda item: item[0]):
        values = _finite((x, y))
        if len(values) == 2:
            pairs.append((str(day), values[0], values[1]))
    numeric_pairs = [(x, y) for _, x, y in pairs]
    value = _pearson(numeric_pairs)
    sample_count = len(numeric_pairs)
    valid_days = len({day for day, _, _ in pairs})
    interval = _correlation_interval(value, sample_count) if value is not None else None
    if interval:
        confidence_interval = {
            "level": 0.95,
            "lower": round(interval[0], 4),
            "upper": round(interval[1], 4),
            "metric": "pearson_r",
            "method": "fisher_z",
        }
        precision = max(0.0, 1 - (interval[1] - interval[0]) / 2)
    else:
        confidence_interval = None
        precision = 0.0

    replication: dict[str, Any] = {
        "attempted": False,
        "kind": None,
        "discovery_sample_count": sample_count,
        "replication_sample_count": 0,
        "discovery_effect": round(value, 4) if value is not None else None,
        "replication_effect": None,
        "status": "not-attempted",
    }
    if value is None:
        status = "invalid"
    elif valid_days <= 7 or sample_count < 14:
        status = "exploratory"
    elif sample_count >= 28:
        midpoint = sample_count // 2
        discovery = _pearson(numeric_pairs[:midpoint])
        holdout = _pearson(numeric_pairs[midpoint:])
        reproduced = (
            discovery is not None
            and holdout is not None
            and abs(discovery) >= effect_threshold
            and abs(holdout) >= effect_threshold
            and math.copysign(1, discovery) == math.copysign(1, holdout)
        )
        replication = {
            "attempted": True,
            "kind": "temporal_holdout",
            "discovery_sample_count": midpoint,
            "replication_sample_count": sample_count - midpoint,
            "discovery_effect": round(discovery, 4) if discovery is not None else None,
            "replication_effect": round(holdout, 4) if holdout is not None else None,
            "status": "reproduced" if reproduced else "not-reproduced",
        }
        status = replication["status"]
    else:
        status = "emerging"

    effect = {
        "metric": "pearson_r",
        "value": round(value, 4) if value is not None else None,
        "magnitude": _magnitude(value, metric="pearson_r") if value is not None else "unknown",
        "direction": "positive" if value and value > 0 else "negative" if value and value < 0 else "none",
    }
    return value, _base_envelope(
        sample_count=sample_count,
        valid_days=valid_days,
        expected_days=expected_days,
        effect_size=effect,
        confidence_interval=confidence_interval,
        temporal_direction=temporal_direction,
        status=status,
        replication=replication,
        precision=precision,
    )


def comparison_confidence(
    group_a: Sequence[float],
    group_b: Sequence[float],
    *,
    valid_days: int,
    expected_days: int,
    temporal_direction: str = "non-directional-group-comparison",
    unit: str | None = None,
    phase_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe a two-group mean difference with a normal Welch interval."""
    first = _finite(group_a)
    second = _finite(group_b)
    sample_count = len(first) + len(second)
    valid = len(first) >= 2 and len(second) >= 2
    difference = _mean(first) - _mean(second) if valid else None
    variance_a = _sample_variance(first) if valid else 0.0
    variance_b = _sample_variance(second) if valid else 0.0
    standard_error = math.sqrt(variance_a / len(first) + variance_b / len(second)) if valid else 0.0
    pooled_denominator = len(first) + len(second) - 2
    pooled_variance = (
        ((len(first) - 1) * variance_a + (len(second) - 1) * variance_b) / pooled_denominator
        if valid and pooled_denominator > 0
        else 0.0
    )
    standardized = (
        difference / math.sqrt(pooled_variance)
        if difference is not None and pooled_variance > 0
        else None
    )
    interval = (
        (difference - Z_95 * standard_error, difference + Z_95 * standard_error)
        if difference is not None
        else None
    )
    if not valid:
        status = "invalid"
    elif valid_days <= 7 or sample_count < 14:
        status = "exploratory"
    else:
        status = "emerging"
    confidence_interval = None
    if interval:
        confidence_interval = {
            "level": 0.95,
            "lower": round(interval[0], 4),
            "upper": round(interval[1], 4),
            "metric": "mean_difference",
            "method": "normal_welch",
            "unit": unit,
        }
    scale = max(abs(difference or 0), standard_error * 4, 1)
    precision = max(0.0, 1 - (2 * Z_95 * standard_error) / (2 * scale)) if valid else 0.0
    return _base_envelope(
        sample_count=sample_count,
        valid_days=valid_days,
        expected_days=expected_days,
        effect_size={
            "metric": "cohen_d",
            "value": round(standardized, 4) if standardized is not None else None,
            "magnitude": _magnitude(standardized, metric="cohen_d") if standardized is not None else "unknown",
            "direction": "positive" if difference and difference > 0 else "negative" if difference and difference < 0 else "none",
            "raw_difference": round(difference, 4) if difference is not None else None,
            "unit": unit,
        },
        confidence_interval=confidence_interval,
        temporal_direction=temporal_direction,
        status=status,
        replication={
            "attempted": False,
            "kind": None,
            "discovery_sample_count": sample_count,
            "replication_sample_count": 0,
            "discovery_effect": round(standardized, 4) if standardized is not None else None,
            "replication_effect": None,
            "status": "not-attempted",
        },
        precision=precision,
        phase_provenance=phase_provenance,
    )


def mean_confidence(
    values: Sequence[float],
    *,
    valid_days: int,
    expected_days: int,
    temporal_direction: str = "repeated-observation",
    unit: str | None = None,
) -> dict[str, Any]:
    """Describe an observational sample mean with a normal 95% interval."""
    samples = _finite(values)
    sample_count = len(samples)
    valid = sample_count >= 2
    average = _mean(samples) if samples else None
    standard_error = (
        math.sqrt(_sample_variance(samples) / sample_count) if valid else None
    )
    interval = (
        (
            average - Z_95 * standard_error,
            average + Z_95 * standard_error,
        )
        if average is not None and standard_error is not None
        else None
    )
    if not valid:
        status = "invalid"
    elif valid_days <= 7 or sample_count < 14:
        status = "exploratory"
    else:
        status = "emerging"
    width = interval[1] - interval[0] if interval else None
    scale = max(abs(average or 0), (width or 0), 1)
    precision = max(0.0, 1 - (width or scale) / scale) if valid else 0.0
    return _base_envelope(
        sample_count=sample_count,
        valid_days=valid_days,
        expected_days=expected_days,
        effect_size={
            "metric": "observed_mean",
            "value": round(average, 4) if average is not None else None,
            "magnitude": "not_clinically_classified",
            "direction": "observed",
            "unit": unit,
        },
        confidence_interval={
            "level": 0.95,
            "lower": round(interval[0], 4),
            "upper": round(interval[1], 4),
            "metric": "observed_mean",
            "method": "normal_mean",
            "unit": unit,
        }
        if interval
        else None,
        temporal_direction=temporal_direction,
        status=status,
        replication={
            "attempted": False,
            "kind": None,
            "discovery_sample_count": sample_count,
            "replication_sample_count": 0,
            "discovery_effect": round(average, 4) if average is not None else None,
            "replication_effect": None,
            "status": "not-attempted",
        },
        precision=precision,
    )


def proportion_confidence(
    successes: int,
    trials: int,
    *,
    valid_days: int,
    expected_days: int,
    temporal_direction: str = "repeated-observation",
) -> dict[str, Any]:
    """Describe a repeated binary observation with a Wilson interval."""
    successes = max(0, int(successes))
    trials = max(0, int(trials))
    valid = trials > 0 and successes <= trials
    rate = successes / trials if valid else None
    interval = None
    if rate is not None:
        denominator = 1 + Z_95**2 / trials
        center = (rate + Z_95**2 / (2 * trials)) / denominator
        margin = Z_95 * math.sqrt(rate * (1 - rate) / trials + Z_95**2 / (4 * trials**2)) / denominator
        interval = max(0.0, center - margin), min(1.0, center + margin)
    if not valid:
        status = "invalid"
    elif valid_days <= 7 or trials < 30:
        status = "exploratory"
    else:
        status = "emerging"
    width = interval[1] - interval[0] if interval else 1.0
    return _base_envelope(
        sample_count=trials,
        valid_days=valid_days,
        expected_days=expected_days,
        effect_size={
            "metric": "observed_rate",
            "value": round(rate, 4) if rate is not None else None,
            "magnitude": _magnitude(rate, metric="observed_rate") if rate is not None else "unknown",
            "direction": "present" if successes else "none",
        },
        confidence_interval={
            "level": 0.95,
            "lower": round(interval[0], 4),
            "upper": round(interval[1], 4),
            "metric": "observed_rate",
            "method": "wilson_score",
        } if interval else None,
        temporal_direction=temporal_direction,
        status=status,
        replication={
            "attempted": False,
            "kind": None,
            "discovery_sample_count": trials,
            "replication_sample_count": 0,
            "discovery_effect": round(rate, 4) if rate is not None else None,
            "replication_effect": None,
            "status": "not-attempted",
        },
        precision=max(0.0, 1 - width),
    )


def phase_provenance(
    rows: Iterable[dict[str, Any]],
    *,
    included_dates: set[str] | None = None,
) -> dict[str, Any]:
    """Count explicitly recorded versus algorithm-inferred cycle phase days."""
    by_phase: dict[str, dict[str, int]] = {}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        day = str(row.get("date") or "")
        phase = str(row.get("phase") or "")
        if not day or not phase or (included_dates is not None and day not in included_dates):
            continue
        key = (day, phase)
        if key in seen:
            continue
        seen.add(key)
        bucket = by_phase.setdefault(phase, {"confirmed_days": 0, "inferred_days": 0, "total_days": 0})
        field = "inferred_days" if "inferred" in str(row.get("source") or "").lower() else "confirmed_days"
        bucket[field] += 1
        bucket["total_days"] += 1
    confirmed = sum(item["confirmed_days"] for item in by_phase.values())
    inferred = sum(item["inferred_days"] for item in by_phase.values())
    classification = "mixed" if confirmed and inferred else "inferred" if inferred else "confirmed" if confirmed else "unknown"
    return {
        "classification": classification,
        "confirmed_days": confirmed,
        "inferred_days": inferred,
        "total_days": confirmed + inferred,
        "definition": "confirmed means explicitly recorded/imported; inferred means algorithm-estimated",
        "by_phase": dict(sorted(by_phase.items())),
    }


def safe_analytics_text(text: Any, envelope: dict[str, Any], fallback: str = "") -> str:
    """Reject definitive or causal model prose and prepend the governed lead."""
    lead = str(envelope.get("language", {}).get("lead") or "").strip()
    candidate = " ".join(str(text or "").split())
    lowered = candidate.lower()
    if any(claim in lowered for claim in PROHIBITED_CLAIMS):
        candidate = " ".join(str(fallback or "").split())
    return " ".join(part for part in (lead, candidate) if part)
