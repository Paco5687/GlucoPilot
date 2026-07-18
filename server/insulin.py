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

import logging
import re
from statistics import mean
from typing import Any

from . import db, profile
from .config import OWNER_EMAIL

log = logging.getLogger("glucopilot.insulin")

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


async def handle(body: dict[str, Any]) -> dict[str, Any]:
    if body.get("action", "resistance") == "resistance":
        return estimate()
    return {"error": "Unknown action", "_status": 400}
