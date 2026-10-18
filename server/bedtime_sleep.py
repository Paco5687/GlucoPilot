"""Bedtime glucose and that night's sleep, measured from her own CGM and Oura data.

Each night is anchored on the local evening it starts. Bedtime glucose is the
mean CGM reading across a fixed bedtime window (22:30 to 00:30 local): the
Oura rows stored here carry sleep durations and scores but no bedtime, so the
window stands in for it. The night's sleep is the Oura day the night ends on,
which is Oura's own convention (a night belongs to the morning it ends).

Nights are grouped into bedtime-glucose bands so "a high bedtime means no
sleep" can be checked against how she actually slept after nights in each
band, and against the next day's mean glucose. Where a dinner time is logged
(a carbohydrate entry between 16:00 and the bedtime window), late and earlier
dinners are compared the same way. All figures are medians of temporal
associations, not causal or clinical guidance.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from statistics import mean, median
from typing import Any
from zoneinfo import ZoneInfo

ALGORITHM_VERSION = "bedtime-sleep/1.0.0"
BEDTIME_START = time(22, 30)
BEDTIME_MINUTES = 120
MIN_BEDTIME_POINTS = 12  # half of a 5-minute cadence across the window
NEXT_DAY_START = time(6, 0)
NEXT_DAY_END = time(22, 0)
MIN_NEXT_DAY_POINTS = 96  # half of a 5-minute cadence across 06:00-22:00
DINNER_FROM = time(16, 0)
LATE_DINNER_FROM = time(20, 0)
HIGH_BEDTIME_MG_DL = 180
MIN_GROUP = 5
BANDS = (("under_140", None, 140), ("140_to_180", 140, 180), ("over_180", 180, None))


def _instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _points(readings: list[dict[str, Any]]) -> list[tuple[datetime, float]]:
    unique: dict[datetime, float] = {}
    for row in readings:
        instant = _instant(row.get("timestamp"))
        value = _number(row.get("value"))
        if instant is not None and value is not None:
            unique.setdefault(instant, value)
    return sorted(unique.items())


def _between(points: list[tuple[datetime, float]], start: datetime, end: datetime) -> list[float]:
    return [value for instant, value in points if start <= instant < end]


def _band(value: float) -> str:
    for name, low, high in BANDS:
        if (low is None or value > low) and (high is None or value <= high):
            return name
    return "unknown"


def build_nights(
    readings: list[dict[str, Any]],
    sleep_days: list[dict[str, Any]],
    treatments: list[dict[str, Any]],
    *,
    start: date,
    end: date,
    timezone_name: str = "UTC",
) -> list[dict[str, Any]]:
    """One row per night starting on a local date in [start, end] with enough
    CGM coverage at bedtime. Pure."""
    tz = ZoneInfo(timezone_name)
    points = _points(readings)
    sleep_by_day = {
        str(row.get("date") or "")[:10]: row
        for row in sleep_days
        if row.get("sleep_total_seconds") is not None
    }
    carbs = sorted(
        instant
        for row in treatments
        if row.get("type") == "carb" and (instant := _instant(row.get("timestamp"))) is not None
    )
    nights = []
    day = start
    while day <= end:
        bedtime = datetime.combine(day, BEDTIME_START, tzinfo=tz).astimezone(timezone.utc)
        window = _between(points, bedtime, bedtime + timedelta(minutes=BEDTIME_MINUTES))
        if len(window) >= MIN_BEDTIME_POINTS:
            morning = day + timedelta(days=1)
            sleep = sleep_by_day.get(morning.isoformat())
            next_day = _between(
                points,
                datetime.combine(morning, NEXT_DAY_START, tzinfo=tz).astimezone(timezone.utc),
                datetime.combine(morning, NEXT_DAY_END, tzinfo=tz).astimezone(timezone.utc),
            )
            dinner_from = datetime.combine(day, DINNER_FROM, tzinfo=tz).astimezone(timezone.utc)
            dinners = [instant for instant in carbs if dinner_from <= instant < bedtime]
            dinner = dinners[-1].astimezone(tz) if dinners else None
            bedtime_glucose = mean(window)
            sleep_id = str(sleep.get("id") or "") if sleep else ""
            nights.append({
                "night": day.isoformat(),
                "bedtime_glucose": round(bedtime_glucose),
                "band": _band(bedtime_glucose),
                "sleep_day": morning.isoformat() if sleep else None,
                "sleep_entity_id": sleep_id or None,
                "sleep_hours": (
                    round(float(sleep["sleep_total_seconds"]) / 3600, 2) if sleep else None
                ),
                "sleep_score": _number(sleep.get("sleep_score")) if sleep else None,
                "sleep_efficiency": _number(sleep.get("sleep_efficiency")) if sleep else None,
                "next_day_glucose": (
                    round(mean(next_day)) if len(next_day) >= MIN_NEXT_DAY_POINTS else None
                ),
                "dinner_time": dinner.strftime("%H:%M") if dinner else None,
                "late_dinner": (
                    dinner.time() >= LATE_DINNER_FROM if dinner else None
                ),
            })
        day += timedelta(days=1)
    return nights


def _med(rows: list[dict[str, Any]], key: str, digits: int = 0) -> float | None:
    values = [row[key] for row in rows if row.get(key) is not None]
    if not values:
        return None
    value = median(values)
    return round(value, digits) if digits else round(value)


def _group(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(rows) < MIN_GROUP:
        return None
    slept = [row for row in rows if row.get("sleep_hours") is not None]
    return {
        "nights": len(rows),
        "nights_with_sleep": len(slept),
        "median_bedtime_glucose": _med(rows, "bedtime_glucose"),
        "median_sleep_hours": _med(slept, "sleep_hours", 1),
        "median_sleep_score": _med(slept, "sleep_score"),
        "median_sleep_efficiency": _med(slept, "sleep_efficiency"),
        "median_next_day_glucose": _med(rows, "next_day_glucose"),
    }


def analyze(
    readings: list[dict[str, Any]],
    sleep_days: list[dict[str, Any]],
    treatments: list[dict[str, Any]],
    *,
    start: date,
    end: date,
    timezone_name: str = "UTC",
) -> dict[str, Any]:
    """Bedtime glucose against that night's sleep over [start, end]. Pure."""
    nights = build_nights(
        readings, sleep_days, treatments, start=start, end=end, timezone_name=timezone_name
    )
    dinners = [row for row in nights if row["dinner_time"] is not None]
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "nights_measured": len(nights),
        "nights_with_sleep": sum(1 for row in nights if row["sleep_hours"] is not None),
        "first_night": nights[0]["night"] if nights else None,
        "last_night": nights[-1]["night"] if nights else None,
        "highest_bedtime_glucose": max((row["bedtime_glucose"] for row in nights), default=None),
        "nights_over_180": sum(1 for row in nights if row["bedtime_glucose"] > HIGH_BEDTIME_MG_DL),
        "bands": {name: _group([row for row in nights if row["band"] == name]) for name, _, _ in BANDS},
        "band_nights": {name: sum(1 for row in nights if row["band"] == name) for name, _, _ in BANDS},
        "dinner": {
            "nights_logged": len(dinners),
            "late": _group([row for row in dinners if row["late_dinner"]]),
            "earlier": _group([row for row in dinners if not row["late_dinner"]]),
        },
        "nights": nights,
        "semantics": {
            "bedtime": "Bedtime glucose is the mean CGM reading from 22:30 to 00:30 local time; nights with under an hour of readings in that window are not counted.",
            "sleep": "Sleep is the Oura main sleep period for the morning the night ends on.",
            "next_day": "Next-day glucose is the mean CGM reading from 06:00 to 22:00 local time on the following day.",
            "dinner": "Dinner time is the last carbohydrate entry between 16:00 and the bedtime window; a dinner at or after 20:00 counts as late.",
            "association": "Medians of temporal associations from one person's records; not causal and not treatment guidance.",
        },
    }
