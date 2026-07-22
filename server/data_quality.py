"""Deterministic coverage, freshness, reliability, and AI-eligibility metadata."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo


QUALITY_VERSION = "data-quality/1.0.0"
MIN_AI_RELIABILITY_SCORE = 0.60

DOMAIN_RULES = {
    "cgm": {"min_coverage_pct": 70.0, "max_freshness_days": 2},
    "pump_tdd": {"min_coverage_pct": 50.0, "max_freshness_days": 14},
    "insulin_response": {"min_coverage_pct": 100.0, "max_freshness_days": 14},
    "wearables": {"min_coverage_pct": 50.0, "max_freshness_days": 7},
    "nutrition": {"min_coverage_pct": 30.0, "max_freshness_days": 7},
    "cycle": {"min_coverage_pct": 50.0, "max_freshness_days": 45},
}


def _instant(value: Any, tz: ZoneInfo) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(timezone.utc)


def _date(value: Any, tz: ZoneInfo) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    instant = _instant(value, tz)
    if instant is not None:
        return instant.astimezone(tz).date()
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _hash_inputs(values: Iterable[Any]) -> str:
    encoded = json.dumps(sorted(str(value) for value in values), separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _usable(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    return True


def _scan_cgm(
    rows: list[dict[str, Any]], tz: ZoneInfo, start: datetime, end: datetime
) -> tuple[dict[str, tuple[datetime, float]], int, int]:
    valid: dict[str, tuple[datetime, float]] = {}
    invalid = 0
    in_window = 0
    for row in rows:
        instant = _instant(row.get("timestamp"), tz)
        try:
            value = float(row.get("value"))
        except (TypeError, ValueError):
            value = math.nan
        if instant is None or not math.isfinite(value):
            invalid += 1
            continue
        if start <= instant <= end:
            in_window += 1
            valid[instant.isoformat()] = (instant, value)
    return valid, invalid, max(0, in_window - len(valid))


def cgm_points(
    rows: list[dict[str, Any]], tz: ZoneInfo, *, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    """Return the unique, numeric, in-window points used by CGM derivations."""
    valid, _, _ = _scan_cgm(rows, tz, start, end)
    return sorted(valid.values())


def build_envelope(
    domain: str,
    *,
    observed: int,
    expected: int,
    unit: str,
    data_through: date | None,
    as_of: date,
    limitations: Iterable[str] = (),
    blocking_reasons: Iterable[str] = (),
    input_values: Iterable[Any] = (),
) -> dict[str, Any]:
    """Build a stable quality envelope using the registered domain thresholds."""
    if domain not in DOMAIN_RULES:
        raise ValueError(f"Unknown quality domain: {domain}")
    rule = DOMAIN_RULES[domain]
    expected = max(0, int(expected))
    observed = max(0, int(observed))
    coverage_pct = round(min(1.0, observed / expected) * 100, 1) if expected else 0.0
    freshness_days = max(0, (as_of - data_through).days) if data_through else None
    future_data = data_through is not None and data_through > as_of
    max_freshness = int(rule["max_freshness_days"])
    if freshness_days is None:
        freshness_score = 0.0
    elif freshness_days <= max_freshness / 2:
        freshness_score = 1.0
    else:
        freshness_score = max(0.0, 1 - (freshness_days - max_freshness / 2) / (max_freshness * 1.5))
    score = round((coverage_pct / 100) * 0.8 + freshness_score * 0.2, 3)
    blockers = list(dict.fromkeys(str(reason) for reason in blocking_reasons if reason))
    if blockers:
        score = min(score, 0.49)
    if score >= 0.85:
        label = "high"
    elif score >= 0.60:
        label = "medium"
    else:
        label = "low"
    exclusions = list(blockers)
    if future_data:
        exclusions.append("data-through date is in the future")
    if score < MIN_AI_RELIABILITY_SCORE:
        exclusions.append(
            f"reliability score {score:g} is below the {MIN_AI_RELIABILITY_SCORE:g} AI threshold"
        )
    if coverage_pct < float(rule["min_coverage_pct"]):
        exclusions.append(
            f"coverage {coverage_pct:g}% is below the {rule['min_coverage_pct']:g}% AI threshold"
        )
    if freshness_days is None:
        exclusions.append("no data-through date is available")
    elif freshness_days > max_freshness:
        exclusions.append(
            f"data is {freshness_days} days old; maximum AI freshness is {max_freshness} days"
        )
    exclusions = list(dict.fromkeys(exclusions))
    hash_values = [
        f"domain:{domain}", f"observed:{observed}", f"expected:{expected}", f"unit:{unit}",
        f"data_through:{data_through.isoformat() if data_through else ''}", f"as_of:{as_of.isoformat()}",
    ]
    hash_values.extend(str(value) for value in input_values)
    return {
        "quality_version": QUALITY_VERSION,
        "domain": domain,
        "coverage_pct": coverage_pct,
        "observed": observed,
        "expected": expected,
        "unit": unit,
        "data_through": data_through.isoformat() if data_through else None,
        "freshness_days": freshness_days,
        "reliability_score": score,
        "reliability": label,
        "complete": (
            not blockers and not future_data and coverage_pct >= 95
            and freshness_days is not None and freshness_days <= max_freshness
        ),
        "ai_eligible": not exclusions,
        "limitations": list(dict.fromkeys(str(value) for value in limitations if value)),
        "exclusion_reasons": exclusions,
        "input_data_version": _hash_inputs(hash_values),
    }


def assess_cgm(
    rows: list[dict[str, Any]],
    tz: ZoneInfo,
    *,
    start: datetime,
    end: datetime,
    as_of: date,
    expected_interval_minutes: int = 5,
) -> dict[str, Any]:
    valid, invalid, duplicate_count = _scan_cgm(rows, tz, start, end)
    expected = max(1, round((end - start).total_seconds() / (expected_interval_minutes * 60)))
    limitations = []
    if invalid:
        limitations.append(f"{invalid} readings without an interpretable timestamp or value were excluded.")
    if duplicate_count:
        limitations.append(f"{duplicate_count} duplicate timestamps were counted once.")
    return build_envelope(
        "cgm", observed=len(valid), expected=expected, unit="readings",
        data_through=max(point[0] for point in valid.values()).astimezone(tz).date() if valid else None,
        as_of=as_of, limitations=limitations,
        input_values=(f"{key}:{point[1]:g}" for key, point in valid.items()),
    )


def assess_daily(
    domain: str,
    rows: list[dict[str, Any]],
    tz: ZoneInfo,
    *,
    start_date: date,
    end_date: date,
    as_of: date,
    date_field: str = "date",
    required_fields: tuple[str, ...] = (),
    limitations: Iterable[str] = (),
) -> dict[str, Any]:
    valid_days = set()
    invalid = 0
    for row in rows:
        day = _date(row.get(date_field), tz)
        if (
            day is None
            or not start_date <= day <= end_date
            or (required_fields and not any(_usable(row.get(field)) for field in required_fields))
        ):
            invalid += 1
            continue
        valid_days.add(day)
    notes = list(limitations)
    if invalid:
        notes.append(f"{invalid} records outside the window or without required values were excluded.")
    return build_envelope(
        domain, observed=len(valid_days), expected=max(1, (end_date - start_date).days + 1),
        unit="days", data_through=max(valid_days) if valid_days else None, as_of=as_of,
        limitations=notes, input_values=(day.isoformat() for day in valid_days),
    )


def assess_nutrition(
    rows: list[dict[str, Any]], tz: ZoneInfo, *, start_date: date, end_date: date, as_of: date
) -> dict[str, Any]:
    valid_days = set()
    invalid = 0
    for row in rows:
        if str(row.get("type") or "").lower() != "carb":
            continue
        day = _date(row.get("timestamp"), tz)
        try:
            amount = float(row.get("amount"))
        except (TypeError, ValueError):
            amount = 0
        if day is None or not math.isfinite(amount) or amount <= 0:
            invalid += 1
        elif start_date <= day <= end_date:
            valid_days.add(day)
    blockers = ["carbohydrate records are uninterpretable"] if invalid and not valid_days else []
    limitations = ["Carbohydrate coverage describes logged entries only; missing days do not imply no intake."]
    if invalid:
        limitations.append(f"{invalid} carbohydrate records without a positive numeric amount were excluded.")
    return build_envelope(
        "nutrition", observed=len(valid_days), expected=max(1, (end_date - start_date).days + 1),
        unit="days_with_logs", data_through=max(valid_days) if valid_days else None, as_of=as_of,
        limitations=limitations, blocking_reasons=blockers,
        input_values=(day.isoformat() for day in valid_days),
    )


def assess_pump_tdd(
    reconciliation: dict[str, Any], *, start_date: date, end_date: date, as_of: date
) -> dict[str, Any]:
    days = [
        day for day in reconciliation.get("days", [])
        if start_date <= date.fromisoformat(day["date"]) <= end_date
    ]
    complete = [
        day for day in days
        if day["pump_reported"]["selected"] is not None or day["calculated"]["total_units"] is not None
    ]
    limitations = list(reconciliation.get("summary", {}).get("limitations", []))
    if len(complete) < len(days):
        limitations.append(f"{len(days) - len(complete)} observed insulin days lacked complete TDD.")
    return build_envelope(
        "pump_tdd", observed=len(complete), expected=max(1, (end_date - start_date).days + 1),
        unit="complete_days", data_through=max((date.fromisoformat(day["date"]) for day in complete), default=None),
        as_of=as_of, limitations=limitations,
        input_values=(f"{day['date']}:{day['completeness']}" for day in days),
    )
