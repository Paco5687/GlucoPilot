"""Insulin resistance / sensitivity estimate (#39).

The true total daily dose (basal + bolus) isn't a summable stream here — basal
comes from the pump and is only captured in the "Daily Total" treatment rows,
whose notes read like "Bolus: 4.25U | Basal: 24.3U | Total: 28.55U". We parse
those for the authoritative TDD, then derive body-relative resistance proxies:

  - TDD/kg — the standard resistance proxy (needs weight from the body profile)
  - estimated ISF via the 1800 rule, carb ratio via the 500 rule
  - basal/bolus split, per-cycle-phase TDD/kg, and a recent-vs-prior trend

Everything is an ESTIMATE/indicator, and it's only as current as the last day
with complete pump data (Glooko-only stretches have no basal) — so we surface
`data_through` prominently.
"""

import bisect
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from statistics import mean, median, pstdev
from typing import Any
from zoneinfo import ZoneInfo

from . import profile
from .config import APP_TIMEZONE, OWNER_EMAIL
from .db import config_value
from .data_quality import assess_cgm, assess_pump_tdd, build_envelope, cgm_points
from .insulin_reconciliation import reconcile_treatments
from .repositories import get_repositories

log = logging.getLogger("glucopilot.insulin")

# Absorption analysis tunables
ABS_WINDOW_DAYS = 120
RESPONSE_MIN = 120        # look for the post-dose low within this many minutes
CARB_GUARD_MIN = 20       # exclude boluses with carbs within ±this (meal-related)
STACK_GUARD_PRE_MIN = 30  # exclude if another dose sits in [-this, +RESPONSE_MIN]
MIN_UNITS = 0.5
MIN_START_GLUCOSE = 100   # need room to fall to see a correction's effect

WINDOW_DAYS = 90
CURRENT_DATA_DAYS = 14


def _all_treatments() -> list[dict[str, Any]]:
    repository = get_repositories().treatments
    rows, skip = [], 0
    while True:
        page = repository.query({"owner_email": OWNER_EMAIL}, "timestamp", 5000, skip)
        rows.extend(page)
        if len(page) < 5000:
            return rows
        skip += 5000


def _app_timezone() -> ZoneInfo:
    try:
        timezone_name = config_value("app_timezone", APP_TIMEZONE)
    except sqlite3.OperationalError:
        # Repository-unit tests deliberately run without initializing SQLite.
        timezone_name = APP_TIMEZONE
    return ZoneInfo(timezone_name)


def _reconciliation() -> dict[str, Any]:
    return reconcile_treatments(_all_treatments(), _app_timezone())


def _daily_tdd() -> dict[str, dict[str, float]]:
    """Compatibility view of non-conflicting, complete pump-reported totals."""
    output = {}
    for day in _reconciliation()["days"]:
        selected = day["pump_reported"]["selected"]
        if selected is not None:
            output[day["date"]] = {
                "total": selected["total_units"],
                "basal": selected["basal_units"],
                "bolus": selected["bolus_units"],
            }
    return output


def _category(tdd_per_kg: float | None) -> str:
    if tdd_per_kg is None:
        return "unknown"
    if tdd_per_kg < 0.4:
        return "low"          # more insulin-sensitive
    if tdd_per_kg < 0.6:
        return "typical"
    if tdd_per_kg < 0.8:
        return "elevated"
    return "high"             # more insulin-resistant


def estimate() -> dict[str, Any]:
    repositories = get_repositories()
    reconciliation = _reconciliation()
    today = datetime.now(_app_timezone()).date()
    quality = assess_pump_tdd(
        reconciliation, start_date=today - timedelta(days=WINDOW_DAYS - 1), end_date=today, as_of=today
    )
    by_day = {}
    source_by_day = {}
    for day in reconciliation["days"]:
        reported = day["pump_reported"]["selected"]
        calculated = day["calculated"]
        if reported is not None:
            by_day[day["date"]] = {
                "total": reported["total_units"],
                "basal": reported["basal_units"],
                "bolus": reported["bolus_units"],
            }
            source_by_day[day["date"]] = f"pump_reported:{reported['source']}"
        elif calculated["total_units"] is not None:
            by_day[day["date"]] = {
                "total": calculated["total_units"],
                "basal": calculated["delivered_basal_units"],
                "bolus": calculated["bolus_units"],
            }
            source_by_day[day["date"]] = "calculated:tandem_delivered"
    if not by_day:
        return {
            "available": False,
            "current": False,
            "reason": "No complete daily insulin totals found — complete pump totals or delivered basal coverage are needed.",
            "latest_insulin_activity": reconciliation["summary"]["latest_activity_date"],
            "reconciliation": reconciliation["summary"],
            "algorithm_version": reconciliation["algorithm_version"],
            "input_data_version": reconciliation["input_data_version"],
            "quality": quality,
        }

    prof = profile.get_profile()
    weight = prof.get("weight_kg")
    days_sorted = sorted(by_day)
    window = days_sorted[-WINDOW_DAYS:]
    totals = [by_day[d]["total"] for d in window]
    basals = [by_day[d]["basal"] for d in window if by_day[d]["basal"]]
    boluses = [by_day[d]["bolus"] for d in window if by_day[d]["bolus"]]

    avg_tdd = round(mean(totals), 1)
    tdd_per_kg = round(avg_tdd / weight, 3) if weight else None
    basal_pct = round(sum(basals) / (sum(basals) + sum(boluses)) * 100) if (basals and boluses) else None

    # Per cycle-phase TDD/kg (luteal resistance shows up here)
    phase_days: dict[str, list[str]] = {}
    for p in repositories.entity("PeriodLog").query(
        {"owner_email": OWNER_EMAIL}, "date", 5000
    ):
        if p.get("date") and p.get("phase"):
            phase_days.setdefault(p["phase"], []).append(p["date"])
    per_phase = {}
    if weight:
        for ph, ds in phase_days.items():
            vals = [by_day[d]["total"] for d in ds if d in by_day]
            if len(vals) >= 5:
                per_phase[ph] = round(mean(vals) / weight, 3)

    # Trend: recent vs prior half of the available window
    trend = None
    if len(window) >= 20:
        half = len(window) // 2
        prior = mean(by_day[d]["total"] for d in window[:half])
        recent = mean(by_day[d]["total"] for d in window[half:])
        if prior:
            trend = {"recent_tdd": round(recent, 1), "prior_tdd": round(prior, 1),
                     "pct_change": round((recent - prior) / prior * 100)}

    data_through = days_sorted[-1]
    latest_activity = reconciliation["summary"]["latest_activity_date"]
    age_days = (datetime.now(_app_timezone()).date() - date.fromisoformat(data_through)).days
    source_counts: dict[str, int] = {}
    for day in window:
        source_counts[source_by_day[day]] = source_counts.get(source_by_day[day], 0) + 1
    reconciled_by_date = {day["date"]: day for day in reconciliation["days"]}
    window_reconciled = [reconciled_by_date[day] for day in window]
    window_reported = [day for day in window_reconciled if day["pump_reported"]["selected"]]
    window_calculated = [day for day in window_reconciled if day["calculated"]["total_units"] is not None]
    window_reported_sources: dict[str, int] = {}
    for day in window_reported:
        source = day["pump_reported"]["selected"]["source"]
        window_reported_sources[source] = window_reported_sources.get(source, 0) + 1
    estimate_reconciliation = {
        **reconciliation["summary"],
        "pump_reported_avg_tdd": round(
            mean(day["pump_reported"]["selected"]["total_units"] for day in window_reported), 1
        ) if window_reported else None,
        "pump_reported_days": len(window_reported),
        "pump_reported_sources": dict(sorted(window_reported_sources.items())),
        "calculated_avg_tdd": round(
            mean(day["calculated"]["total_units"] for day in window_calculated), 1
        ) if window_calculated else None,
        "calculated_days": len(window_calculated),
        "calculated_source": "tandem_delivered" if window_calculated else None,
        "analysis_window_days": len(window),
        "all_history_pump_reported_days": reconciliation["summary"]["pump_reported_days"],
        "all_history_calculated_days": reconciliation["summary"]["calculated_days"],
    }

    return {
        "available": True,
        "avg_tdd": avg_tdd,
        "tdd_per_kg": tdd_per_kg,
        "category": _category(tdd_per_kg),
        "basal_pct": basal_pct,
        "weight_kg": weight,
        "est_isf_mgdl_per_u": round(1800 / avg_tdd) if avg_tdd else None,   # 1800 rule
        "est_carb_ratio_g_per_u": round(500 / avg_tdd, 1) if avg_tdd else None,  # 500 rule
        "per_phase_tdd_per_kg": per_phase,
        "trend": trend,
        "n_days": len(window),
        "data_through": data_through,
        "latest_insulin_activity": latest_activity,
        "current": age_days <= CURRENT_DATA_DAYS,
        "data_age_days": age_days,
        "total_sources": source_counts,
        "reconciliation": estimate_reconciliation,
        "algorithm_version": reconciliation["algorithm_version"],
        "input_data_version": reconciliation["input_data_version"],
        "quality": quality,
        "needs_weight": weight is None,
    }


def _epoch(ts: str) -> float | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def absorption() -> dict[str, Any]:
    """How consistently insulin lowers glucose. Isolates CLEAN correction boluses
    (no carbs nearby, no dose stacking, glucose elevated so there's room to fall),
    measures the drop over the next RESPONSE_MIN minutes, and reports drop-per-unit
    and — the key signal — its variability (CV). High CV = the same dose can do
    very different things ('sometimes a lot does little, a little does a lot')."""
    now = datetime.now(timezone.utc)
    since_at = now - timedelta(days=ABS_WINDOW_DAYS)
    since = since_at.isoformat(timespec="seconds").replace("+00:00", "Z")

    repositories = get_repositories()
    glucose_rows = repositories.glucose.query(
        {"owner_email": OWNER_EMAIL, "timestamp": {"$gte": since}},
        "timestamp",
        300000,
    )
    tz = _app_timezone()
    cgm_quality = assess_cgm(
        glucose_rows, tz, start=since_at, end=now, as_of=now.astimezone(tz).date()
    )
    pts = [(instant.timestamp(), value) for instant, value in cgm_points(
        glucose_rows, tz, start=since_at, end=now
    )]
    times = [p[0] for p in pts]
    vals = [p[1] for p in pts]

    def response_quality(samples: list[tuple[float, float]]) -> dict[str, Any]:
        latest = max((sample[0] for sample in samples), default=None)
        blockers = () if cgm_quality["ai_eligible"] else ("CGM input did not meet the reliability threshold",)
        return build_envelope(
            "insulin_response", observed=len(samples), expected=8, unit="clean_correction_boluses",
            data_through=datetime.fromtimestamp(latest, tz).date() if latest is not None else None,
            as_of=now.astimezone(tz).date(), blocking_reasons=blockers,
            limitations=("Response estimates exclude meal-related and stacked doses.",),
            input_values=(f"{timestamp}:{value:g}" for timestamp, value in samples),
        )

    if len(times) < 100:
        quality = response_quality([])
        return {
            "available": False, "reason": "Not enough CGM data in range.", "quality": quality,
            "data_quality": {"cgm": cgm_quality, "insulin_response": quality},
        }

    boluses, carbs, ins_times = [], [], []
    for t in repositories.treatments.query(
        {"owner_email": OWNER_EMAIL, "timestamp": {"$gte": since}},
        "timestamp",
        100000,
    ):
        e = _epoch(t.get("timestamp"))
        if e is None:
            continue
        if t.get("type") == "insulin" and t.get("event_type") != "Daily Total" and t.get("amount"):
            boluses.append((e, float(t["amount"])))
            ins_times.append(e)
        elif t.get("type") == "carb" and t.get("amount"):
            carbs.append(e)
    boluses.sort()
    carbs.sort()
    ins_times.sort()

    def any_in(sorted_list, lo, hi):
        return bisect.bisect_right(sorted_list, hi) > bisect.bisect_left(sorted_list, lo)

    def cgm_at(e, tol=600):
        i = bisect.bisect_left(times, e)
        best, bd = None, None
        for j in (i - 1, i):
            if 0 <= j < len(times):
                d = abs(times[j] - e)
                if d <= tol and (bd is None or d < bd):
                    bd, best = d, vals[j]
        return best

    dpus: list[tuple[float, float]] = []
    for e, units in boluses:
        if units < MIN_UNITS:
            continue
        if any_in(carbs, e - CARB_GUARD_MIN * 60, e + CARB_GUARD_MIN * 60):
            continue  # meal-related
        lo = bisect.bisect_left(ins_times, e - STACK_GUARD_PRE_MIN * 60)
        hi = bisect.bisect_right(ins_times, e + RESPONSE_MIN * 60)
        if hi - lo > 1:
            continue  # dose stacking in the response window
        g0 = cgm_at(e)
        if g0 is None or g0 < MIN_START_GLUCOSE:
            continue
        a = bisect.bisect_left(times, e)
        b = bisect.bisect_right(times, e + RESPONSE_MIN * 60)
        seg = vals[a:b]
        if not seg:
            continue
        dpus.append((e, (g0 - min(seg)) / units))

    quality = response_quality(dpus)
    if len(dpus) < 8:
        return {
            "available": False,
            "reason": f"Only {len(dpus)} clean correction boluses in {ABS_WINDOW_DAYS}d — need more.",
            "quality": quality,
            "data_quality": {"cgm": cgm_quality, "insulin_response": quality},
        }

    values = [value for _, value in dpus]
    m = mean(values)
    cv = round(pstdev(values) / m * 100) if m else None
    consistency = "highly variable" if (cv or 0) >= 50 else "variable" if (cv or 0) >= 30 else "consistent"
    est = estimate()
    return {
        "available": True,
        "n": len(values),
        "mean_drop_per_unit": round(m, 1),
        "median_drop_per_unit": round(median(values), 1),
        "cv_pct": cv,
        "min_drop_per_unit": round(min(values)),
        "max_drop_per_unit": round(max(values)),
        "consistency": consistency,
        "expected_isf": est.get("est_isf_mgdl_per_u") if est.get("available") else None,
        "window_days": ABS_WINDOW_DAYS,
        "quality": quality,
        "data_quality": {"cgm": cgm_quality, "insulin_response": quality},
    }


async def handle(body: dict[str, Any]) -> dict[str, Any]:
    action = body.get("action", "resistance")
    if action == "resistance":
        return estimate()
    if action == "absorption":
        return absorption()
    return {"error": "Unknown action", "_status": 400}
