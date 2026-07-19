"""90-day Visit Report — a clinical-discussion summary Emily prints for her doctor.

Computes standardized glucose metrics (TIR/GMI/CV + AGP-style hourly
percentiles), insulin totals, per-cycle-phase breakdowns, sleep/recovery
averages, and lab values with trends, then asks the LLM for an
observational "quarter in review" narrative (explicitly non-diagnostic —
a data summary to support the clinical conversation, not replace it).

Read-only: available to both admin and provider sessions.
"""

import math
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from . import db
from .auth import require_login
from .config import APP_TIMEZONE, DEMO_MODE, OWNER_EMAIL
from .db import config_value
from .llm import invoke_llm

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
    out, skip = [], 0
    while True:
        page = db.query_entities(
            etype, {"owner_email": OWNER_EMAIL, "timestamp": {"$gte": since_iso}}, "timestamp", 5000, skip
        )
        out.extend(page)
        if len(page) < 5000:
            break
        skip += 5000
    return out


def _glucose(tz: ZoneInfo, since_iso: str) -> dict[str, Any]:
    readings = _paged("GlucoseReading", since_iso)
    values = [float(r["value"]) for r in readings if r.get("value") is not None]
    if not values:
        return {"available": False}
    n = len(values)
    avg = sum(values) / n
    std = math.sqrt(sum((v - avg) ** 2 for v in values) / n)
    by_hour: dict[int, list[float]] = {}
    by_day: dict[str, list[float]] = {}
    for r in readings:
        ts = _parse_ts(r.get("timestamp"))
        if ts is None or r.get("value") is None:
            continue
        local = ts.astimezone(tz)
        by_hour.setdefault(local.hour, []).append(float(r["value"]))
        by_day.setdefault(local.date().isoformat(), []).append(float(r["value"]))

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
        "agp": agp,
        "daily": daily,
    }


def _insulin(tz: ZoneInfo, since_iso: str, glucose_days: int) -> dict[str, Any]:
    treatments = _paged("Treatment", since_iso)
    days = max(glucose_days, 1)
    total_bolus = 0.0
    bolus_count = 0
    total_carbs = 0.0
    basal_est = 0.0
    for t in treatments:
        ttype = t.get("type")
        if ttype == "insulin" and t.get("amount") and t.get("event_type") != "Daily Total":
            total_bolus += float(t["amount"])
            bolus_count += 1
        elif ttype == "carb" and t.get("amount"):
            total_carbs += float(t["amount"])
        elif ttype == "tempbasal" and t.get("absolute") is not None and t.get("duration"):
            basal_est += float(t["absolute"]) * float(t["duration"]) / 60.0
    return {
        "available": bolus_count > 0 or basal_est > 0,
        "avg_daily_bolus": round(total_bolus / days, 1),
        "boluses_per_day": round(bolus_count / days, 1),
        "avg_daily_carbs": round(total_carbs / days),
        "avg_daily_basal_est": round(basal_est / days, 1),
        "avg_tdd_est": round((total_bolus + basal_est) / days, 1),
        "has_basal": basal_est > 0,
    }


def _cycle(tz: ZoneInfo, since_iso: str, glucose: dict) -> dict[str, Any]:
    since_date = since_iso[:10]
    logs = [
        l
        for l in db.query_entities("PeriodLog", {"owner_email": OWNER_EMAIL}, "date", 5000)
        if l.get("date") and l["date"] >= since_date and l.get("phase")
    ]
    if not logs:
        return {"available": False}
    phase_days: dict[str, set[str]] = {}
    for l in logs:
        phase_days.setdefault(l["phase"], set()).add(l["date"])

    daily_tir = {d["date"]: d["tir"] for d in glucose.get("daily", [])}
    daily_avg = {d["date"]: d["avg"] for d in glucose.get("daily", [])}

    per_phase = {}
    for phase, dates in phase_days.items():
        tirs = [daily_tir[d] for d in dates if d in daily_tir]
        avgs = [daily_avg[d] for d in dates if d in daily_avg]
        per_phase[phase] = {
            "days": len(dates),
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
    }


def _avg(rows: list[dict], field: str) -> float | None:
    vals = [float(r[field]) for r in rows if r.get(field) is not None]
    return sum(vals) / len(vals) if vals else None


def _wellness(days: int) -> dict[str, Any]:
    oura = db.query_entities("OuraDaily", {"owner_email": OWNER_EMAIL}, "-date", days)
    fitbit = db.query_entities("FitbitDaily", {"owner_email": OWNER_EMAIL}, "-date", days)
    out: dict[str, Any] = {"oura": None, "fitbit": None}
    if oura:
        temps = [float(r["readiness_temperature_deviation"]) for r in oura if r.get("readiness_temperature_deviation") is not None]
        out["oura"] = {
            "days": len(oura),
            "avg_sleep_score": round(_avg(oura, "sleep_score")) if _avg(oura, "sleep_score") else None,
            "avg_readiness_score": round(_avg(oura, "readiness_score")) if _avg(oura, "readiness_score") else None,
            "avg_resting_hr": round(_avg(oura, "lowest_heart_rate")) if _avg(oura, "lowest_heart_rate") else None,
            "avg_spo2": round(_avg(oura, "spo2_average"), 1) if _avg(oura, "spo2_average") else None,
            "temp_range": f"{min(temps):+.1f} to {max(temps):+.1f} °C" if temps else None,
        }
    if fitbit:
        out["fitbit"] = {
            "days": len(fitbit),
            "avg_steps": round(_avg(fitbit, "steps")) if _avg(fitbit, "steps") else None,
            "avg_resting_hr": round(_avg(fitbit, "resting_heart_rate")) if _avg(fitbit, "resting_heart_rate") else None,
            "avg_sleep_hours": round(_avg(fitbit, "sleep_minutes") / 60, 1) if _avg(fitbit, "sleep_minutes") else None,
            "avg_spo2": round(_avg(fitbit, "spo2_avg"), 1) if _avg(fitbit, "spo2_avg") else None,
        }
    return out


def _labs() -> dict[str, Any]:
    rows = db.query_entities("LabResult", {"owner_email": OWNER_EMAIL}, "collected_date", 5000)
    by_test: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("value") is None or not r.get("test_name"):
            continue
        by_test.setdefault(r["test_name"], []).append(r)

    categories: dict[str, list[dict]] = {}
    flagged = []
    for test, points in by_test.items():
        points.sort(key=lambda p: str(p.get("collected_date") or ""))
        latest = points[-1]
        prev = points[-2] if len(points) >= 2 else None
        trend = None
        if prev and prev.get("value") is not None:
            delta = latest["value"] - prev["value"]
            trend = "up" if delta > 0 else "down" if delta < 0 else "flat"
        entry = {
            "test_name": test,
            "value": latest["value"],
            "unit": latest.get("unit", ""),
            "reference_low": latest.get("reference_low"),
            "reference_high": latest.get("reference_high"),
            "flag": latest.get("flag", ""),
            "collected_date": latest.get("collected_date", ""),
            "count": len(points),
            "trend": trend,
            "history": [{"date": p.get("collected_date", ""), "value": p["value"]} for p in points[-6:]],
        }
        categories.setdefault(latest.get("category") or "Other", []).append(entry)
        if latest.get("flag") and latest["flag"] not in ("normal", ""):
            flagged.append(entry)
    return {"available": bool(by_test), "categories": categories, "flagged": flagged}


async def _narrative(payload: dict) -> dict[str, Any] | None:
    g = payload["glucose"]
    summary = {
        "period_days": payload["days"],
        "glucose": {k: g.get(k) for k in ("avg", "gmi", "cv", "tir", "tbr70", "tar180")} if g.get("available") else None,
        "insulin": payload["insulin"] if payload["insulin"].get("available") else None,
        "cycle": {
            "cycles": payload["cycle"].get("cycles_detected"),
            "avg_length": payload["cycle"].get("avg_cycle_length"),
            "per_phase": payload["cycle"].get("per_phase"),
        }
        if payload["cycle"].get("available")
        else None,
        "wellness": payload["wellness"],
        "flagged_labs": [
            {"test": f["test_name"], "value": f["value"], "unit": f["unit"], "flag": f["flag"]}
            for f in payload["labs"].get("flagged", [])
        ],
    }
    try:
        return await invoke_llm(
            f"""You are a diabetes data analyst preparing a summary for a Type 1 diabetes patient to bring to her endocrinologist. You are NOT a physician; this is an observational data summary to support the clinical conversation, never a diagnosis or treatment recommendation.

Data for the last {payload['days']} days:
{summary}

Write a concise, professional "quarter in review" for the care team. Reference the actual numbers. Note relationships worth discussing (e.g. cycle-phase patterns, glucose vs. sleep/activity), always as observations to explore with the clinician — never as instructions to change therapy. Keep it factual and readable.""",
            response_json_schema={
                "type": "object",
                "properties": {
                    "headline": {"type": "string", "description": "One-sentence overall summary of the quarter"},
                    "glucose_summary": {"type": "string", "description": "2-3 sentences on glucose control"},
                    "cycle_summary": {"type": "string", "description": "1-2 sentences on cycle-phase patterns, or empty if no cycle data"},
                    "lifestyle_summary": {"type": "string", "description": "1-2 sentences tying in sleep/activity/labs, or empty"},
                    "discussion_points": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "3-5 specific things worth discussing with the care team",
                    },
                },
                "required": ["headline", "glucose_summary", "discussion_points"],
            },
            max_tokens=1500,
            tier="quality",
        )
    except Exception:
        return None


class ReportBody(BaseModel):
    days: int = 90


@router.post("/api/report/visit")
async def visit_report(body: ReportBody):
    days = max(7, min(body.days, 365))
    tz = ZoneInfo(config_value("app_timezone", APP_TIMEZONE))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_iso = since.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    glucose = _glucose(tz, since_iso)
    insulin = _insulin(tz, since_iso, glucose.get("days", 0))
    cycle = _cycle(tz, since_iso, glucose)
    wellness = _wellness(days)
    labs = _labs()

    from . import conditions, insurance, meds

    payload = {
        "conditions": conditions.report_block(),
        "medications": meds.get_medications(),
        "allergies": meds.get_allergies(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "days": days,
        "start_date": since_iso[:10],
        "end_date": datetime.now(timezone.utc).date().isoformat(),
        "glucose": glucose,
        "insulin": insulin,
        "cycle": cycle,
        "wellness": wellness,
        "labs": labs,
        "insurance": insurance.report_block(),
    }
    payload["narrative"] = await _narrative(payload)
    if payload["narrative"] is None and DEMO_MODE:
        # Self-contained demo: show a representative narrative even without an
        # LLM configured, so the report looks complete in screenshots.
        payload["narrative"] = DEMO_NARRATIVE
    return payload
