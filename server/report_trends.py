"""History and timing sections for the Visit Report.

The report's headline numbers describe one window (90 days by default). A
clinician also needs how those numbers changed and when things happen:
glucose and insulin month by month, how the pump is used across the day,
when lows fall, how A1c compares with the CGM's estimate, and the imaging on
file. Everything here is a deterministic calculation over recorded data.
All functions are pure; the report supplies the rows.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from statistics import mean, median, pstdev
from typing import Any
from zoneinfo import ZoneInfo

from .clinician_briefs import low_episodes
from .lab_audit import effective_flag

HISTORY_MONTHS = 12
READINGS_PER_DAY = 288
LOW_MG_DL = 70
VERY_LOW_MG_DL = 54
HIGH_MG_DL = 180
VERY_HIGH_MG_DL = 250
# The shortest run of hours holding this share of low readings is reported.
LOW_SPAN_SHARE = 0.8
OVERNIGHT_END_HOUR = 6
# A night counts when at least half of its 00:00–06:00 readings exist.
OVERNIGHT_MIN_READINGS = 36
A1C_LOOKBACK_DAYS = 90
A1C_MIN_CGM_DAYS = 14
# A month's U/kg uses that month's weight, else the latest within this many
# days before the month starts.
WEIGHT_CARRY_DAYS = 62
MAX_MODE_SEGMENT_MINUTES = 24 * 60
IMAGING_DOC_TYPES = {"imaging_report", "radiology_report"}


def history_start(today: date) -> date:
    """First day of the month HISTORY_MONTHS - 1 months before today's."""
    months = today.year * 12 + today.month - 1 - (HISTORY_MONTHS - 1)
    return date(months // 12, months % 12 + 1, 1)


def is_imaging_category(category: Any) -> bool:
    return str(category or "").strip().lower().startswith("imaging")


def _gmi(average: float) -> float:
    return round(3.31 + 0.02392 * average, 1)


def _pct(count: int, total: int) -> float:
    return round(count / total * 100, 1) if total else 0.0


def glucose_by_month(points: list[tuple[datetime, float]], tz: ZoneInfo) -> list[dict[str, Any]]:
    """CGM metrics for each local calendar month with readings."""
    by_month: dict[str, list[tuple[datetime, float]]] = {}
    for instant, value in points:
        by_month.setdefault(instant.astimezone(tz).strftime("%Y-%m"), []).append((instant, value))
    rows = []
    for month, month_points in sorted(by_month.items()):
        values = [value for _, value in month_points]
        days = sorted({instant.astimezone(tz).date() for instant, _ in month_points})
        spanned = (days[-1] - days[0]).days + 1
        average = mean(values)
        count = len(values)
        rows.append({
            "month": month,
            "first_day": days[0].isoformat(),
            "last_day": days[-1].isoformat(),
            "days": len(days),
            "readings": count,
            "coverage_pct": round(min(100.0, count / (spanned * READINGS_PER_DAY) * 100), 1),
            "avg": round(average),
            "gmi": _gmi(average),
            "cv": round(pstdev(values) / average * 100, 1) if average else None,
            "tir": _pct(sum(1 for v in values if LOW_MG_DL <= v <= HIGH_MG_DL), count),
            "tbr70": _pct(sum(1 for v in values if v < LOW_MG_DL), count),
            "tbr54": _pct(sum(1 for v in values if v < VERY_LOW_MG_DL), count),
            "tar180": _pct(sum(1 for v in values if v > HIGH_MG_DL), count),
            "tar250": _pct(sum(1 for v in values if v > VERY_HIGH_MG_DL), count),
            "low_episodes": low_episodes(month_points, tz)["episodes"],
        })
    return rows


def _shortest_span(counts: list[int], share: float) -> dict[str, Any] | None:
    """The shortest run of consecutive hours (wrapping midnight) holding at
    least ``share`` of the counts; among equal lengths, the largest share."""
    total = sum(counts)
    if not total:
        return None
    for length in range(1, 25):
        best = None
        for start in range(24):
            held = sum(counts[(start + offset) % 24] for offset in range(length))
            if held >= share * total and (best is None or held > best[1]):
                best = (start, held)
        if best:
            return {
                "start_hour": best[0],
                "end_hour": (best[0] + length) % 24,
                "share_pct": round(best[1] / total * 100),
            }
    return None


def lows_by_hour(points: list[tuple[datetime, float]], tz: ZoneInfo) -> dict[str, Any]:
    """When readings below 70 fall: the share below 70 in each local hour,
    the shortest span of hours holding most low readings, and how many
    nights had any low between midnight and 06:00."""
    totals = [0] * 24
    lows = [0] * 24
    nights: dict[date, list[int]] = {}
    for instant, value in points:
        local = instant.astimezone(tz)
        totals[local.hour] += 1
        low = value < LOW_MG_DL
        lows[local.hour] += low
        if local.hour < OVERNIGHT_END_HOUR:
            night = nights.setdefault(local.date(), [0, 0])
            night[0] += 1
            night[1] += low
    counted = [night for night in nights.values() if night[0] >= OVERNIGHT_MIN_READINGS]
    return {
        "pct_below_70_by_hour": [round(lows[h] / totals[h] * 100, 1) if totals[h] else None for h in range(24)],
        "low_readings": sum(lows),
        "span": _shortest_span(lows, LOW_SPAN_SHARE),
        "span_share_target_pct": round(LOW_SPAN_SHARE * 100),
        "overnight": {
            "nights": len(counted),
            "nights_with_low": sum(1 for night in counted if night[1]),
            "hours": f"00:00–{OVERNIGHT_END_HOUR:02d}:00",
        },
    }


def _instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _longest_run(flags: list[bool]) -> dict[str, int] | None:
    """Longest run of True hours, wrapping midnight."""
    if all(flags):
        return {"start_hour": 0, "end_hour": 0, "hours": 24}
    best = None
    for start in range(24):
        if not flags[start] or flags[start - 1]:
            continue
        length = 0
        while length < 24 and flags[(start + length) % 24]:
            length += 1
        if best is None or length > best["hours"]:
            best = {"start_hour": start, "end_hour": (start + length) % 24, "hours": length}
    return best


def pump_use(treatments: list[dict[str, Any]], tz: ZoneInfo, days: int) -> dict[str, Any] | None:
    """How the pump is run across the day: minutes in each mode by local
    hour, the hours mostly in manual mode, and how temp basals are set."""
    minutes = {"automatic": [0.0] * 24, "manual": [0.0] * 24}
    mode_days: set[date] = set()
    seen: set[tuple[str, str]] = set()
    for row in treatments:
        if row.get("type") != "pump_mode":
            continue
        mode = str(row.get("mode") or "").lower()
        start, duration = _instant(row.get("timestamp")), _number(row.get("duration"))
        key = (str(row.get("timestamp")), mode)
        if mode not in minutes or start is None or not duration or duration <= 0 or key in seen:
            continue
        seen.add(key)
        cursor = start.astimezone(tz)
        remaining = min(duration, MAX_MODE_SEGMENT_MINUTES)
        while remaining > 0:
            mode_days.add(cursor.date())
            to_next_hour = 60 - cursor.minute - cursor.second / 60
            chunk = min(remaining, to_next_hour)
            minutes[mode][cursor.hour] += chunk
            cursor += timedelta(minutes=chunk)
            remaining -= chunk
    total = sum(sum(values) for values in minutes.values())

    # Counted as the report's temp-basal tile counts them: a rate, an
    # increase and a duration, once per (time, rate, increase, duration).
    corrections = []
    seen_corrections: set[tuple[Any, ...]] = set()
    for row in treatments:
        if row.get("type") != "tempbasal_correction":
            continue
        rate = _number(row.get("absolute"))
        multiplier, length = _number(row.get("multiplier")), _number(row.get("duration"))
        key = (row.get("timestamp"), rate, multiplier, length)
        if rate is None or not multiplier or multiplier <= 1 or not length or length <= 0 or key in seen_corrections:
            continue
        seen_corrections.add(key)
        corrections.append((round((multiplier - 1) * 100), length))
    if not total and not corrections:
        return None

    modes = None
    if total:
        share = [
            minutes["manual"][h] / (minutes["manual"][h] + minutes["automatic"][h])
            if minutes["manual"][h] + minutes["automatic"][h] else 0.0
            for h in range(24)
        ]
        modes = {
            "days_with_mode_records": len(mode_days),
            "automated_pct": round(sum(minutes["automatic"]) / total * 100),
            "manual_pct": round(sum(minutes["manual"]) / total * 100),
            "manual_share_by_hour": [round(value * 100) for value in share],
            "manual_span": _longest_run([value >= 0.5 for value in share]),
        }
    return {
        "modes": modes,
        "temp_basals": {
            "count": len(corrections),
            "per_day": round(len(corrections) / max(days, 1), 1),
            "median_increase_pct": round(median(pct for pct, _ in corrections)),
            "median_minutes": round(median(length for _, length in corrections)),
        } if corrections else None,
    }


def _weight_for_month(month: str, series: list[dict[str, Any]]) -> dict[str, Any] | None:
    first = date.fromisoformat(f"{month}-01")
    inside = [point for point in series if point["date"][:7] == month]
    if inside:
        return {**inside[-1], "carried": False}
    floor = (first - timedelta(days=WEIGHT_CARRY_DAYS)).isoformat()
    before = [point for point in series if floor <= point["date"] < first.isoformat()]
    return {**before[-1], "carried": True} if before else None


def insulin_by_month(reconciled_days: list[dict[str, Any]], weight_series: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pump-reported daily totals averaged per month, with insulin per kg
    from that month's weight."""
    by_month: dict[str, list[dict[str, Any]]] = {}
    for day in reconciled_days:
        selected = (day.get("pump_reported") or {}).get("selected")
        if selected and selected.get("total_units") is not None:
            by_month.setdefault(str(day["date"])[:7], []).append(selected)
    rows = []
    for month, totals in sorted(by_month.items()):
        def avg(key: str) -> float | None:
            values = [float(t[key]) for t in totals if t.get(key) is not None]
            return round(mean(values), 1) if values else None

        tdd = avg("total_units")
        weight = _weight_for_month(month, weight_series)
        rows.append({
            "month": month,
            "days": len(totals),
            "tdd": tdd,
            "basal": avg("basal_units"),
            "bolus": avg("bolus_units"),
            "weight_kg": weight["weight_kg"] if weight else None,
            "weight_date": weight["date"] if weight else None,
            "weight_carried": weight["carried"] if weight else None,
            "units_per_kg": round(tdd / weight["weight_kg"], 2) if weight and tdd else None,
        })
    return rows


def a1c_vs_gmi(a1c_points: list[dict[str, Any]], points: list[tuple[datetime, float]], tz: ZoneInfo) -> list[dict[str, Any]]:
    """Each A1c beside the GMI of the CGM readings in the 90 days before its
    draw, when at least A1C_MIN_CGM_DAYS of readings exist."""
    rows = []
    for draw in sorted(a1c_points, key=lambda point: point["date"]):
        drawn = date.fromisoformat(draw["date"])
        start = drawn - timedelta(days=A1C_LOOKBACK_DAYS)
        window = [
            (instant.astimezone(tz).date(), value) for instant, value in points
            if start <= instant.astimezone(tz).date() < drawn
        ]
        days = len({day for day, _ in window})
        if days < A1C_MIN_CGM_DAYS:
            continue
        gmi = _gmi(mean(value for _, value in window))
        rows.append({
            "date": draw["date"],
            "a1c": draw["value"],
            "gmi": gmi,
            "difference": round(draw["value"] - gmi, 1),
            "cgm_days": days,
        })
    return rows


def imaging_studies(records: list[dict[str, Any]], lab_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Imaging reports on file, newest first, with any measurements their
    extraction recorded."""
    measurements: dict[str, list[dict[str, Any]]] = {}
    for row in lab_rows:
        if is_imaging_category(row.get("category")) and row.get("record_id"):
            measurements.setdefault(str(row["record_id"]), []).append({
                "name": " ".join(str(row.get("test_name") or "").split()),
                "value": row.get("value"),
                "unit": row.get("unit") or "",
                "reference_low": row.get("reference_low"),
                "reference_high": row.get("reference_high"),
                "flag": effective_flag(row)[0],
            })
    studies = []
    for record in records:
        if str(record.get("doc_type") or "").lower() not in IMAGING_DOC_TYPES:
            continue
        studies.append({
            "id": record.get("id"),
            "title": record.get("title") or record.get("filename") or "Imaging report",
            "date": record.get("record_date") or None,
            "summary": (record.get("summary") or "").strip() or None,
            "measurements": measurements.get(str(record.get("id")), []),
        })
    studies.sort(key=lambda study: study["date"] or "", reverse=True)
    return studies
