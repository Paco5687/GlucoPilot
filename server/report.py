"""90-day Visit Report — a clinical-discussion summary Emily prints for her doctor.

Computes standardized glucose metrics (TIR/GMI/CV + AGP-style hourly
percentiles), insulin totals, per-cycle-phase breakdowns, sleep/recovery
averages, and lab values with trends, then asks the LLM for an
observational "quarter in review" narrative (explicitly non-diagnostic —
a data summary to support the clinical conversation, not replace it).

Read-only: available to both admin and provider sessions.
"""

import json
import logging
import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .analytics_confidence import phase_provenance
from .auth import require_login
from .canonical_time import temporal_metadata
from .clinician_briefs import low_episodes
from .clinical_evidence import build_context as build_clinical_evidence
from .clinical_evidence import link_generated_narrative, resolve_prompt_aliases
from .clinical_evidence import prompt_view as prompt_evidence_view
from .config import APP_TIMEZONE, DEMO_MODE, OWNER_EMAIL
from .db import config_value, get_setting
from .data_quality import assess_cgm, assess_daily, assess_nutrition, assess_pump_tdd, cgm_points
from .glucose_reconciliation import summarize as summarize_fingersticks
from .insulin_reconciliation import reconcile_treatments
from .lab_audit import ABNORMAL_FLAGS, effective_flag
from .lab_names import canonical_lab_name
from .lab_audit import qualification as lab_qualification
from .lab_audit import summary_eligible as lab_summary_eligible
from .llm import CONTEXT_SAFETY_MARGIN, context_limit, count_tokens, invoke_llm
from .repositories import get_repositories

log = logging.getLogger("glucopilot.report")

# International consensus CGM targets (Battelino et al., Diabetes Care 2019).
TBR70_TARGET_PCT = 4.0
TBR54_TARGET_PCT = 1.0

DEMO_NARRATIVE = {
    "headline": "A solid quarter — 91% time in range with steady, well-controlled glucose across the cycle.",
    "glucose_summary": "Average glucose was 129 mg/dL with a GMI of 6.4% and a coefficient of variation around 32%, indicating stable day-to-day control. Time in range held at 91%, with most out-of-range time coming from a consistent early-morning dawn rise and occasional post-dinner spikes.",
    "cycle_summary": "Time in range was modestly lower in the luteal phase (about 78%) than the follicular phase (about 86%), consistent with cycle-related insulin resistance worth noting to the care team.",
    "lifestyle_summary": "Nights with higher sleep scores and higher-step days both tended to line up with better next-day time in range — associations worth keeping an eye on, not causes.",
    "discussion_points": [
        "Review overnight basal timing given the recurring dawn rise (~40 mg/dL, 3–7am).",
        "Consider pre-bolus timing for dinner, where post-meal spikes appear most often.",
        "Discuss whether luteal-phase settings adjustments could lift time in range during that week.",
        "A1c trend is improving (7.4 → 6.8% over the quarter); confirm against the next lab draw.",
    ],
}

router = APIRouter(dependencies=[Depends(require_login)])


def _parse_ts(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def _paged(etype: str, since_iso: str) -> list[dict]:
    repository = get_repositories().entity(etype)
    out, skip = [], 0
    while True:
        page = repository.query(
            {"owner_email": OWNER_EMAIL, "timestamp": {"$gte": since_iso}},
            "timestamp",
            5000,
            skip,
        )
        out.extend(page)
        if len(page) < 5000:
            break
        skip += 5000
    return out


def _patient() -> dict[str, Any]:
    """Who the report is about: name and date of birth for the header.

    The name comes from the health profile; an owner display name saved under
    account settings is the fallback. The login username is never used — it
    names an account, not a patient.
    """
    from . import profile

    prof = profile.get_profile()
    name = prof.get("full_name") or ""
    if not name:
        try:
            name = str(json.loads(get_setting("user_profile") or "{}").get("full_name") or "")
        except (ValueError, AttributeError):
            name = ""
    return {
        "name": " ".join(name.split()) or None,
        "date_of_birth": prof.get("date_of_birth") or None,
        "age": prof.get("age"),
        "sex": prof.get("sex") or None,
    }


WEIGHT_PEAK_LOOKBACK_DAYS = 365


def _weight(start: date) -> dict[str, Any]:
    """Body-weight trend: the full logged series plus change from the recent
    peak, over the report window, and since the first entry.

    The headline is change from the 12-month high to the latest weight. The
    first entry can be years old (a pre-pregnancy baseline, say), so change
    since it can read as a gain while the patient is losing weight; it stays
    in the payload as secondary context only. Weight loss is a headline
    finding for a clinician, so it is reported whenever WeightLog has data,
    regardless of whether the change looks favourable."""
    from . import profile

    by_day: dict[date, float] = {}
    rows = get_repositories().entity("WeightLog").query({"owner_email": OWNER_EMAIL}, "date", 5000)
    for row in rows:
        try:
            day = date.fromisoformat(str(row.get("date"))[:10])
            weight = float(row.get("weight_kg"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(weight) and 20 <= weight <= 400:
            by_day[day] = weight  # one point per day; the later-written row wins
    units = profile.get_profile().get("units") or "imperial"
    if not by_day:
        return {"available": False, "units": units}

    series = [{"date": day.isoformat(), "weight_kg": round(kg, 1)} for day, kg in sorted(by_day.items())]
    first, latest = series[0], series[-1]
    # Window baseline: the last weight on or before the window start, else the
    # first weight inside the window.
    before = [point for point in series if point["date"] <= start.isoformat()]
    baseline = before[-1] if before else next(
        (point for point in series if point["date"] >= start.isoformat()), None
    )

    # Peak: the highest weight in the year up to the latest entry; the
    # earliest date wins a tie so the period covers the whole decline.
    lookback = (date.fromisoformat(latest["date"]) - timedelta(days=WEIGHT_PEAK_LOOKBACK_DAYS)).isoformat()
    recent = [point for point in series if point["date"] >= lookback]
    peak = max(recent, key=lambda point: (point["weight_kg"], -date.fromisoformat(point["date"]).toordinal()))

    def change(origin: dict[str, Any] | None) -> dict[str, Any] | None:
        if origin is None or origin["date"] == latest["date"]:
            return None
        delta = latest["weight_kg"] - origin["weight_kg"]
        return {
            "from_date": origin["date"],
            "from_kg": origin["weight_kg"],
            "change_kg": round(delta, 1),
            "change_pct": round(delta / origin["weight_kg"] * 100, 1),
            "days": (date.fromisoformat(latest["date"]) - date.fromisoformat(origin["date"])).days,
        }

    return {
        "available": True,
        "units": units,
        "latest": latest,
        "first": first,
        "entries": len(series),
        "peak": peak,
        "peak_lookback_days": WEIGHT_PEAK_LOOKBACK_DAYS,
        "peak_change": change(peak),
        "window_change": change(baseline),
        "since_first_change": change(first),
        "series": series[-200:],
    }


def _glucose(tz: ZoneInfo, since_iso: str) -> dict[str, Any]:
    readings = _paged("GlucoseReading", since_iso)
    now = datetime.now(timezone.utc)
    start = _parse_ts(since_iso) or now
    quality = assess_cgm(readings, tz, start=start, end=now, as_of=now.astimezone(tz).date())
    # Preserve the report's historical snapshot behavior for explicitly dated
    # fixtures/imports, while the quality envelope still rejects future data
    # from AI use relative to the actual report-generation time.
    timestamps = [_parse_ts(row.get("timestamp")) for row in readings]
    latest = max((timestamp for timestamp in timestamps if timestamp is not None), default=now)
    points = cgm_points(readings, tz, start=start, end=max(now, latest))
    values = [value for _, value in points]
    if not values:
        return {"available": False, "quality": quality}
    n = len(values)
    avg = sum(values) / n
    std = math.sqrt(sum((v - avg) ** 2 for v in values) / n)
    by_hour: dict[int, list[float]] = {}
    by_day: dict[str, list[float]] = {}
    for ts, value in points:
        local = ts.astimezone(tz)
        by_hour.setdefault(local.hour, []).append(value)
        by_day.setdefault(local.date().isoformat(), []).append(value)

    agp = []
    for h in range(24):
        vals = sorted(by_hour.get(h, []))
        if len(vals) < 5:
            agp.append({"hour": h})
            continue
        agp.append(
            {
                "hour": h,
                "p5": round(_percentile(vals, 0.05)),
                "p25": round(_percentile(vals, 0.25)),
                "p50": round(_percentile(vals, 0.50)),
                "p75": round(_percentile(vals, 0.75)),
                "p95": round(_percentile(vals, 0.95)),
            }
        )
    daily = []
    for day in sorted(by_day):
        dv = by_day[day]
        if len(dv) < 50:
            continue
        daily.append(
            {
                "date": day,
                "tir": round(sum(1 for v in dv if 70 <= v <= 180) / len(dv) * 100),
                "avg": round(sum(dv) / len(dv)),
            }
        )
    lows = low_episodes(points, tz)
    return {
        "available": True,
        "readings": n,
        "days": len(by_day),
        "avg": round(avg),
        "gmi": round(3.31 + 0.02392 * avg, 1),
        "cv": round(std / avg * 100, 1) if avg else 0,
        "std": round(std),
        "tir": round(sum(1 for v in values if 70 <= v <= 180) / n * 100, 1),
        "tbr70": round(sum(1 for v in values if v < 70) / n * 100, 1),
        "tbr54": round(sum(1 for v in values if v < 54) / n * 100, 1),
        "tar180": round(sum(1 for v in values if v > 180) / n * 100, 1),
        "tar250": round(sum(1 for v in values if v > 250) / n * 100, 1),
        "low_episodes": {**lows, "per_week": round(lows["episodes"] / max(1, len(by_day)) * 7, 1)},
        "agp": agp,
        "daily": daily,
        "quality": quality,
    }


BOLUS_STREAM_MIN_SHARE = 0.10
CARB_MIN_COVERAGE_PCT = 70.0


def _temp_basal_corrections(treatments: list[dict], days: int) -> dict[str, Any]:
    """Insulin delivered by manual-mode temp-basal corrections beyond the
    programmed rate: ``rate * (1 - 1/multiplier) * hours`` (as in
    correction_response). These corrections are not boluses, so the bolus
    stream never counts them."""
    seen: set[tuple[Any, ...]] = set()
    extra = 0.0
    count = 0
    for row in treatments:
        if row.get("type") != "tempbasal_correction":
            continue
        try:
            rate = float(row.get("absolute"))
            multiplier = float(row.get("multiplier"))
            minutes = float(row.get("duration"))
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in (rate, multiplier, minutes)) or multiplier <= 1 or minutes <= 0:
            continue
        key = (row.get("timestamp"), rate, multiplier, minutes)
        if key in seen:
            continue
        seen.add(key)
        extra += rate * (1 - 1 / multiplier) * minutes / 60
        count += 1
    return {
        "count": count,
        "extra_units_total": round(extra, 1),
        "extra_units_per_day": round(extra / max(days, 1), 1),
        "per_day": round(count / max(days, 1), 1),
        "source": "Glooko temporary basal (manual mode)",
    }


def _insulin(tz: ZoneInfo, since_iso: str, glucose_days: int) -> dict[str, Any]:
    treatments = _paged("Treatment", since_iso)
    days = max(glucose_days, 1)
    total_carbs = 0.0
    carb_records = 0
    for t in treatments:
        ttype = t.get("type")
        if ttype == "carb":
            carb_records += 1
            try:
                amount = float(t.get("amount"))
            except (TypeError, ValueError):
                amount = 0
            if math.isfinite(amount) and amount > 0:
                total_carbs += amount

    reconciliation = reconcile_treatments(treatments, tz)
    now_date = datetime.now(tz).date()
    start_date = (_parse_ts(since_iso) or datetime.now(timezone.utc)).astimezone(tz).date()
    pump_quality = assess_pump_tdd(
        reconciliation, start_date=start_date, end_date=now_date, as_of=now_date
    )
    nutrition_quality = assess_nutrition(
        treatments, tz, start_date=start_date, end_date=now_date, as_of=now_date
    )
    reconciled_days = reconciliation["days"]
    total_bolus = sum(float(day["calculated"]["bolus_units"]) for day in reconciled_days)
    bolus_count = sum(int(day["calculated"]["bolus_events"]) for day in reconciled_days)
    reported_days = [day for day in reconciled_days if day["pump_reported"]["selected"]]
    calculated_days = [day for day in reconciled_days if day["calculated"]["total_units"] is not None]
    scheduled_days = [day for day in reconciled_days if day["scheduled_basal"]["coverage_pct"] > 0]
    pump_reported_avg_basal = (
        round(
            sum(float(day["pump_reported"]["selected"]["basal_units"]) for day in reported_days)
            / len(reported_days),
            1,
        )
        if reported_days
        else None
    )
    calculated_avg_basal = (
        round(
            sum(float(day["calculated"]["delivered_basal_units"]) for day in calculated_days)
            / len(calculated_days),
            1,
        )
        if calculated_days
        else None
    )
    scheduled_avg_basal = (
        round(sum(float(day["scheduled_basal"]["units"]) for day in scheduled_days) / len(scheduled_days), 1)
        if scheduled_days
        else None
    )
    summary = reconciliation["summary"]
    preferred_tdd = summary["pump_reported_avg_tdd"]
    if preferred_tdd is None:
        preferred_tdd = summary["calculated_avg_tdd"]
    corrections = _temp_basal_corrections(treatments, days)
    avg_daily_bolus = round(total_bolus / days, 1)
    # Omnipod 5 automated delivery is not itemized in Glooko's event streams
    # and its daily totals carry bolus as 0, so a bolus figure far below the
    # TDD is a coverage gap, not a dosing record. Flag it rather than print a
    # number that reads as missed doses.
    bolus_stream_incomplete = bool(
        preferred_tdd and (bolus_count == 0 or avg_daily_bolus < BOLUS_STREAM_MIN_SHARE * float(preferred_tdd))
    )
    carb_coverage = nutrition_quality.get("coverage_pct")
    carbs_representative = bool(
        carb_records and carb_coverage is not None and carb_coverage >= CARB_MIN_COVERAGE_PCT
    )
    return {
        "available": bool(
            bolus_count or reported_days or calculated_days or scheduled_days or carb_records
            or corrections["count"]
        ),
        "avg_daily_bolus": avg_daily_bolus,
        "boluses_per_day": round(bolus_count / days, 1),
        "bolus_source": "Glooko bolus event stream",
        "bolus_stream_incomplete": bolus_stream_incomplete,
        "temp_basal_corrections": corrections,
        "avg_daily_carbs": round(total_carbs / days),
        "carb_logged_days": nutrition_quality.get("observed"),
        "carb_expected_days": nutrition_quality.get("expected"),
        "carbs_representative": carbs_representative,
        "pump_reported_avg_tdd": summary["pump_reported_avg_tdd"],
        "pump_reported_avg_basal": pump_reported_avg_basal,
        "pump_reported_days": summary["pump_reported_days"],
        "pump_reported_sources": summary["pump_reported_sources"],
        "calculated_avg_tdd": summary["calculated_avg_tdd"],
        "calculated_avg_daily_basal": calculated_avg_basal,
        "calculated_days": summary["calculated_days"],
        "calculated_source": summary["calculated_source"],
        "scheduled_avg_daily_basal": scheduled_avg_basal,
        "scheduled_days": len(scheduled_days),
        "avg_daily_basal_est": calculated_avg_basal,
        "avg_tdd_est": preferred_tdd,
        "has_basal": pump_reported_avg_basal is not None or calculated_avg_basal is not None,
        "latest_complete_date": summary["latest_complete_date"],
        "latest_activity_date": summary["latest_activity_date"],
        "incomplete_days": summary["incomplete_days"],
        "discrepancy_days": summary["discrepancy_days"],
        "discrepancies": summary["discrepancies"],
        "limitations": summary["limitations"],
        "algorithm_version": reconciliation["algorithm_version"],
        "input_data_version": reconciliation["input_data_version"],
        "quality": pump_quality,
        "nutrition_quality": nutrition_quality,
    }


def _cycle(tz: ZoneInfo, since_iso: str, glucose: dict) -> dict[str, Any]:
    since_date = since_iso[:10]
    logs = [
        l
        for l in get_repositories().entity("PeriodLog").query(
            {"owner_email": OWNER_EMAIL}, "date", 5000
        )
        if l.get("date") and l["date"] >= since_date and l.get("phase")
    ]
    now_date = datetime.now(tz).date()
    quality = assess_daily(
        "cycle", logs, tz, start_date=date.fromisoformat(since_date), end_date=now_date,
        as_of=now_date, required_fields=("phase",),
        limitations=("Cycle coverage reflects days with a recorded or inferred phase.",),
    )
    if not logs:
        return {"available": False, "quality": quality}
    phase_days: dict[str, set[str]] = {}
    for l in logs:
        phase_days.setdefault(l["phase"], set()).add(l["date"])
    provenance = phase_provenance(logs)

    daily_tir = {d["date"]: d["tir"] for d in glucose.get("daily", [])}
    daily_avg = {d["date"]: d["avg"] for d in glucose.get("daily", [])}

    per_phase = {}
    for phase, dates in phase_days.items():
        tirs = [daily_tir[d] for d in dates if d in daily_tir]
        avgs = [daily_avg[d] for d in dates if d in daily_avg]
        per_phase[phase] = {
            "days": len(dates),
            "confirmed_days": provenance["by_phase"].get(phase, {}).get("confirmed_days", 0),
            "inferred_days": provenance["by_phase"].get(phase, {}).get("inferred_days", 0),
            "tir": round(sum(tirs) / len(tirs)) if tirs else None,
            "avg_glucose": round(sum(avgs) / len(avgs)) if avgs else None,
        }

    # cycle starts from menstrual onsets
    by_date = {l["date"]: l["phase"] for l in logs}
    starts = []
    for d in sorted(by_date):
        if by_date[d] != "menstrual":
            continue
        prev = (datetime.fromisoformat(d) - timedelta(days=1)).date().isoformat()
        if by_date.get(prev) != "menstrual":
            starts.append(d)
    lengths = [
        (datetime.fromisoformat(b) - datetime.fromisoformat(a)).days
        for a, b in zip(starts, starts[1:])
        if 15 <= (datetime.fromisoformat(b) - datetime.fromisoformat(a)).days <= 60
    ]
    return {
        "available": True,
        "per_phase": per_phase,
        "cycles_detected": len(starts),
        "avg_cycle_length": round(sum(lengths) / len(lengths), 1) if lengths else None,
        "source": "inferred from Oura temperature" if any(l.get("source") == "oura_inferred" for l in logs) else "logged",
        "phase_provenance": provenance,
        "quality": quality,
    }


# Plausible physiological ranges for wearable daily values. Devices write 0
# (or nothing sensible) on days without a reading — an SpO2 of 0.0 is a missing
# night, not a desaturation — so values outside these ranges are excluded from
# averages rather than averaged in.
WELLNESS_PLAUSIBLE: dict[str, tuple[float, float]] = {
    "sleep_score": (1, 100),
    "readiness_score": (1, 100),
    "lowest_heart_rate": (25, 200),
    "resting_heart_rate": (25, 200),
    "spo2_average": (70, 100),
    "spo2_avg": (70, 100),
    "steps": (1, 100_000),
    "sleep_minutes": (1, 24 * 60),
}


def _avg(rows: list[dict], field: str) -> float | None:
    low, high = WELLNESS_PLAUSIBLE.get(field, (-math.inf, math.inf))
    vals = []
    for r in rows:
        try:
            value = float(r.get(field))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and low <= value <= high:
            vals.append(value)
    return sum(vals) / len(vals) if vals else None


def _mean(rows: list[dict], field: str, digits: int = 0, scale: float = 1.0) -> float | int | None:
    value = _avg(rows, field)
    if value is None:
        return None
    rounded = round(value / scale, digits)
    return int(rounded) if digits == 0 else rounded


def _wellness(days: int) -> dict[str, Any]:
    repositories = get_repositories()
    oura = repositories.oura_daily.query(
        {"owner_email": OWNER_EMAIL}, "-date", days
    )
    fitbit = repositories.fitbit_daily.query(
        {"owner_email": OWNER_EMAIL}, "-date", days
    )
    tz = ZoneInfo(config_value("app_timezone", APP_TIMEZONE))
    as_of = datetime.now(tz).date()
    start_date = as_of - timedelta(days=max(1, days) - 1)
    out: dict[str, Any] = {
        "oura": None,
        "fitbit": None,
        "quality": {
            "oura": assess_daily(
                "wearables", oura, tz, start_date=start_date, end_date=as_of, as_of=as_of,
                required_fields=("sleep_score", "readiness_score", "lowest_heart_rate"),
            ),
            "fitbit": assess_daily(
                "wearables", fitbit, tz, start_date=start_date, end_date=as_of, as_of=as_of,
                required_fields=("steps", "resting_heart_rate", "sleep_minutes"),
            ),
        },
    }
    if oura:
        temps = [float(r["readiness_temperature_deviation"]) for r in oura if r.get("readiness_temperature_deviation") is not None]
        out["oura"] = {
            "days": len(oura),
            "avg_sleep_score": _mean(oura, "sleep_score"),
            "avg_readiness_score": _mean(oura, "readiness_score"),
            "avg_resting_hr": _mean(oura, "lowest_heart_rate"),
            "avg_spo2": _mean(oura, "spo2_average", 1),
            "temp_range": f"{min(temps):+.1f} to {max(temps):+.1f} °C" if temps else None,
        }
    if fitbit:
        out["fitbit"] = {
            "days": len(fitbit),
            "avg_steps": _mean(fitbit, "steps"),
            "avg_resting_hr": _mean(fitbit, "resting_heart_rate"),
            "avg_sleep_hours": _mean(fitbit, "sleep_minutes", 1, scale=60),
            "avg_spo2": _mean(fitbit, "spo2_avg", 1),
        }
    return out


_MONTHS = {
    name: index
    for index, names in enumerate(
        (
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"), ("dec", "december"),
        ),
        1,
    )
    for name in names
}
_ISO_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")
_SLASH_DATE = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2}|\d{4})\b")
_YMD_SLASH_DATE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})\b")
_NAMED_DATE = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b")


def _lab_date(value: Any) -> date | None:
    """Parse a collected date written the ways lab reports write them.

    Returns None for text with no recoverable calendar date (e.g. "5/16 PM",
    which has no year), so such rows can never sort ahead of a real draw.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if match := _ISO_DATE.match(text):
            return date(int(match[1]), int(match[2]), int(match[3]))
        if match := _YMD_SLASH_DATE.match(text):
            return date(int(match[1]), int(match[2]), int(match[3]))
        if match := _SLASH_DATE.match(text):
            year = int(match[3])
            if year < 100:
                year += 2000 if year <= (date.today().year % 100) else 1900
            return date(year, int(match[1]), int(match[2]))
        if match := _NAMED_DATE.match(text):
            month = _MONTHS.get(match[1].lower())
            if month:
                return date(int(match[3]), month, int(match[2]))
    except ValueError:
        return None
    return None


# Core labs for the key-results panel, in display order.
KEY_LABS = (
    "Hemoglobin A1c", "TSH", "Free T4", "TPO antibodies", "Cortisol (AM)", "Prolactin",
    "FSH", "LH", "Estradiol", "Glucose", "C-peptide", "Creatinine", "eGFR",
    "Urine albumin/creatinine ratio", "LDL cholesterol", "Vitamin D, 25-OH", "Ferritin", "Hemoglobin",
    "Sodium", "Potassium",
)


def _lab_value_key(value: Any) -> str:
    try:
        return f"{float(value):.6g}"
    except (TypeError, ValueError):
        return str(value).strip().lower()


_VERIFIED_RANK = {"approved": 0, "edited": 0}


def _lab_flag(point: dict[str, Any]) -> str:
    """The flag the value and reference range support; the reported flag when
    there is no numeric range to check it against."""
    return effective_flag(point)[0]


def _dedupe_lab_points(points: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Collapse rows that report the same result more than once.

    Identical (date, value, unit) rows are one result — typically the same
    draw extracted from two documents. A row with no parseable date that
    repeats a dated row's value and unit is the same result with a damaged
    date. Verified copies win over unverified ones.
    """
    def rank(point: dict[str, Any]) -> tuple[int, str]:
        status = str(point.get("verification_status") or "unverified").lower()
        return (_VERIFIED_RANK.get(status, 1), str(point.get("id") or ""))

    kept: dict[tuple[str, str, str], dict[str, Any]] = {}
    for point in sorted(points, key=rank):
        parsed = point["_date"]
        key = (
            parsed.isoformat() if parsed else "raw:" + str(point.get("collected_date") or "").strip().lower(),
            _lab_value_key(point.get("value")),
            str(point.get("unit") or "").strip().lower(),
        )
        kept.setdefault(key, point)
    dated_values = {(value, unit) for (day, value, unit) in kept if not day.startswith("raw:")}
    result = [
        point
        for (day, value, unit), point in kept.items()
        if not (day.startswith("raw:") and (value, unit) in dated_values)
    ]
    return result, len(points) - len(result)


def _labs() -> dict[str, Any]:
    rows = get_repositories().labs.query(
        {"owner_email": OWNER_EMAIL}, "collected_date", 5000
    )
    by_test: dict[str, list[dict]] = {}
    reported_names: dict[str, set[str]] = {}
    qualification_counts = {"approved": 0, "edited": 0, "unverified": 0, "rejected": 0, "invalid": 0}
    for r in rows:
        if r.get("value") is None or not r.get("test_name"):
            continue
        quality = lab_qualification(r)
        verification = quality["verification_status"]
        qualification_counts[verification] = qualification_counts.get(verification, 0) + 1
        if quality["validation_status"] == "invalid":
            qualification_counts["invalid"] += 1
        if not lab_summary_eligible(r):
            continue
        # "Anion Gap" and "Anion gap" are one test; the newest row names it.
        key = canonical_lab_name(r["test_name"]).lower()
        by_test.setdefault(key, []).append({**r, "_date": _lab_date(r.get("collected_date"))})
        reported_names.setdefault(key, set()).add(" ".join(str(r["test_name"]).split()))

    timezone_name = config_value("app_timezone", APP_TIMEZONE)

    def time_fields(point: dict[str, Any]) -> dict[str, Any]:
        times = temporal_metadata("LabResult", point, default_timezone=timezone_name)
        return {"event_time": times.get("observed"), "ingestion_time": times.get("received")}

    def iso(point: dict[str, Any]) -> str | None:
        return point["_date"].isoformat() if point["_date"] else None

    categories: dict[str, list[dict]] = {}
    flagged = []
    key_results = []
    duplicates_removed = 0
    undated_results = 0
    for key, raw_points in by_test.items():
        points, removed = _dedupe_lab_points(raw_points)
        duplicates_removed += removed
        undated_results += sum(1 for p in points if p["_date"] is None)
        # Real calendar dates order the series; rows whose date could not be
        # parsed sort first, so they can never become the "latest" result
        # while a dated result exists.
        points.sort(key=lambda p: (p["_date"] is not None, p["_date"] or date.min, str(p.get("collected_date") or "")))
        latest = points[-1]
        test = canonical_lab_name(latest["test_name"])
        prev = points[-2] if len(points) >= 2 else None
        trend = None
        if (
            prev and prev.get("value") is not None
            and str(prev.get("unit") or "").lower() == str(latest.get("unit") or "").lower()
        ):
            delta = latest["value"] - prev["value"]
            trend = "up" if delta > 0 else "down" if delta < 0 else "flat"
        flag = _lab_flag(latest)
        entry = {
            "test_name": test,
            "reported_names": sorted(reported_names.get(key, set()) - {test}),
            "value": latest["value"],
            "unit": latest.get("unit", ""),
            "reference_low": latest.get("reference_low"),
            "reference_high": latest.get("reference_high"),
            "flag": flag,
            "collected_date": latest.get("collected_date", ""),
            "date": iso(latest),
            **time_fields(latest),
            "count": len(points),
            "trend": trend,
            "verification": lab_qualification(latest),
            "source": {
                "record_id": latest.get("record_id"),
                "page": latest.get("source_page"),
                "location": latest.get("extraction_location"),
            },
            "history": [
                {
                    "date": p.get("collected_date", ""),
                    "parsed_date": iso(p),
                    "value": p["value"],
                    "unit": p.get("unit", ""),
                    "verification": lab_qualification(p),
                    **time_fields(p),
                }
                for p in points[-6:]
            ],
        }
        categories.setdefault(latest.get("category") or "Other", []).append(entry)
        if flag in ABNORMAL_FLAGS:
            flagged.append(entry)
        if test in KEY_LABS:
            key_results.append(entry)
    for tests in categories.values():
        tests.sort(key=lambda entry: entry["test_name"].lower())
    key_results.sort(key=lambda entry: KEY_LABS.index(entry["test_name"]))
    # Newest draw first; undated results last.
    flagged.sort(key=lambda entry: (entry["date"] or "", entry["test_name"].lower()), reverse=True)
    flagged.sort(key=lambda entry: entry["date"] is None)
    return {
        "available": bool(by_test),
        "categories": categories,
        "flagged": flagged,
        "key_results": key_results,
        "duplicates_removed": duplicates_removed,
        "undated_results": undated_results,
        "verification": {
            "counts": qualification_counts,
            "note": "Machine-extracted results are labeled unverified until approved or corrected against the source document.",
        },
    }


NARRATIVE_MAX_TOKENS = 1500
# Hard cap on the narrative prompt in tokens, whatever window the serving
# model advertises. The full evidence dossier runs to ~1.3M characters on a
# real record (long document notes, dozens of TDD contradictions), so the
# prompt is built at the most detailed level that fits this budget: the
# deterministic metrics always, then a shrinking slice of clipped evidence.
NARRATIVE_PROMPT_BUDGET = 8000
NARRATIVE_EVIDENCE_ITEMS = 24
NARRATIVE_LIST_LIMIT = 10
NARRATIVE_TEXT_LIMIT = 240
NARRATIVE_ITEM_CHARS = 800
# With no tokenizer to ask, estimate tokens from length. Dense JSON runs
# under 2 characters per token, so 1.5 keeps the estimate on the safe side.
FALLBACK_CHARS_PER_TOKEN = 1.5
# Shedding ladder, most detailed first:
# (evidence items, contradictions and caveats kept, key labs, full detail).
_NARRATIVE_LEVELS = (
    (NARRATIVE_EVIDENCE_ITEMS, 10, "key", True),
    (12, 6, "key", True),
    (12, 6, "abnormal", False),
    (6, 4, "abnormal", False),
    (0, 3, "abnormal", False),
    (0, 0, None, False),
)
NARRATIVE_UNAVAILABLE_NOTE = (
    "Every figure in this report is computed directly from the data and is unaffected."
)
_INSULIN_AI_FIELDS = (
    "avg_tdd_est", "pump_reported_avg_tdd", "pump_reported_days", "calculated_avg_tdd",
    "calculated_days", "scheduled_avg_daily_basal", "incomplete_days", "discrepancy_days",
    "limitations", "temp_basal_corrections", "bolus_source", "bolus_stream_incomplete",
)
_QUALITY_AI_FIELDS = (
    "ai_eligible", "coverage_pct", "reliability", "data_through", "exclusion_reasons", "limitations",
)


class NarrativeUnavailable(RuntimeError):
    """The narrative was not generated; the message is safe to show the user."""


def _clip(
    value: Any,
    text_limit: int = NARRATIVE_TEXT_LIMIT,
    list_limit: int = NARRATIVE_LIST_LIMIT,
    depth: int = 0,
) -> Any:
    """Bound a JSON-like value: long strings truncated, long lists shortened."""
    if isinstance(value, str):
        return value if len(value) <= text_limit else value[:text_limit] + "…"
    if depth >= 5 and isinstance(value, (dict, list)):
        return "…"
    if isinstance(value, dict):
        # A block's own fields are kept; nested maps are capped like lists.
        kept = list(value.items()) if depth == 0 else list(value.items())[: list_limit * 3]
        return {key: _clip(item, text_limit, list_limit, depth + 1) for key, item in kept}
    if isinstance(value, list):
        return [_clip(item, text_limit, list_limit, depth + 1) for item in value[:list_limit]]
    return value


def _quality_for_ai(envelope: Any) -> Any:
    if not isinstance(envelope, dict):
        return None
    if "ai_eligible" not in envelope:  # a per-provider map (wearables)
        return {key: _quality_for_ai(value) for key, value in envelope.items()}
    return {key: _clip(envelope.get(key), 160, 3) for key in _QUALITY_AI_FIELDS if key in envelope}


def _narrative_metrics(payload: dict) -> dict[str, Any]:
    g = payload["glucose"]
    insulin_payload = payload["insulin"]
    insulin_for_ai = None
    if insulin_payload.get("available") and insulin_payload.get("quality", {}).get("ai_eligible"):
        insulin_for_ai = {key: insulin_payload.get(key) for key in _INSULIN_AI_FIELDS if key in insulin_payload}
        # A bolus figure from an incomplete stream reads as missed doses; leave it out.
        if not insulin_payload.get("bolus_stream_incomplete"):
            insulin_for_ai["avg_daily_bolus"] = insulin_payload.get("avg_daily_bolus")
            insulin_for_ai["boluses_per_day"] = insulin_payload.get("boluses_per_day")
        if (
            insulin_payload.get("nutrition_quality", {}).get("ai_eligible")
            and insulin_payload.get("carbs_representative", True)
        ):
            insulin_for_ai["avg_daily_carbs"] = insulin_payload.get("avg_daily_carbs")
    response_payload = payload.get("insulin_response") or {}
    activity_position_payload = payload.get("activity_position") or {}
    response_for_ai = None
    if (
        response_payload.get("available")
        and response_payload.get("quality", {}).get("ai_eligible")
    ):
        response_for_ai = {
            "algorithm_version": response_payload.get("algorithm_version"),
            "counts": response_payload.get("counts"),
            "analysis": response_payload.get("analysis"),
            "confidence": response_payload.get("confidence"),
            "semantics": response_payload.get("semantics"),
            "assumptions": response_payload.get("assumptions"),
        }
    wellness_for_ai = {
        provider: payload["wellness"].get(provider)
        for provider in ("oura", "fitbit")
        if payload["wellness"].get(provider)
        and payload["wellness"].get("quality", {}).get(provider, {}).get("ai_eligible")
    }
    quality = {
        "glucose": _quality_for_ai(g.get("quality")),
        "pump_tdd": _quality_for_ai(insulin_payload.get("quality")),
        "nutrition": _quality_for_ai(insulin_payload.get("nutrition_quality")),
        "insulin_response": _quality_for_ai(response_payload.get("quality")),
        "cycle": _quality_for_ai(payload["cycle"].get("quality")),
        "wearables": _quality_for_ai(payload["wellness"].get("quality")),
    }
    fingersticks = g.get("fingerstick_reconciliation") or {}
    weight = payload.get("weight") or {}
    labs = payload.get("labs") or {}
    patient = payload.get("patient") or {}
    metrics = {
        "period_days": payload["days"],
        "patient": {"age": patient.get("age"), "sex": patient.get("sex")} if patient.get("age") else None,
        "weight": {
            key: weight.get(key)
            for key in ("latest", "entries", "peak", "peak_change", "window_change")
        }
        if weight.get("available") else None,
        "glucose": {
            **{k: g.get(k) for k in ("avg", "gmi", "cv", "tir", "tbr70", "tbr54", "tar180", "tar250", "days", "low_episodes")},
            "consensus_targets_pct": {"tbr70_below": TBR70_TARGET_PCT, "tbr54_below": TBR54_TARGET_PCT, "tir_above": 70},
            "fingerstick_reconciliation": {
                key: fingersticks.get(key)
                for key in ("paired", "mean_abs_delta", "persistent_bias", "low_reconciliation", "semantics")
                if key in fingersticks
            } or None,
        }
        if g.get("available") and g.get("quality", {}).get("ai_eligible") else None,
        "insulin": insulin_for_ai,
        "insulin_response": response_for_ai,
        "activity_position": {
            "algorithm_version": activity_position_payload.get("algorithm_version"),
            "semantics": activity_position_payload.get("semantics"),
            "qualifying_effects": activity_position_payload.get("qualifying_effects"),
        }
        if activity_position_payload.get("qualifying_effects")
        else None,
        "cycle": {
            "cycles": payload["cycle"].get("cycles_detected"),
            "avg_length": payload["cycle"].get("avg_cycle_length"),
            "per_phase": payload["cycle"].get("per_phase"),
            "phase_provenance": payload["cycle"].get("phase_provenance"),
        }
        if payload["cycle"].get("available") and payload["cycle"].get("quality", {}).get("ai_eligible")
        else None,
        "wellness": wellness_for_ai,
        "key_labs": [
            {
                "test": entry.get("test_name"),
                "value": entry.get("value"),
                "unit": entry.get("unit"),
                "date": entry.get("date") or entry.get("collected_date"),
                "flag": entry.get("flag"),
                "verification_status": (entry.get("verification") or {}).get("verification_status"),
            }
            for entry in labs.get("key_results") or []
        ] or None,
        "data_quality": quality,
    }
    # Key labs are already a fixed short panel; everything else is bounded.
    return {key: value if key == "key_labs" else _clip(value, 160, 5) for key, value in metrics.items()}


_SEVERITY_ORDER = {"blocking": 0, "warning": 1}


def _compact_contradiction(entry: dict[str, Any]) -> dict[str, Any]:
    """Both sides of a contradiction, without its detection plumbing."""
    return {
        key: _clip(entry.get(key), 160, 4)
        for key in ("domain", "severity", "explanation", "left", "right")
        if entry.get(key) not in (None, "", {}, [])
    }


def _abnormal_lab(entry: dict[str, Any]) -> bool:
    return entry.get("test") == "Hemoglobin A1c" or str(entry.get("flag") or "normal") != "normal"


def _narrative_level(
    metrics: dict[str, Any], evidence: dict[str, Any], level: tuple[int, int, str | None, bool]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The metrics and evidence at one rung of the shedding ladder."""
    item_count, note_count, labs_mode, detail = level
    metrics = dict(metrics)
    if labs_mode is None:
        metrics.pop("key_labs", None)
    elif labs_mode == "abnormal" and metrics.get("key_labs"):
        metrics["key_labs"] = [entry for entry in metrics["key_labs"] if _abnormal_lab(entry)] or None
    if not detail:
        # The headline numbers stay; algorithm metadata and quality prose go.
        metrics.pop("activity_position", None)
        if metrics.get("insulin_response"):
            metrics["insulin_response"] = {
                key: metrics["insulin_response"].get(key) for key in ("counts", "confidence")
            }
        if metrics.get("insulin"):
            metrics["insulin"] = {key: value for key, value in metrics["insulin"].items() if key != "limitations"}
        if metrics.get("cycle"):
            metrics["cycle"] = {key: value for key, value in metrics["cycle"].items() if key != "phase_provenance"}
        metrics["data_quality"] = {
            domain: _clip(envelope, 60, 3) for domain, envelope in (metrics.get("data_quality") or {}).items()
        }
    items = evidence["items"]
    contradictions = evidence["contradictions"]
    caveats = evidence["missing_data_caveats"]
    trimmed = {
        "data_through": evidence.get("data_through"),
        "contradictions": contradictions[:note_count],
        "missing_data_caveats": caveats[:note_count],
        "items": items[:item_count],
    }
    available = evidence.get("items_available", len(items))
    if available > item_count:
        trimmed["items_truncated"] = {"kept": min(item_count, len(items)), "available": available}
    if len(contradictions) > note_count:
        trimmed["contradictions_truncated"] = {
            "kept": note_count,
            "unresolved": len(contradictions),
            "blocking": sum(entry.get("severity") == "blocking" for entry in contradictions),
        }
    return metrics, trimmed


def _narrative_evidence(evidence_reasoning: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """The evidence at its most detailed level: the first items, clipped,
    and the unresolved contradictions with blocking ones first."""
    slim, alias_to_id = prompt_evidence_view(evidence_reasoning)
    items = slim.get("items") or []
    contradictions = sorted(
        slim.get("contradictions") or [],
        key=lambda entry: _SEVERITY_ORDER.get(str(entry.get("severity")), 2),
    )
    bounded = {
        "data_through": slim.get("data_through"),
        "contradictions": [_compact_contradiction(entry) for entry in contradictions],
        "missing_data_caveats": [
            _clip(entry, 160, 4) for entry in (slim.get("missing_data_caveats") or [])[:NARRATIVE_LIST_LIMIT]
        ],
        "items": [_clip_item(item) for item in items[:NARRATIVE_EVIDENCE_ITEMS]],
        "items_available": len(items),
    }
    kept = {item["id"] for item in bounded["items"]}
    return bounded, {alias: real for alias, real in alias_to_id.items() if alias in kept}


def _clip_item(item: dict[str, Any]) -> dict[str, Any]:
    """One evidence item, clipped harder when its data is unusually wide."""
    clipped = _clip(item, 160, 4)
    if len(json.dumps(clipped, default=str, ensure_ascii=False)) > NARRATIVE_ITEM_CHARS:
        clipped = _clip(item, 60, 3)
    return clipped


def _estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / FALLBACK_CHARS_PER_TOKEN)


def _narrative_prompt(days: int, metrics: dict[str, Any], evidence: dict[str, Any]) -> str:
    data = json.dumps(
        {"deterministic_metrics": metrics, "shared_evidence_context": evidence},
        default=str,
        ensure_ascii=False,
    )
    return f"""You are a diabetes data analyst preparing a summary for a Type 1 diabetes patient to bring to her endocrinologist. You are NOT a physician; this is an observational data summary to support the clinical conversation, never a diagnosis or treatment recommendation.

Data for the last {days} days (JSON):
{data}

Write a concise, professional "quarter in review" for the care team. Reference the actual numbers. Judge hypoglycemia against the consensus targets supplied (time below 70 mg/dL under 4%, below 54 mg/dL under 1%); when either is exceeded, say so plainly in the headline or glucose summary — a high time in range does not offset excess time below range. Weight change is given from the 12-month high (peak_change) and over the report window (window_change); when either shows a loss, state the amount, percentage, and period. When contradictions_truncated is present, say how many unresolved contradictions exist in total. Insulin figures marked bolus_stream_incomplete have no reliable bolus/basal split; never describe that as missed or skipped doses. Explicitly call machine-extracted labs "unverified" unless their verification status is approved or edited; never imply that parser confidence is clinical verification. For every unresolved contradiction, present both sides and say it remains unresolved; never silently choose one value, especially for a blocking contradiction. If health-history or symptom evidence is present, use it as context without treating it as a clinician-confirmed fact. Note relationships worth discussing (e.g. cycle-phase patterns, glucose vs. sleep/activity, symptoms vs. labs), always as observations to explore with the clinician — never as instructions to change therapy. Distinguish explicitly recorded/imported cycle phase days from algorithm-inferred days. Scale every analytics claim to its supplied discovery status and numerical confidence metadata; exploratory, emerging, or not-reproduced results cannot be presented as definitive. Write discussion_points as short, direct items the patient can raise and the clinician can act on, most clinically important first (hypoglycemia, weight change measured from the 12-month high, out-of-range labs, reported symptoms); keep statistics vocabulary (correlation coefficients, replication, discovery status, algorithm names) out of the text. Copy the IDs of every Evidence Bundle item used into evidence_item_ids; never invent an ID. Keep it factual and readable."""


async def _fitted_narrative_prompt(days: int, metrics: dict[str, Any], evidence: dict[str, Any]) -> str:
    """The most detailed prompt that fits NARRATIVE_PROMPT_BUDGET tokens, or
    the serving model's window when that is smaller.

    Tokens are counted by the serving model's tokenizer when it has one, and
    estimated conservatively from length when it doesn't — the budget holds
    either way.
    """
    budget = NARRATIVE_PROMPT_BUDGET
    limit = await context_limit()
    if limit:
        budget = min(budget, limit - NARRATIVE_MAX_TOKENS - CONTEXT_SAFETY_MARGIN)
    used = None
    for rung, level in enumerate(_NARRATIVE_LEVELS):
        candidate = _narrative_prompt(days, *_narrative_level(metrics, evidence, level))
        used = await count_tokens(candidate)
        if used is None:
            used = _estimate_tokens(candidate)
        if used <= budget:
            if rung:
                log.info("visit narrative prompt trimmed to level %s (%s tokens, budget %s)", rung, used, budget)
            return candidate
    log.warning("visit narrative prompt does not fit: %s tokens at the smallest level, budget %s", used, budget)
    raise NarrativeUnavailable("the report data did not fit the language model's context window")


async def _narrative(payload: dict) -> dict[str, Any] | None:
    """Generate the quarter-in-review narrative, or None when it can't be.

    A failure is logged and recorded in payload["narrative_status"] so the
    page can say the narrative is unavailable instead of silently omitting it.
    """
    evidence_reasoning = payload.get("_evidence_reasoning") or {
        "bundle_id": None,
        "items": [],
        "contradictions": [],
        "missing_data_caveats": [],
    }
    metrics = _narrative_metrics(payload)
    evidence, alias_to_id = _narrative_evidence(evidence_reasoning)
    # Fast default model: the quality (27B) model is currently GPU-starved and
    # takes minutes for 1500 tokens, which hangs the report. The fast model
    # produces a solid structured narrative in seconds.
    try:
        prompt = await _fitted_narrative_prompt(payload["days"], metrics, evidence)
        generated = await invoke_llm(
            prompt,
            response_json_schema={
                "type": "object",
                "properties": {
                    "headline": {"type": "string", "description": "One-sentence overall summary of the quarter"},
                    "glucose_summary": {"type": "string", "description": "2-3 sentences on glucose control"},
                    "cycle_summary": {"type": "string", "description": "1-2 sentences on cycle-phase patterns, or empty if no cycle data"},
                    "lifestyle_summary": {"type": "string", "description": "1-2 sentences tying in weight/sleep/activity/labs, or empty"},
                    "discussion_points": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "3-5 specific things worth discussing with the care team",
                    },
                    "evidence_item_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "IDs copied only from shared_evidence_context.items that support the generated narrative",
                    },
                },
                "required": ["headline", "glucose_summary", "discussion_points", "evidence_item_ids"],
            },
            max_tokens=NARRATIVE_MAX_TOKENS,
            site="visit_narrative",
        )
    except NarrativeUnavailable as err:
        log.warning("visit report narrative skipped: %s", err)
        payload["narrative_status"] = {"available": False, "reason": f"The written summary is unavailable: {err}."}
        return None
    except Exception as err:  # noqa: BLE001 — any provider failure degrades to "unavailable"
        log.exception("visit report narrative failed (%s)", type(err).__name__)
        payload["narrative_status"] = {
            "available": False,
            "reason": "The written summary is unavailable: the language model request failed.",
        }
        return None
    if not isinstance(generated, dict) or not generated.get("headline"):
        log.warning("visit report narrative returned no headline")
        payload["narrative_status"] = {
            "available": False,
            "reason": "The written summary is unavailable: the language model returned an empty response.",
        }
        return None
    generated = resolve_prompt_aliases(generated, alias_to_id)
    return link_generated_narrative(generated, evidence_reasoning)


class ReportBody(BaseModel):
    days: int = 90


def _demo_narrative_allowed(payload: dict[str, Any]) -> bool:
    return bool(
        payload["glucose"].get("quality", {}).get("ai_eligible")
        and payload["insulin"].get("quality", {}).get("ai_eligible")
        and payload["insulin"].get("nutrition_quality", {}).get("ai_eligible")
        and payload["cycle"].get("quality", {}).get("ai_eligible")
        and any(
            payload["wellness"].get("quality", {}).get(provider, {}).get("ai_eligible")
            for provider in ("oura", "fitbit")
        )
    )


def _evidence_quality(payload: dict[str, Any]) -> dict[str, Any]:
    wellness_quality = payload["wellness"].get("quality", {})
    return {
        "cgm": payload["glucose"].get("quality"),
        "pump_tdd": payload["insulin"].get("quality"),
        "nutrition": payload["insulin"].get("nutrition_quality"),
        "insulin_response": (payload.get("insulin_response") or {}).get("quality"),
        "cycle": payload["cycle"].get("quality"),
        "oura": wellness_quality.get("oura"),
        "fitbit": wellness_quality.get("fitbit"),
    }


@router.post("/api/report/visit")
async def visit_report(body: ReportBody):
    days = max(7, min(body.days, 365))
    tz = ZoneInfo(config_value("app_timezone", APP_TIMEZONE))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_iso = since.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    glucose = _glucose(tz, since_iso)
    glucose["fingerstick_reconciliation"] = summarize_fingersticks(
        _paged("FingerstickReading", since_iso)
    )
    insulin = _insulin(tz, since_iso, glucose.get("days", 0))
    cycle = _cycle(tz, since_iso, glucose)
    wellness = _wellness(days)
    labs = _labs()

    from . import (
        care_notes,
        conditions,
        activity_position,
        episodes,
        history,
        hypotheses,
        insulin as insulin_analytics,
        insurance,
        management_burden,
        meds,
        pump_settings,
        symptoms,
    )
    insulin_response = insulin_analytics.absorption()
    insulin_response.pop("events", None)

    payload = {
        "conditions": conditions.report_block(),
        "hypotheses": hypotheses.report_block(),
        "health_episodes": episodes.report_block(),
        "activity_position": activity_position.report_block(days),
        "management_burden": management_burden.report_block(days),
        "medications": meds.get_medications(),
        "allergies": meds.get_allergies(),
        "history": history.report_block(),
        "symptoms": symptoms.report_block(days),
        "care_notes": care_notes.report_block(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "days": days,
        "start_date": since_iso[:10],
        "end_date": datetime.now(timezone.utc).date().isoformat(),
        "patient": _patient(),
        "weight": _weight(since.astimezone(tz).date()),
        "glucose": glucose,
        "insulin": insulin,
        "pump_settings": pump_settings.report_block(),
        "insulin_response": insulin_response,
        "cycle": cycle,
        "wellness": wellness,
        "labs": labs,
        "insurance": insurance.report_block(),
    }
    evidence_context, evidence_reasoning = build_clinical_evidence(
        days,
        data_quality=_evidence_quality(payload),
        as_of=datetime.now(timezone.utc).date(),
    )
    payload["evidence_context"] = evidence_context
    payload["_evidence_reasoning"] = evidence_reasoning
    unresolved = evidence_context["contradictions"]
    payload["contradictions"] = {
        "unresolved": unresolved,
        "counts": {
            "unresolved": len(unresolved),
            "blocking": sum(item["severity"] == "blocking" for item in unresolved),
        },
    }
    payload["narrative"] = await _narrative(payload)
    if payload["narrative"] is None and DEMO_MODE and _demo_narrative_allowed(payload):
        # Self-contained demo: show a representative narrative even without an
        # LLM configured, but only when its source domains pass the same quality
        # contract as a generated narrative.
        payload["narrative"] = link_generated_narrative(
            {**DEMO_NARRATIVE, "evidence_item_ids": []},
            evidence_reasoning,
        )
    if payload["narrative"]:
        payload["narrative_status"] = {"available": True, "reason": None}
    else:
        payload.setdefault("narrative_status", {
            "available": False,
            "reason": "The written summary is unavailable: it was not generated.",
        })
    payload["narrative_status"]["note"] = (
        None if payload["narrative_status"]["available"] else NARRATIVE_UNAVAILABLE_NOTE
    )
    payload.pop("_evidence_reasoning", None)
    return payload
