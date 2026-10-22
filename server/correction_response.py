"""Observed correction response: how much glucose actually moved per unit of
insulin deliberately used to bring it down.

Two correction methods are recognised and measured the same way, so the page
can show both what each achieves and how use shifts between them over time:

- **temp basal** — a temporary basal increase (Omnipod manual mode). Extra
  insulin is what the raised rate delivered beyond the scheduled rate:
  ``rate * (1 - 1/multiplier) * hours``. Contiguous segments (gaps under
  15 min) form one episode.
- **bolus** — a standalone correction bolus: no carbohydrate entry within
  45 minutes and no other bolus in the prior 2 hours.

Response for either = starting glucose minus the lowest reading in the
following 3 hours, per extra unit. Episodes with carbohydrates inside that
window are kept but marked confounded and left out of the summaries. This is
an observational effect size, not an insulin-sensitivity factor: corrections
launched near range cannot fall far (a floor effect), which is why the
starting glucose travels with every episode and every weekly summary.
"""

from __future__ import annotations

import bisect
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any

RESPONSE_WINDOW_MINUTES = 180
EPISODE_GAP_MINUTES = 15
CARB_EXCLUSION_MINUTES = 45
PRIOR_BOLUS_MINUTES = 120
MIN_EXTRA_UNITS = 0.5
MIN_CGM_POINTS = 24  # ~2 of the 3 hours at 5-minute cadence
HIGH_START_MG_DL = 150
CONFIDENT_WEEK_EPISODES = 5
ALGORITHM_VERSION = "correction-response/1.0.0"


def _ts(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _temp_basal_episodes(treatments: list[dict]) -> list[dict]:
    rows = []
    for t in treatments:
        if t.get("type") != "tempbasal_correction" or not t.get("multiplier"):
            continue
        start = _ts(t.get("timestamp"))
        rate = t.get("absolute")
        minutes = t.get("duration")
        multiplier = float(t["multiplier"])
        if start is None or rate is None or not minutes or multiplier <= 1.0:
            continue
        extra = float(rate) * (1 - 1 / multiplier) * float(minutes) / 60
        rows.append((start, start + timedelta(minutes=float(minutes)), extra, str(t.get("id") or "")))
    rows.sort()
    episodes: list[dict] = []
    for start, end, extra, source_id in rows:
        if episodes and (start - episodes[-1]["end"]).total_seconds() < EPISODE_GAP_MINUTES * 60:
            episodes[-1]["end"] = max(episodes[-1]["end"], end)
            episodes[-1]["units"] += extra
            episodes[-1]["source_ids"].append(source_id)
        else:
            episodes.append({"method": "temp_basal", "start": start, "end": end, "units": extra,
                             "source_ids": [source_id]})
    return episodes


def _bolus_episodes(treatments: list[dict]) -> list[dict]:
    boluses, carbs = [], []
    for t in treatments:
        start = _ts(t.get("timestamp"))
        if start is None:
            continue
        if t.get("event_type") == "Bolus" and t.get("amount"):
            boluses.append((start, float(t["amount"]), str(t.get("id") or "")))
        elif t.get("type") == "carb" and t.get("amount"):
            carbs.append(start)
    boluses.sort()
    carbs.sort()
    episodes = []
    for i, (start, amount, source_id) in enumerate(boluses):
        j = bisect.bisect_left(carbs, start - timedelta(minutes=CARB_EXCLUSION_MINUTES))
        if j < len(carbs) and carbs[j] <= start + timedelta(minutes=CARB_EXCLUSION_MINUTES):
            continue  # a meal bolus, not a correction
        if i > 0 and (start - boluses[i - 1][0]).total_seconds() < PRIOR_BOLUS_MINUTES * 60:
            continue  # stacked on an earlier bolus; its response is not separable
        episodes.append({"method": "bolus", "start": start, "end": start, "units": amount,
                         "source_ids": [source_id]})
    return episodes


def build_correction_episodes(treatments: list[dict], readings: list[dict]) -> list[dict]:
    """Every correction episode with its measured response. Pure."""
    points = sorted(
        ((_ts(r.get("timestamp")), float(r["value"])) for r in readings if r.get("value") is not None and _ts(r.get("timestamp"))),
    )
    times = [p[0] for p in points]
    values = [p[1] for p in points]
    carb_times = sorted(_ts(t.get("timestamp")) for t in treatments if t.get("type") == "carb" and _ts(t.get("timestamp")))

    episodes = _temp_basal_episodes(treatments) + _bolus_episodes(treatments)
    episodes.sort(key=lambda e: e["start"])
    output = []
    for e in episodes:
        if e["units"] < MIN_EXTRA_UNITS:
            continue
        window_end = e["start"] + timedelta(minutes=RESPONSE_WINDOW_MINUTES)
        i0 = bisect.bisect_left(times, e["start"])
        i1 = bisect.bisect_left(times, window_end)
        if i1 - i0 < MIN_CGM_POINTS or i0 >= len(values):
            continue
        start_bg = values[i0]
        nadir = min(values[i0:i1])
        k = bisect.bisect_left(carb_times, e["start"])
        confounded = k < len(carb_times) and carb_times[k] <= window_end
        output.append({
            "method": e["method"],
            "start": _iso(e["start"]),
            "minutes": round((e["end"] - e["start"]).total_seconds() / 60),
            "units": round(e["units"], 2),
            "start_glucose": start_bg,
            "nadir_glucose": nadir,
            "drop_per_unit": round((start_bg - nadir) / e["units"], 1),
            "confounded": confounded,
            "source_ids": [source_id for source_id in e["source_ids"] if source_id],
        })
    return output


def weekly_correction_series(episodes: list[dict], end: date, weeks: int) -> list[dict]:
    """Per week: episode counts and units by method, and the median response
    per unit by method — with the starting glucose that explains it."""
    clean = [e for e in episodes if not e["confounded"]]
    series = []
    for k in range(weeks - 1, -1, -1):
        week_end = end - timedelta(days=7 * k)
        week_start = week_end - timedelta(days=6)
        rows = [e for e in clean if week_start <= date.fromisoformat(e["start"][:10]) <= week_end]
        if not rows:
            continue
        entry: dict[str, Any] = {"date": week_end.isoformat(), "n": len(rows)}
        for method in ("temp_basal", "bolus"):
            sub = [e for e in rows if e["method"] == method]
            entry[f"n_{method}"] = len(sub)
            entry[f"units_{method}"] = round(sum(e["units"] for e in sub), 1)
            entry[f"drop_per_unit_{method}"] = round(median(e["drop_per_unit"] for e in sub), 1) if sub else None
        entry["drop_per_unit"] = round(median(e["drop_per_unit"] for e in rows), 1)
        entry["median_start_glucose"] = round(median(e["start_glucose"] for e in rows))
        high = [e for e in rows if e["start_glucose"] >= HIGH_START_MG_DL]
        low = [e for e in rows if e["start_glucose"] < HIGH_START_MG_DL]
        entry["drop_per_unit_from_high"] = round(median(e["drop_per_unit"] for e in high), 1) if high else None
        entry["drop_per_unit_from_low"] = round(median(e["drop_per_unit"] for e in low), 1) if low else None
        entry["confident"] = len(rows) >= CONFIDENT_WEEK_EPISODES
        series.append(entry)
    return series


TIMECOURSE_OFFSETS_MINUTES = (30, 60, 90, 120, 180)
TIMECOURSE_TOLERANCE_MINUTES = 10
FALL_MG_DL = 10
RISE_MG_DL = 10
RISING_LOOKBACK_MINUTES = 15
RISING_MG_DL = 5
PEAK_SEARCH_MINUTES = 120
MIN_TIMECOURSE_EPISODES = 5


def _pct(part: int, whole: int) -> int | None:
    return round(100 * part / whole) if whole else None


def timecourse(
    episodes: list[dict],
    readings: list[dict],
    *,
    method: str = "temp_basal",
    min_start_glucose: float = HIGH_START_MG_DL,
) -> dict[str, Any]:
    """How glucose moved, minute by minute, after a correction started.

    For clean episodes of one method that started at or above
    ``min_start_glucose``: the median change at fixed offsets, how long until
    glucose first sat FALL_MG_DL below where it started, and how often it rose
    further first. The fall time answers "how long before it starts working"
    from her own corrections rather than from a rule of thumb."""
    points = sorted(
        (ts, float(r["value"]))
        for r in readings
        if r.get("value") is not None and (ts := _ts(r.get("timestamp"))) is not None
    )
    times = [p[0] for p in points]
    values = [p[1] for p in points]
    tolerance = TIMECOURSE_TOLERANCE_MINUTES * 60

    def near(when: datetime) -> float | None:
        k = bisect.bisect_left(times, when)
        best = None
        for j in (k - 1, k):
            if 0 <= j < len(times):
                gap = abs((times[j] - when).total_seconds())
                if gap <= tolerance and (best is None or gap < best[0]):
                    best = (gap, values[j])
        return best[1] if best else None

    rows = []
    for e in episodes:
        if e["method"] != method or e["confounded"] or e["start_glucose"] < min_start_glucose:
            continue
        start = _ts(e["start"])
        if start is None:
            continue
        i0 = bisect.bisect_left(times, start)
        i1 = bisect.bisect_left(times, start + timedelta(minutes=RESPONSE_WINDOW_MINUTES))
        window = list(zip(times[i0:i1], values[i0:i1]))
        if len(window) < MIN_CGM_POINTS:
            continue
        start_bg = e["start_glucose"]
        before = near(start - timedelta(minutes=RISING_LOOKBACK_MINUTES))
        deltas = {}
        for minutes in TIMECOURSE_OFFSETS_MINUTES:
            value = near(start + timedelta(minutes=minutes))
            deltas[f"delta_{minutes}"] = round(value - start_bg) if value is not None else None
        fall = next(
            (round((t - start).total_seconds() / 60) for t, v in window if v <= start_bg - FALL_MG_DL),
            None,
        )
        early = [(t, v) for t, v in window if t < start + timedelta(minutes=PEAK_SEARCH_MINUTES)] or window[:1]
        peak_time, peak = max(early, key=lambda point: (point[1], -point[0].timestamp()))
        nadir_time, _nadir = min(window, key=lambda point: (point[1], point[0].timestamp()))
        rows.append({
            **deltas,
            "minutes_to_fall": fall,
            "rising_at_start": before is not None and start_bg - before >= RISING_MG_DL,
            "rose_first": peak >= start_bg + RISE_MG_DL,
            "peak_rise": round(peak - start_bg),
            "minutes_to_peak": round((peak_time - start).total_seconds() / 60),
            "minutes_to_nadir": round((nadir_time - start).total_seconds() / 60),
            "minutes": e.get("minutes"),
            "start_glucose": start_bg,
        })

    def med(values: list[float]) -> float | None:
        return round(median(values)) if values else None

    fell = [r["minutes_to_fall"] for r in rows if r["minutes_to_fall"] is not None]
    rose = [r for r in rows if r["rose_first"]]
    rising = [r for r in rows if r["rising_at_start"]]
    rising_fell = [r["minutes_to_fall"] for r in rising if r["minutes_to_fall"] is not None]
    out: dict[str, Any] = {
        "algorithm_version": ALGORITHM_VERSION,
        "method": method,
        "min_start_glucose": min_start_glucose,
        "n": len(rows),
        "enough": len(rows) >= MIN_TIMECOURSE_EPISODES,
        "median_start_glucose": med([r["start_glucose"] for r in rows]),
        "median_minutes": med([r["minutes"] for r in rows if r.get("minutes") is not None]),
    }
    for minutes in TIMECOURSE_OFFSETS_MINUTES:
        out[f"delta_{minutes}"] = med([r[f"delta_{minutes}"] for r in rows if r[f"delta_{minutes}"] is not None])
    out.update({
        "median_minutes_to_fall": med(fell),
        "fell_within_60_pct": _pct(sum(1 for m in fell if m <= 60), len(rows)),
        "fell_within_120_pct": _pct(sum(1 for m in fell if m <= 120), len(rows)),
        "did_not_fall_pct": _pct(len(rows) - len(fell), len(rows)),
        "rose_first_pct": _pct(len(rose), len(rows)),
        "median_peak_rise": med([r["peak_rise"] for r in rose]),
        "median_minutes_to_peak": med([r["minutes_to_peak"] for r in rose]),
        "median_minutes_to_nadir": med([r["minutes_to_nadir"] for r in rows]),
        # Started while glucose was still climbing: how much further, and how
        # long until it came back down below the starting value.
        "rising_at_start": {
            "n": len(rising),
            "enough": len(rising) >= MIN_TIMECOURSE_EPISODES,
            "median_further_rise": med([max(0, r["peak_rise"]) for r in rising]),
            "median_minutes_to_peak": med([r["minutes_to_peak"] for r in rising]),
            "median_minutes_to_fall": med(rising_fell),
            "fell_within_120_pct": _pct(sum(1 for m in rising_fell if m <= 120), len(rising)),
        },
    })
    return out


def summarize(episodes: list[dict]) -> dict[str, Any]:
    clean = [e for e in episodes if not e["confounded"]]
    out: dict[str, Any] = {
        "algorithm_version": ALGORITHM_VERSION,
        "response_window_minutes": RESPONSE_WINDOW_MINUTES,
        "n_total": len(episodes),
        "n_clean": len(clean),
        "n_confounded": len(episodes) - len(clean),
    }
    for method in ("temp_basal", "bolus"):
        sub = [e for e in clean if e["method"] == method]
        out[f"n_{method}"] = len(sub)
        out[f"drop_per_unit_{method}"] = round(median(e["drop_per_unit"] for e in sub), 1) if sub else None
    high = [e for e in clean if e["start_glucose"] >= HIGH_START_MG_DL]
    low = [e for e in clean if e["start_glucose"] < HIGH_START_MG_DL]
    out["drop_per_unit_from_high"] = round(median(e["drop_per_unit"] for e in high), 1) if high else None
    out["drop_per_unit_from_low"] = round(median(e["drop_per_unit"] for e in low), 1) if low else None
    out["median_start_glucose"] = round(median(e["start_glucose"] for e in clean)) if clean else None
    return out
