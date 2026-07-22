"""90-day Visit Report — a clinical-discussion summary Emily prints for her doctor.

Computes standardized glucose metrics (TIR/GMI/CV + AGP-style hourly
percentiles), insulin totals, per-cycle-phase breakdowns, sleep/recovery
averages, and lab values with trends, then asks the LLM for an
observational "quarter in review" narrative (explicitly non-diagnostic —
a data summary to support the clinical conversation, not replace it).

Read-only: available to both admin and provider sessions.
"""

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .auth import require_login
from .canonical_time import temporal_metadata
from .config import APP_TIMEZONE, DEMO_MODE, OWNER_EMAIL
from .contradictions import contradiction_context
from .db import config_value
from .data_quality import assess_cgm, assess_daily, assess_nutrition, assess_pump_tdd, cgm_points
from .insulin_reconciliation import reconcile_treatments
from .lab_audit import qualification as lab_qualification
from .lab_audit import summary_eligible as lab_summary_eligible
from .llm import invoke_llm
from .repositories import get_repositories

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
        "quality": quality,
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
    return {
        "available": bool(bolus_count or reported_days or calculated_days or scheduled_days or carb_records),
        "avg_daily_bolus": round(total_bolus / days, 1),
        "boluses_per_day": round(bolus_count / days, 1),
        "avg_daily_carbs": round(total_carbs / days),
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
        "quality": quality,
    }


def _avg(rows: list[dict], field: str) -> float | None:
    vals = [float(r[field]) for r in rows if r.get(field) is not None]
    return sum(vals) / len(vals) if vals else None


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
    rows = get_repositories().labs.query(
        {"owner_email": OWNER_EMAIL}, "collected_date", 5000
    )
    by_test: dict[str, list[dict]] = {}
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
        by_test.setdefault(r["test_name"], []).append(r)

    timezone_name = config_value("app_timezone", APP_TIMEZONE)

    def time_fields(point: dict[str, Any]) -> dict[str, Any]:
        times = temporal_metadata("LabResult", point, default_timezone=timezone_name)
        return {"event_time": times.get("observed"), "ingestion_time": times.get("received")}

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
                    "value": p["value"],
                    "verification": lab_qualification(p),
                    **time_fields(p),
                }
                for p in points[-6:]
            ],
        }
        categories.setdefault(latest.get("category") or "Other", []).append(entry)
        if latest.get("flag") and latest["flag"] not in ("normal", ""):
            flagged.append(entry)
    return {
        "available": bool(by_test),
        "categories": categories,
        "flagged": flagged,
        "verification": {
            "counts": qualification_counts,
            "note": "Machine-extracted results are labeled unverified until approved or corrected against the source document.",
        },
    }


async def _narrative(payload: dict) -> dict[str, Any] | None:
    g = payload["glucose"]
    insulin_payload = payload["insulin"]
    insulin_for_ai = None
    if insulin_payload.get("available") and insulin_payload.get("quality", {}).get("ai_eligible"):
        insulin_for_ai = dict(insulin_payload)
        if not insulin_payload.get("nutrition_quality", {}).get("ai_eligible"):
            insulin_for_ai.pop("avg_daily_carbs", None)
    wellness_for_ai = {
        provider: payload["wellness"].get(provider)
        for provider in ("oura", "fitbit")
        if payload["wellness"].get(provider)
        and payload["wellness"].get("quality", {}).get(provider, {}).get("ai_eligible")
    }
    quality = {
        "glucose": g.get("quality"),
        "pump_tdd": insulin_payload.get("quality"),
        "nutrition": insulin_payload.get("nutrition_quality"),
        "cycle": payload["cycle"].get("quality"),
        "wearables": payload["wellness"].get("quality"),
    }
    summary = {
        "period_days": payload["days"],
        "glucose": {k: g.get(k) for k in ("avg", "gmi", "cv", "tir", "tbr70", "tar180")}
        if g.get("available") and g.get("quality", {}).get("ai_eligible") else None,
        "insulin": insulin_for_ai,
        "cycle": {
            "cycles": payload["cycle"].get("cycles_detected"),
            "avg_length": payload["cycle"].get("avg_cycle_length"),
            "per_phase": payload["cycle"].get("per_phase"),
        }
        if payload["cycle"].get("available") and payload["cycle"].get("quality", {}).get("ai_eligible")
        else None,
        "wellness": wellness_for_ai,
        "flagged_labs": [
            {
                "test": f["test_name"], "value": f["value"], "unit": f["unit"],
                "flag": f["flag"], "verification": f["verification"],
            }
            for f in payload["labs"].get("flagged", [])
        ],
        "lab_verification": payload["labs"].get("verification"),
        "symptom_journal": payload.get("symptoms"),
        "health_history": payload.get("history"),
        "data_quality": quality,
        "unresolved_contradictions": [
            {
                "severity": item["severity"],
                "domain": item["domain"],
                "explanation": item["explanation"],
                "left": item["left"],
                "right": item["right"],
                "detection_state": item["detection_state"],
            }
            for item in payload.get("contradictions", {}).get("unresolved", [])
        ],
    }
    # Fast default model: the quality (27B) model is currently GPU-starved and
    # takes minutes for 1500 tokens, which hangs the report. The fast model
    # produces a solid structured narrative in seconds.
    try:
        return await invoke_llm(
            f"""You are a diabetes data analyst preparing a summary for a Type 1 diabetes patient to bring to her endocrinologist. You are NOT a physician; this is an observational data summary to support the clinical conversation, never a diagnosis or treatment recommendation.

Data for the last {payload['days']} days:
{summary}

Write a concise, professional "quarter in review" for the care team. Reference the actual numbers. Explicitly call machine-extracted labs "unverified" unless their verification status is approved or edited; never imply that parser confidence is clinical verification. For every unresolved contradiction, present both sides and say it remains unresolved; never silently choose one value, especially for a blocking contradiction. If a health_history is present, use it as background (diagnoses, exposures, injuries, hospital visits, and the patient's own narrative) to frame what you observe. If a symptom_journal is present, summarize the symptoms she has actually reported (how often and how severe) and note any that coincide with the data. Note relationships worth discussing (e.g. cycle-phase patterns, glucose vs. sleep/activity, symptoms vs. labs), always as observations to explore with the clinician — never as instructions to change therapy. Keep it factual and readable.""",
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
        )
    except Exception:
        return None


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
    contradictions = contradiction_context(refresh=True, limit=100)

    from . import conditions, history, insurance, meds, symptoms

    payload = {
        "conditions": conditions.report_block(),
        "medications": meds.get_medications(),
        "allergies": meds.get_allergies(),
        "history": history.report_block(),
        "symptoms": symptoms.report_block(days),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "days": days,
        "start_date": since_iso[:10],
        "end_date": datetime.now(timezone.utc).date().isoformat(),
        "glucose": glucose,
        "insulin": insulin,
        "cycle": cycle,
        "wellness": wellness,
        "labs": labs,
        "contradictions": contradictions,
        "insurance": insurance.report_block(),
    }
    payload["narrative"] = await _narrative(payload)
    if payload["narrative"] is None and DEMO_MODE and _demo_narrative_allowed(payload):
        # Self-contained demo: show a representative narrative even without an
        # LLM configured, but only when its source domains pass the same quality
        # contract as a generated narrative.
        payload["narrative"] = DEMO_NARRATIVE
    return payload
