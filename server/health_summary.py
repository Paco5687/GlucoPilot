"""Cross-domain health summary (Phase 2).

Synthesizes a single "big picture" narrative across every domain — glucose,
labs (blood/urine/neuro/gut + imaging measurements), menstrual cycle, and
wearables (HRV/RHR/sleep) — and asks the quality LLM to spot *interesting
connections* a person could never eyeball across 450+ analytes. Observational
only: it surfaces patterns and things to watch, never diagnoses.

Stored as a single HealthSummary entity (replaced on each run). Regenerated
periodically by the scheduler; can also be forced from the Overview page.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import conditions, db, meds, profile, report
from .config import APP_TIMEZONE, OWNER_EMAIL
from .db import config_value, set_config_value
from .llm import invoke_llm

log = logging.getLogger("glucopilot.health_summary")

WINDOW_DAYS = 90

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "One plain-language sentence capturing the overall picture."},
        "observations": {
            "type": "array",
            "description": "The interesting cross-domain connections — where two or more data sources line up.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short, concrete (e.g. 'Higher HRV weeks track with steadier glucose')"},
                    "detail": {"type": "string", "description": "2-4 sentences, plain and direct, citing the actual numbers. State the fact/indicator; no hedging boilerplate."},
                    "domains": {"type": "array", "items": {"type": "string"}, "description": "e.g. ['glucose','wearables'], ['labs','cycle'], ['imaging']"},
                },
                "required": ["title", "detail"],
            },
        },
        "working": {"type": "array", "items": {"type": "string"}, "description": "A few things that look good / on track."},
        "watch": {"type": "array", "items": {"type": "string"}, "description": "A few things worth keeping an eye on or raising with the care team."},
    },
    "required": ["headline", "observations"],
}


def _labs_snapshot() -> tuple[list, list]:
    rows = db.query_entities("LabResult", {"owner_email": OWNER_EMAIL}, "collected_date", 100000)
    by_test: dict[str, list] = {}
    for lab in rows:
        if lab.get("value") is None or not lab.get("test_name"):
            continue
        by_test.setdefault(lab["test_name"], []).append(lab)
    flagged, trends = [], []
    for name, pts in by_test.items():
        pts.sort(key=lambda p: str(p.get("collected_date")))
        latest = pts[-1]
        fl = (latest.get("flag") or "").lower()
        if fl and fl not in ("normal", ""):
            flagged.append({"name": name, "value": latest["value"], "unit": latest.get("unit", ""),
                            "flag": fl, "category": latest.get("category", ""),
                            "date": latest.get("collected_date", "")})
        if len(pts) >= 3:
            first, last = pts[0]["value"], pts[-1]["value"]
            if first and abs(last - first) / abs(first) >= 0.15:
                trends.append({"name": name, "from": first, "to": last, "unit": latest.get("unit", ""),
                               "n": len(pts), "direction": "up" if last > first else "down"})
    return flagged[:45], trends[:25]


def _wearable_trends() -> dict[str, Any]:
    rows = db.query_entities("FitbitDaily", {"owner_email": OWNER_EMAIL, "source": "google_health"}, "-date", 60)

    def avg(field: str, sl: list):
        vals = [r[field] for r in sl if r.get(field) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    recent, prior = rows[:14], rows[14:42]
    out = {}
    for f in ("hrv", "resting_heart_rate", "sleep_minutes", "breathing_rate", "spo2_avg"):
        r = avg(f, recent)
        if r is not None:
            out[f] = {"recent": r, "prior": avg(f, prior)}
    return out


def _insights_snapshot() -> list:
    return [{"title": i.get("title"), "category": i.get("category"), "severity": i.get("severity")}
            for i in db.query_entities("Insight", {"owner_email": OWNER_EMAIL}, "-date_generated", 25)]


def _imaging_snapshot() -> list:
    return [{"date": r.get("record_date"), "summary": (r.get("summary") or "")[:700]}
            for r in db.query_entities("MedicalRecord", {"owner_email": OWNER_EMAIL}, "-record_date", 60)
            if r.get("doc_type") == "imaging_report" and r.get("summary")][:5]


def _build_context() -> dict[str, Any]:
    tz = ZoneInfo(config_value("app_timezone", APP_TIMEZONE))
    since = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    since_iso = since.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    glucose = report._glucose(tz, since_iso)
    cycle = report._cycle(tz, since_iso, glucose)
    flagged, trends = _labs_snapshot()
    prof = profile.get_profile()
    return {
        "window_days": WINDOW_DAYS,
        "conditions": conditions.get_conditions() or None,
        "medications": meds.get_medications() or None,
        "allergies": meds.get_allergies() or None,
        "profile": {k: prof.get(k) for k in ("age", "sex", "bmi", "weight_kg")} if prof.get("age") or prof.get("bmi") else None,
        "glucose": {k: glucose.get(k) for k in ("available", "tir", "avg", "gmi", "cv", "days")} if glucose.get("available") else None,
        "cycle": {"cycles": cycle.get("cycles_detected"), "avg_length": cycle.get("avg_cycle_length"),
                  "per_phase": cycle.get("per_phase")} if cycle.get("available") else None,
        "wearables": _wearable_trends(),
        "labs_out_of_range": flagged,
        "lab_trends": trends,
        "computed_correlations": _insights_snapshot(),
        "imaging": _imaging_snapshot(),
    }


async def generate() -> dict[str, Any]:
    context = _build_context()
    prompt = (
        "You are a health-data analyst writing the overall picture for a person with Type 1 diabetes who "
        "tracks a great deal of data. Using the multi-domain snapshot below, write a substantive, specific "
        "summary that spots INTERESTING CONNECTIONS and POTENTIAL INDICATORS across domains — where glucose, "
        "labs (blood/urine/neurotransmitter/gut/imaging), menstrual cycle, wearables (HRV, resting HR, sleep), "
        "and body profile line up or move together.\n\n"
        "Style:\n"
        "- State the facts and potential indicators plainly and directly. Cite the ACTUAL numbers every time.\n"
        "- Do NOT add hedging boilerplate like 'correlation is not causation' or 'this is not a diagnosis' — "
        "the reader already knows that. Just point out what the data shows.\n"
        "- Be specific and concrete, not vague. Name the analytes/metrics involved.\n"
        "- Mind recency: each lab carries a `date`. Weight recent results more; call out clearly "
        "when a notable value is old (say, 6+ months) rather than treating it as current.\n"
        "- Aim for 6-9 genuinely distinct observations, richest/most-actionable first. Also give a fuller "
        "'working' (on track) list and a 'watch' list.\n\n"
        f"DATA SNAPSHOT (last {WINDOW_DAYS} days where applicable):\n{json.dumps(context, indent=2, default=str)}"
    )
    result = await invoke_llm(prompt, response_json_schema=SUMMARY_SCHEMA, max_tokens=3000, tier="quality")

    # Compact metric strip for the page header (computed, not LLM-generated).
    g = context.get("glucose") or {}
    w = context.get("wearables") or {}
    p = context.get("profile") or {}
    metrics = {
        "tir": g.get("tir"), "avg": g.get("avg"), "gmi": g.get("gmi"), "cv": g.get("cv"),
        "hrv": (w.get("hrv") or {}).get("recent"),
        "resting_hr": (w.get("resting_heart_rate") or {}).get("recent"),
        "bmi": p.get("bmi"), "age": p.get("age"),
        "labs_out_of_range": len(context.get("labs_out_of_range") or []),
    }
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    payload = {"generated_at": now, "data": json.dumps({**(result or {}), "metrics": metrics}), "owner_email": OWNER_EMAIL}
    for old in db.query_entities("HealthSummary", {"owner_email": OWNER_EMAIL}):
        db.delete_entity("HealthSummary", old["id"])
    db.create_entity("HealthSummary", payload)
    set_config_value("health_summary_last_run", now)
    return {"generated_at": now, **(result or {})}


def _latest() -> dict[str, Any] | None:
    rows = db.query_entities("HealthSummary", {"owner_email": OWNER_EMAIL}, "-created_date", 1)
    if not rows:
        return None
    row = rows[0]
    try:
        data = json.loads(row.get("data") or "{}")
    except ValueError:
        data = {}
    return {"generated_at": row.get("generated_at"), **data}


async def handle(body: dict[str, Any]) -> dict[str, Any]:
    action = body.get("action", "get")
    if action == "generate":
        try:
            return {"ok": True, "summary": await generate()}
        except Exception as err:
            log.exception("health summary generation failed")
            return {"error": f"Summary generation failed: {err}", "_status": 502}
    return {"summary": _latest()}
