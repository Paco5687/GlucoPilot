"""Meal response: what glucose did after each logged meal, from her own data.

Each meal is a carb Treatment written by the food logger (``food.py``): source
``manual`` with a ``food_name``. For each one the CGM trace around the meal
time is measured: the reading at the meal and its change over the prior half
hour, the low point in the first 90 minutes, when the rise starts (the first
reading at least ``RISE_MG_DL`` above that low point, held for the next
reading), and the peak.

Insulin action changes what the trace can show. Glucose already falling from a
bolus or a raised temp basal hides when food starts to arrive, so a meal is
only *readable* when it started from a steady or low number with no bolus or
temp basal near it. Only readable meals feed the summary. Every figure is a
median of temporal observations from one person's records, not a diagnosis or
dosing guidance.
"""

from __future__ import annotations

import bisect
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

ALGORITHM_VERSION = "meal-response/1.0.0"
PRIOR_MINUTES = 30
NADIR_MINUTES = 90
FOLLOW_MINUTES = 180
MIN_FOLLOW_READINGS = 18          # half of a 5-minute CGM's three hours
RISE_MG_DL = 10
STEADY_MG_DL = 15                 # |change over the prior 30 min| at or under this is steady
LOW_START_MG_DL = 80              # starting under this counts as starting low
LOW_BEFORE_MG_DL = 75
INSULIN_BEFORE_MINUTES = 60
INSULIN_AFTER_MINUTES = 120
WALK_MINUTES = 120
MATCH_TOLERANCE_SECONDS = 600
DUPLICATE_SECONDS = 120           # a second CGM source's copy of the same reading
MIN_GROUP = 5


def _instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_logged_meal(row: dict[str, Any]) -> bool:
    """A carb Treatment written by the food logger. Pure."""
    return (
        str(row.get("type") or "") == "carb"
        and str(row.get("source") or "") == "manual"
        and bool(row.get("food_name") or row.get("items"))
    )


def _is_bolus(row: dict[str, Any]) -> bool:
    return (
        str(row.get("type") or "") == "insulin"
        and str(row.get("event_type") or "").lower() != "daily total"
        and str(row.get("insulin_type") or "rapid").lower() != "long"
    )


class _Glucose:
    def __init__(self, readings: list[dict[str, Any]]):
        points = []
        for row in readings:
            t, v = _instant(row.get("timestamp")), _number(row.get("value"))
            if t is not None and v is not None:
                points.append((t, v))
        points.sort()
        self.times: list[datetime] = []
        self.values: list[float] = []
        for t, v in points:
            if self.times and (t - self.times[-1]).total_seconds() < DUPLICATE_SECONDS:
                continue
            self.times.append(t)
            self.values.append(v)

    def at(self, when: datetime) -> float | None:
        k = bisect.bisect_left(self.times, when)
        best = None
        for j in (k - 1, k):
            if 0 <= j < len(self.times):
                gap = abs((self.times[j] - when).total_seconds())
                if gap <= MATCH_TOLERANCE_SECONDS and (best is None or gap < best[0]):
                    best = (gap, j)
        return self.values[best[1]] if best else None

    def window(self, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        lo, hi = bisect.bisect_left(self.times, start), bisect.bisect_right(self.times, end)
        return list(zip(self.times[lo:hi], self.values[lo:hi]))


def _insulin_spans(treatments: list[dict[str, Any]]) -> tuple[list[tuple[datetime, datetime]], list[datetime]]:
    """Temp basal corrections as (start, end) spans and bolus instants."""
    temp_basals, boluses = [], []
    for row in treatments:
        t = _instant(row.get("timestamp"))
        if t is None:
            continue
        if str(row.get("type") or "") == "tempbasal_correction":
            minutes = _number(row.get("duration")) or 0
            temp_basals.append((t, t + timedelta(minutes=max(minutes, 0))))
        elif _is_bolus(row):
            boluses.append(t)
    return sorted(temp_basals), sorted(boluses)


def _walking_segments(intervals: list[dict[str, Any]]) -> list[tuple[datetime, datetime]]:
    segments = []
    for row in intervals:
        if str(row.get("activity") or "") != "walking":
            continue
        start, end = _instant(row.get("start_time")), _instant(row.get("end_time"))
        if start is not None and end is not None and end > start:
            segments.append((start, end))
    return sorted(segments)


def _overlap_minutes(segments: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> float:
    total = 0.0
    for s, e in segments:
        lo, hi = max(s, start), min(e, end)
        if hi > lo:
            total += (hi - lo).total_seconds() / 60
    return total


def _minutes(when: datetime, meal: datetime) -> int:
    return round((when - meal).total_seconds() / 60)


def measure_meal(meal_time: datetime, glucose: _Glucose, temp_basals: list[tuple[datetime, datetime]],
                 boluses: list[datetime], walking: list[tuple[datetime, datetime]]) -> dict[str, Any]:
    """One meal's trace and context. Pure."""
    before = meal_time - timedelta(minutes=INSULIN_BEFORE_MINUTES)
    after = meal_time + timedelta(minutes=INSULIN_AFTER_MINUTES)
    temp_basal = any(s <= after and e >= before for s, e in temp_basals)
    bolus = any(before <= t <= after for t in boluses)
    walk = _overlap_minutes(walking, meal_time, meal_time + timedelta(minutes=WALK_MINUTES))
    prior = glucose.window(meal_time - timedelta(minutes=PRIOR_MINUTES), meal_time - timedelta(seconds=1))
    out: dict[str, Any] = {
        "start_glucose": None, "trend_30": None, "nadir": None, "nadir_minutes": None,
        "rise_start_minutes": None, "rise_start_glucose": None, "peak": None, "peak_minutes": None, "rise": None,
        "temp_basal": temp_basal, "bolus": bolus, "walking_minutes": round(walk),
        "low_before": any(v < LOW_BEFORE_MG_DL for _, v in prior),
    }

    start = glucose.at(meal_time)
    earlier = glucose.at(meal_time - timedelta(minutes=PRIOR_MINUTES))
    follow = glucose.window(meal_time, meal_time + timedelta(minutes=FOLLOW_MINUTES))
    reasons = []
    if start is None or earlier is None:
        reasons.append("no_glucose")
    elif len(follow) < MIN_FOLLOW_READINGS:
        reasons.append("sparse_glucose")
    if temp_basal:
        reasons.append("temp_basal")
    if bolus:
        reasons.append("bolus")

    if start is not None:
        out["start_glucose"] = round(start)
    if start is not None and earlier is not None:
        trend = start - earlier
        out["trend_30"] = round(trend)
        if abs(trend) > STEADY_MG_DL and start >= LOW_START_MG_DL:
            reasons.append("falling" if trend < 0 else "rising")

    early = [p for p in follow if p[0] <= meal_time + timedelta(minutes=NADIR_MINUTES)]
    if early and len(follow) >= MIN_FOLLOW_READINGS:
        nadir_time, nadir = min(early, key=lambda p: (p[1], p[0]))
        out["nadir"], out["nadir_minutes"] = round(nadir), _minutes(nadir_time, meal_time)
        rest = [p for p in follow if p[0] >= nadir_time]
        for (t, v), (_, nxt) in zip(rest, rest[1:]):
            if v >= nadir + RISE_MG_DL and nxt >= nadir + RISE_MG_DL:
                out["rise_start_minutes"], out["rise_start_glucose"] = _minutes(t, meal_time), round(v)
                break
        peak_time, peak = max(rest, key=lambda p: (p[1], -p[0].timestamp()))
        out["peak"], out["peak_minutes"], out["rise"] = round(peak), _minutes(peak_time, meal_time), round(peak - nadir)

    out["readable"] = not reasons
    out["reasons"] = reasons
    return out


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(rows) < MIN_GROUP:
        return None

    def med(key: str) -> int | None:
        values = [r[key] for r in rows if r.get(key) is not None]
        return round(median(values)) if len(values) >= MIN_GROUP else None

    with_rise = [r for r in rows if r.get("rise_start_minutes") is not None]
    return {
        "n": len(rows),
        "n_with_rise": len(with_rise),
        "rise_start_minutes": med("rise_start_minutes"),
        "peak_minutes": round(median(r["peak_minutes"] for r in with_rise)) if len(with_rise) >= MIN_GROUP else None,
        "rise": round(median(r["rise"] for r in with_rise)) if len(with_rise) >= MIN_GROUP else None,
        "start_glucose": med("start_glucose"),
    }


def analyze(treatments: list[dict[str, Any]], readings: list[dict[str, Any]], intervals: list[dict[str, Any]], *,
            start: datetime, end: datetime) -> dict[str, Any]:
    """Meal response for meals logged in [start, end]. Pure and deterministic.

    ``treatments`` should reach back ``INSULIN_BEFORE_MINUTES`` before ``start``
    (plus any temp basal duration) and forward ``INSULIN_AFTER_MINUTES`` after
    ``end``; ``readings`` and ``intervals`` should cover the follow window."""
    glucose = _Glucose(readings)
    temp_basals, boluses = _insulin_spans(treatments)
    walking = _walking_segments(intervals)
    meals = []
    for row in treatments:
        when = _instant(row.get("timestamp"))
        if when is None or not (start <= when <= end) or not is_logged_meal(row):
            continue
        meals.append({
            "id": row.get("id"),
            "timestamp": _iso(when),
            "food_name": row.get("food_name"),
            "carbs": _number(row.get("amount")),
            **measure_meal(when, glucose, temp_basals, boluses, walking),
        })
    meals.sort(key=lambda m: (m["timestamp"], str(m["id"] or "")), reverse=True)
    readable = [m for m in meals if m["readable"]]
    reasons: dict[str, int] = {}
    for m in meals:
        for reason in m["reasons"]:
            reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "range": {"start": _iso(start), "end": _iso(end)},
        "meals_total": len(meals),
        "meals_readable": len(readable),
        "not_readable_reasons": dict(sorted(reasons.items())),
        "summary": _summary(readable),
        "min_group": MIN_GROUP,
        "meals": meals,
        "semantics": {
            "meal": "Carb entries from the food logger (spoken, typed, scanned, or preset), at the time they were logged for.",
            "rise_start": f"The first reading at least {RISE_MG_DL} mg/dL above the lowest reading in the first {NADIR_MINUTES} minutes, held for the next reading.",
            "peak": f"The highest reading from that low point to {FOLLOW_MINUTES} minutes after the meal; rise is peak minus the low point.",
            "readable": (
                f"Readable meals started steady (changed no more than {STEADY_MG_DL} mg/dL over the prior "
                f"{PRIOR_MINUTES} minutes) or under {LOW_START_MG_DL}, with no bolus or temp basal from "
                f"{INSULIN_BEFORE_MINUTES} minutes before to {INSULIN_AFTER_MINUTES} minutes after. Falling glucose "
                "from active insulin hides when the rise starts."
            ),
            "context": f"Walking is minutes of recorded walking in the {WALK_MINUTES} minutes after; low before is a reading under {LOW_BEFORE_MG_DL} in the {PRIOR_MINUTES} minutes before, often a low being treated.",
            "association": f"Medians across readable meals, shown once there are {MIN_GROUP}. Observations from one person's records; not a diagnosis and not dosing guidance.",
        },
    }
