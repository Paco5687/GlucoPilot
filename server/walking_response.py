"""Walking response: what glucose does after a walk, measured from her own data.

Inferred walking intervals arrive as short step-bearing bouts (often 1–3
minutes). Here they are merged into *walks* — bouts separated by less than
``EPISODE_GAP_MINUTES`` — and only walks of at least ``MIN_WALK_MINUTES``
count. For each walk the glucose trajectory after its start is measured at
fixed offsets, plus the lowest point within two hours, the value at the
moment the walk ended, and how much further glucose fell *after* stopping.

Every walk is compared against matched sitting controls: half-hour marks
with no walking within the following two hours, taken from the same
starting-glucose band, so "walking lowers glucose" is stated against what the
same body does at rest. All figures are medians of temporal associations,
not causal or clinical guidance.
"""

from __future__ import annotations

import bisect
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

ALGORITHM_VERSION = "walking-response/1.0.0"
EPISODE_GAP_MINUTES = 8
MIN_WALK_MINUTES = 8
OFFSETS_MINUTES = (15, 30, 60, 90, 120)
FOLLOW_MINUTES = 120
LOW_MG_DL = 70
MATCH_TOLERANCE_SECONDS = 600
MIN_GROUP = 5
BANDS = (("under_120", None, 120), ("120_to_170", 120, 170), ("over_170", 170, None))
DURATIONS = (("under_15", 0, 15), ("15_to_30", 15, 30), ("30_to_60", 30, 60), ("over_60", 60, None))


def _instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def build_walks(intervals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge short walking bouts into walks. Pure."""
    bouts = []
    for row in intervals:
        if str(row.get("activity") or "") != "walking":
            continue
        start, end = _instant(row.get("start_time")), _instant(row.get("end_time"))
        if start is None or end is None or end <= start:
            continue
        bouts.append((start, end))
    bouts.sort()
    walks: list[dict[str, Any]] = []
    for start, end in bouts:
        if walks and (start - walks[-1]["end"]).total_seconds() <= EPISODE_GAP_MINUTES * 60:
            walks[-1]["end"] = max(walks[-1]["end"], end)
            walks[-1]["bouts"] += 1
        else:
            walks.append({"start": start, "end": end, "bouts": 1})
    output = []
    for walk in walks:
        minutes = (walk["end"] - walk["start"]).total_seconds() / 60
        if minutes >= MIN_WALK_MINUTES:
            output.append({**walk, "minutes": round(minutes, 1)})
    return output


class _Glucose:
    def __init__(self, readings: list[dict[str, Any]]):
        points = []
        for row in readings:
            t = _instant(row.get("timestamp"))
            try:
                v = float(row["value"])
            except (KeyError, TypeError, ValueError):
                continue
            if t is not None:
                points.append((t, v))
        points.sort()
        self.times = [p[0] for p in points]
        self.values = [p[1] for p in points]

    def at(self, when: datetime) -> float | None:
        k = bisect.bisect_left(self.times, when)
        best = None
        for j in (k - 1, k):
            if 0 <= j < len(self.times) and abs((self.times[j] - when).total_seconds()) <= MATCH_TOLERANCE_SECONDS:
                if best is None or abs((self.times[j] - when).total_seconds()) < abs((self.times[best] - when).total_seconds()):
                    best = j
        return self.values[best] if best is not None else None

    def window(self, start: datetime, end: datetime) -> list[float]:
        return self.values[bisect.bisect_left(self.times, start):bisect.bisect_left(self.times, end)]


def _measure(glucose: _Glucose, start: datetime, end: datetime) -> dict[str, Any] | None:
    start_value = glucose.at(start)
    if start_value is None:
        return None
    follow = glucose.window(start, start + timedelta(minutes=FOLLOW_MINUTES))
    if len(follow) < 12:
        return None
    end_value = glucose.at(end)
    after = glucose.window(end, end + timedelta(minutes=FOLLOW_MINUTES))
    deltas = {}
    for m in OFFSETS_MINUTES:
        v = glucose.at(start + timedelta(minutes=m))
        deltas[f"delta_{m}"] = round(v - start_value) if v is not None else None
    return {
        "start_glucose": round(start_value),
        **deltas,
        "nadir_delta": round(min(follow) - start_value),
        "went_low": min(follow) < LOW_MG_DL,
        "at_stop_delta": round(end_value - start_value) if end_value is not None else None,
        "after_stop_delta": round(min(after) - end_value) if (after and end_value is not None) else None,
        "went_low_after_stop": bool(after) and min(after) < LOW_MG_DL,
    }


def _band(value: float) -> str:
    for name, lo, hi in BANDS:
        if (lo is None or value > lo) and (hi is None or value <= hi):
            return name
    return "unknown"


def _duration_bucket(minutes: float) -> str:
    for name, lo, hi in DURATIONS:
        if minutes >= lo and (hi is None or minutes < hi):
            return name
    return "unknown"


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(rows) < MIN_GROUP:
        return None

    def med(key: str) -> float | None:
        values = [r[key] for r in rows if r.get(key) is not None]
        return round(median(values)) if values else None

    out: dict[str, Any] = {"n": len(rows), "nadir_delta": med("nadir_delta"),
                           "went_low_pct": round(100 * sum(1 for r in rows if r["went_low"]) / len(rows))}
    for m in OFFSETS_MINUTES:
        out[f"delta_{m}"] = med(f"delta_{m}")
    if any("minutes" in r for r in rows):
        out["minutes"] = round(median(r["minutes"] for r in rows))
        out["at_stop_delta"] = med("at_stop_delta")
        out["after_stop_delta"] = med("after_stop_delta")
        after = [r for r in rows if r.get("after_stop_delta") is not None]
        out["went_low_after_stop_pct"] = round(100 * sum(1 for r in after if r["went_low_after_stop"]) / len(after)) if after else None
    return out


def _controls(walks: list[dict[str, Any]], glucose: _Glucose, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Half-hour marks with no walking in the following two hours."""
    walk_starts = [w["start"] for w in walks]
    out = []
    cursor = start.replace(minute=0, second=0, microsecond=0)
    while cursor < end:
        k = bisect.bisect_left(walk_starts, cursor - timedelta(minutes=FOLLOW_MINUTES))
        clear = True
        while k < len(walks) and walks[k]["start"] < cursor + timedelta(minutes=FOLLOW_MINUTES):
            if walks[k]["end"] > cursor:
                clear = False
                break
            k += 1
        if clear:
            m = _measure(glucose, cursor, cursor + timedelta(minutes=20))
            if m:
                out.append({**m, "hour": cursor.hour})
        cursor += timedelta(minutes=30)
    return out


def analyze(intervals: list[dict[str, Any]], readings: list[dict[str, Any]], *, start: datetime, end: datetime,
            timezone_name: str = "UTC") -> dict[str, Any]:
    """Walking response over [start, end]. Pure and deterministic."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(timezone_name)
    glucose = _Glucose(readings)
    walks = build_walks(intervals)
    measured = []
    for walk in walks:
        m = _measure(glucose, walk["start"], walk["end"])
        if m:
            measured.append({**m, "minutes": walk["minutes"], "hour": walk["start"].astimezone(tz).hour,
                             "start": walk["start"].isoformat().replace("+00:00", "Z")})
    controls = _controls(walks, glucose, start, end)

    bands = {}
    for name, _, _ in BANDS:
        walk_rows = [r for r in measured if _band(r["start_glucose"]) == name]
        control_rows = [r for r in controls if _band(r["start_glucose"]) == name]
        bands[name] = {"walking": _summary(walk_rows), "sitting": _summary(control_rows)}
    mid = [r for r in measured if _band(r["start_glucose"]) == "120_to_170"]
    durations = {name: _summary([r for r in mid if _duration_bucket(r["minutes"]) == name]) for name, _, _ in DURATIONS}
    time_of_day = {
        "morning": _summary([r for r in mid if 5 <= r["hour"] < 12]),
        "afternoon": _summary([r for r in mid if 12 <= r["hour"] < 18]),
        "evening": _summary([r for r in mid if r["hour"] >= 18 or r["hour"] < 5]),
    }
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "walks_total": len(walks),
        "walks_measured": len(measured),
        "controls": len(controls),
        "median_walk_minutes": round(median(r["minutes"] for r in measured)) if measured else None,
        "bands": bands,
        "durations_120_to_170": durations,
        "time_of_day_120_to_170": time_of_day,
        "semantics": {
            "walk": f"Inferred walking bouts merged when separated by under {EPISODE_GAP_MINUTES} minutes; walks under {MIN_WALK_MINUTES} minutes are not counted.",
            "response": "Deltas are glucose relative to the walk's starting reading; nadir is the lowest reading within two hours of the start; after-stop is the further fall from the reading at the walk's end.",
            "control": "Sitting controls are half-hour marks with no walking in the following two hours, compared within the same starting-glucose band.",
            "association": "Medians of temporal associations from one person's records; not causal and not dosing or activity guidance.",
        },
    }
