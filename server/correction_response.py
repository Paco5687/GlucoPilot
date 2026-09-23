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
        rows.append((start, start + timedelta(minutes=float(minutes)), extra))
    rows.sort()
    episodes: list[dict] = []
    for start, end, extra in rows:
        if episodes and (start - episodes[-1]["end"]).total_seconds() < EPISODE_GAP_MINUTES * 60:
            episodes[-1]["end"] = max(episodes[-1]["end"], end)
            episodes[-1]["units"] += extra
        else:
            episodes.append({"method": "temp_basal", "start": start, "end": end, "units": extra})
    return episodes


def _bolus_episodes(treatments: list[dict]) -> list[dict]:
    boluses, carbs = [], []
    for t in treatments:
        start = _ts(t.get("timestamp"))
        if start is None:
            continue
        if t.get("event_type") == "Bolus" and t.get("amount"):
            boluses.append((start, float(t["amount"])))
        elif t.get("type") == "carb" and t.get("amount"):
            carbs.append(start)
    boluses.sort()
    carbs.sort()
    episodes = []
    for i, (start, amount) in enumerate(boluses):
        j = bisect.bisect_left(carbs, start - timedelta(minutes=CARB_EXCLUSION_MINUTES))
        if j < len(carbs) and carbs[j] <= start + timedelta(minutes=CARB_EXCLUSION_MINUTES):
            continue  # a meal bolus, not a correction
        if i > 0 and (start - boluses[i - 1][0]).total_seconds() < PRIOR_BOLUS_MINUTES * 60:
            continue  # stacked on an earlier bolus; its response is not separable
        episodes.append({"method": "bolus", "start": start, "end": start, "units": amount})
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
            "units": round(e["units"], 2),
            "start_glucose": start_bg,
            "nadir_glucose": nadir,
            "drop_per_unit": round((start_bg - nadir) / e["units"], 1),
            "confounded": confounded,
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
