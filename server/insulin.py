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
import sqlite3
from datetime import date, datetime, timedelta, timezone
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from . import profile
from .analytics_confidence import mean_confidence
from .config import APP_TIMEZONE, OWNER_EMAIL
from .db import config_value
from .data_quality import assess_cgm, assess_pump_tdd, build_envelope
from .insulin_reconciliation import reconcile_treatments
from .insulin_response import ASSUMPTIONS as RESPONSE_ASSUMPTIONS
from .insulin_response import IOB_ACTION_MINUTES
from .insulin_response import build_response_events
from .repositories import get_repositories

log = logging.getLogger("glucopilot.insulin")

ABS_WINDOW_DAYS = 120

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


def absorption() -> dict[str, Any]:
    """Build observational response events and analyze clean events by default."""
    now = datetime.now(timezone.utc)
    since_at = now - timedelta(days=ABS_WINDOW_DAYS)
    since = since_at.isoformat(timespec="seconds").replace("+00:00", "Z")
    source_since = (
        since_at - timedelta(minutes=IOB_ACTION_MINUTES)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")

    repositories = get_repositories()
    glucose_rows = repositories.glucose.query(
        {"owner_email": OWNER_EMAIL, "timestamp": {"$gte": source_since}},
        "timestamp",
        300000,
    )
    tz = _app_timezone()
    cgm_quality = assess_cgm(
        glucose_rows, tz, start=since_at, end=now, as_of=now.astimezone(tz).date()
    )

    treatments = repositories.treatments.query(
        {"owner_email": OWNER_EMAIL, "timestamp": {"$gte": source_since}},
        "timestamp",
        100000,
    )

    def entity_rows(entity_type: str, field: str, lower: str) -> list[dict[str, Any]]:
        return repositories.entity(entity_type).query(
            {"owner_email": OWNER_EMAIL, field: {"$gte": lower}},
            field,
            100000,
        )

    response = build_response_events(
        treatments,
        glucose_rows,
        period_logs=entity_rows("PeriodLog", "date", since[:10]),
        wearable_days=[
            *entity_rows("OuraDaily", "date", since[:10]),
            *entity_rows("FitbitDaily", "date", since[:10]),
        ],
        fingersticks=entity_rows("FingerstickReading", "timestamp", source_since),
        timezone_name=str(tz),
        event_start=since_at,
        event_end=now,
    )
    clean = [event for event in response["events"] if event["included_by_default"]]
    samples = [
        (
            event["observed"]["bolus"].get("timestamp"),
            event["calculations"]["nadir_drop_per_unit_mg_dl"],
        )
        for event in clean
        if event["calculations"]["nadir_drop_per_unit_mg_dl"] is not None
    ]
    sample_dates = {
        event["context"]["local_date"]
        for event in clean
        if event["calculations"]["nadir_drop_per_unit_mg_dl"] is not None
    }
    data_through = (
        date.fromisoformat(max(sample_dates)) if sample_dates else None
    )
    blockers = (
        ()
        if cgm_quality["ai_eligible"]
        else ("CGM input did not meet the reliability threshold",)
    )
    quality = build_envelope(
        "insulin_response",
        observed=len(samples),
        expected=8,
        unit="clean_correction_boluses",
        data_through=data_through,
        as_of=now.astimezone(tz).date(),
        blocking_reasons=blockers,
        limitations=(
            "Confounded and under-covered events are retained but excluded from default analysis.",
            RESPONSE_ASSUMPTIONS["iob"]["limitations"],
        ),
        input_values=(
            f"{timestamp}:{value:g}" for timestamp, value in samples
        ),
    )
    values = [float(value) for _, value in samples]
    confidence = mean_confidence(
        values,
        valid_days=len(sample_dates),
        expected_days=ABS_WINDOW_DAYS,
        unit="mg/dL/U",
    )
    available = len(values) >= 8
    cv = response["analysis"]["cv_pct"]
    consistency = (
        "highly variable"
        if (cv or 0) >= 50
        else "variable"
        if (cv or 0) >= 30
        else "consistent"
    )
    estimate_result = estimate() if available else {}
    return {
        **response,
        "available": available,
        "reason": (
            None
            if available
            else f"Only {len(values)} clean correction boluses in {ABS_WINDOW_DAYS}d — need 8."
        ),
        "n": len(values),
        "mean_drop_per_unit": response["analysis"]["mean_nadir_drop_per_unit_mg_dl"],
        "median_drop_per_unit": response["analysis"]["median_nadir_drop_per_unit_mg_dl"],
        "cv_pct": cv,
        "min_drop_per_unit": round(min(values)) if values else None,
        "max_drop_per_unit": round(max(values)) if values else None,
        "consistency": consistency if values else None,
        "expected_isf": (
            estimate_result.get("est_isf_mgdl_per_u")
            if estimate_result.get("available")
            else None
        ),
        "window_days": ABS_WINDOW_DAYS,
        "response_window_minutes": RESPONSE_ASSUMPTIONS["response_window_minutes"],
        "confidence": confidence,
        "quality": quality,
        "data_quality": {"cgm": cgm_quality, "insulin_response": quality},
    }


async def handle(body: dict[str, Any]) -> dict[str, Any]:
    action = body.get("action", "resistance")
    if action == "resistance":
        return estimate()
    if action == "absorption":
        result = absorption()
        if body.get("include_events") is False:
            result.pop("events", None)
        return result
    return {"error": "Unknown action", "_status": 400}
