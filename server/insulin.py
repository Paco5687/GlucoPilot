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
import re
from datetime import datetime, timedelta, timezone
from statistics import mean, median, pstdev
from typing import Any

from . import db, profile
from .config import OWNER_EMAIL

log = logging.getLogger("glucopilot.insulin")

# Absorption analysis tunables
ABS_WINDOW_DAYS = 120
RESPONSE_MIN = 120        # look for the post-dose low within this many minutes
CARB_GUARD_MIN = 20       # exclude boluses with carbs within ±this (meal-related)
STACK_GUARD_PRE_MIN = 30  # exclude if another dose sits in [-this, +RESPONSE_MIN]
MIN_UNITS = 0.5
MIN_START_GLUCOSE = 100   # need room to fall to see a correction's effect

_TOTAL_RE = re.compile(r"total:\s*([\d.]+)", re.I)
_BASAL_RE = re.compile(r"basal:\s*([\d.]+)", re.I)
_BOLUS_RE = re.compile(r"bolus:\s*([\d.]+)", re.I)

WINDOW_DAYS = 90


def _f(rx: re.Pattern, s: str) -> float | None:
    m = rx.search(s or "")
    return float(m.group(1)) if m else None


def _daily_tdd() -> dict[str, dict[str, float]]:
    """day -> {total, basal, bolus} from parsed Daily Total notes. A day can have
    several such rows (e.g. cartridge changes) — sum them."""
    rows = db.query_entities(
        "Treatment", {"owner_email": OWNER_EMAIL, "type": "insulin", "event_type": "Daily Total"}, "timestamp", 100000
    )
    by_day: dict[str, dict[str, float]] = {}
    for t in rows:
        total = _f(_TOTAL_RE, t.get("notes"))
        if total is None:
            continue
        day = (t.get("timestamp") or "")[:10]
        if not day:
            continue
        e = by_day.setdefault(day, {"total": 0.0, "basal": 0.0, "bolus": 0.0})
        e["total"] += total
        e["basal"] += _f(_BASAL_RE, t.get("notes")) or 0.0
        e["bolus"] += _f(_BOLUS_RE, t.get("notes")) or 0.0
    return by_day


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
    by_day = _daily_tdd()
    if not by_day:
        return {"available": False, "reason": "No daily insulin totals (basal+bolus) found — pump data needed."}

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
    for p in db.query_entities("PeriodLog", {"owner_email": OWNER_EMAIL}, "date", 5000):
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
        "data_through": days_sorted[-1],
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
    since = (datetime.now(timezone.utc) - timedelta(days=ABS_WINDOW_DAYS)).isoformat(timespec="seconds").replace("+00:00", "Z")

    pts = []
    for r in db.query_entities("GlucoseReading", {"owner_email": OWNER_EMAIL, "timestamp": {"$gte": since}}, "timestamp", 300000):
        e = _epoch(r.get("timestamp"))
        if e is not None and r.get("value") is not None:
            pts.append((e, float(r["value"])))
    pts.sort()
    times = [p[0] for p in pts]
    vals = [p[1] for p in pts]
    if len(times) < 100:
        return {"available": False, "reason": "Not enough CGM data in range."}

    boluses, carbs, ins_times = [], [], []
    for t in db.query_entities("Treatment", {"owner_email": OWNER_EMAIL, "timestamp": {"$gte": since}}, "timestamp", 100000):
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

    dpus = []
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
        dpus.append((g0 - min(seg)) / units)

    if len(dpus) < 8:
        return {"available": False, "reason": f"Only {len(dpus)} clean correction boluses in {ABS_WINDOW_DAYS}d — need more."}

    m = mean(dpus)
    cv = round(pstdev(dpus) / m * 100) if m else None
    consistency = "highly variable" if (cv or 0) >= 50 else "variable" if (cv or 0) >= 30 else "consistent"
    est = estimate()
    return {
        "available": True,
        "n": len(dpus),
        "mean_drop_per_unit": round(m, 1),
        "median_drop_per_unit": round(median(dpus), 1),
        "cv_pct": cv,
        "min_drop_per_unit": round(min(dpus)),
        "max_drop_per_unit": round(max(dpus)),
        "consistency": consistency,
        "expected_isf": est.get("est_isf_mgdl_per_u") if est.get("available") else None,
        "window_days": ABS_WINDOW_DAYS,
    }


async def handle(body: dict[str, Any]) -> dict[str, Any]:
    action = body.get("action", "resistance")
    if action == "resistance":
        return estimate()
    if action == "absorption":
        return absorption()
    return {"error": "Unknown action", "_status": 400}
