"""Cross-domain insights: glucose × sleep × readiness × activity × cycle.

Joins per-day glucose metrics (TIR, average, variability, lows) with Oura
daily data and cycle phases over the last 90 days, computes correlations and
group comparisons, and stores the meaningful ones as Insight entities with an
LLM-written narrative (best-effort).
"""

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .config import APP_TIMEZONE, OWNER_EMAIL
from .data_quality import assess_cgm, assess_daily, assess_pump_tdd, cgm_points
from .insulin_reconciliation import reconcile_treatments
from .db import config_value
from .llm import invoke_llm
from .repositories import get_repositories
from .unit_of_work import unit_of_work

WINDOW_DAYS = 90
MIN_PAIRS = 14
R_THRESHOLD = 0.3


def _clear_derived_insights() -> None:
    with unit_of_work() as work:
        work.repositories.entity("Insight").delete_where({"owner_email": OWNER_EMAIL})
        work.commit()


def _parse_ts(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _pearson(pairs: list[tuple[float, float]]) -> float | None:
    n = len(pairs)
    if n < MIN_PAIRS:
        return None
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in pairs)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return cov / (sx * sy)


def _daily_glucose_metrics(
    tz: ZoneInfo, since: datetime, now: datetime
) -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
    glucose_repository = get_repositories().glucose
    readings = []
    skip = 0
    while True:
        page = glucose_repository.query(
            {"owner_email": OWNER_EMAIL, "timestamp": {"$gte": since.isoformat().replace("+00:00", "Z")}},
            "timestamp",
            5000,
            skip,
        )
        readings.extend(page)
        if len(page) < 5000:
            break
        skip += 5000

    by_day: dict[str, list[float]] = {}
    for ts, value in cgm_points(readings, tz, start=since, end=now):
        by_day.setdefault(ts.astimezone(tz).date().isoformat(), []).append(value)

    metrics = {}
    for day, values in by_day.items():
        if len(values) < 100:  # skip partial days
            continue
        n = len(values)
        avg = sum(values) / n
        std = math.sqrt(sum((v - avg) ** 2 for v in values) / n)
        metrics[day] = {
            "tir": sum(1 for v in values if 70 <= v <= 180) / n * 100,
            "avg": avg,
            "cv": (std / avg * 100) if avg else 0,
            "lows": sum(1 for v in values if v < 70),
            "highs": sum(1 for v in values if v > 180),
        }
    quality = assess_cgm(
        readings, tz, start=since, end=now, as_of=now.astimezone(tz).date()
    )
    return metrics, quality


def _correlation_candidates(glucose: dict, oura_by_day: dict) -> list[dict[str, Any]]:
    axes = [
        ("sleep_score", "Sleep score (Oura)", "tir", "time in range", "time_in_range", True),
        ("sleep_score", "Sleep score (Oura)", "cv", "glucose variability", "variability", False),
        ("sleep_total_seconds", "Sleep duration (Oura)", "tir", "time in range", "time_in_range", True),
        ("readiness_score", "Readiness score (Oura)", "tir", "time in range", "time_in_range", True),
        ("readiness_hrv_balance", "HRV balance (Oura)", "cv", "glucose variability", "variability", False),
        # Nightly body-temp deviation tracks cycle phase; luteal insulin
        # resistance is a documented T1D phenomenon — this is the axis it shows on.
        ("readiness_temperature_deviation", "Body temperature deviation (Oura)", "avg", "average glucose", "general", False),
        ("readiness_temperature_deviation", "Body temperature deviation (Oura)", "tir", "time in range", "time_in_range", True),
        ("activity_steps", "Daily steps", "avg", "average glucose", "general", False),
        ("spo2_average", "Blood oxygen (SpO2)", "avg", "average glucose", "general", False),
        # Fitbit-sourced daily fields (merged in analyze())
        ("resting_heart_rate", "Resting heart rate (Fitbit)", "avg", "average glucose", "general", False),
        ("resting_heart_rate", "Resting heart rate (Fitbit)", "cv", "glucose variability", "variability", False),
        ("breathing_rate", "Breathing rate (Fitbit)", "cv", "glucose variability", "variability", False),
        ("skin_temp_deviation", "Skin temp deviation (Fitbit)", "avg", "average glucose", "general", False),
        ("sleep_efficiency_fitbit", "Sleep efficiency (Fitbit)", "tir", "time in range", "time_in_range", True),
        # Heart rate / HRV (Google Health via Fitbit). HRV rises with recovery,
        # so higher HRV is expected to line up with better control.
        ("hrv", "Heart rate variability (Fitbit)", "tir", "time in range", "time_in_range", True),
        ("hrv", "Heart rate variability (Fitbit)", "cv", "glucose variability", "variability", False),
        ("hrv", "Heart rate variability (Fitbit)", "avg", "average glucose", "general", False),
        ("nonrem_heart_rate", "Nightly heart rate (Fitbit)", "avg", "average glucose", "general", False),
        ("avg_heart_rate", "Average heart rate (Fitbit)", "avg", "average glucose", "general", False),
        ("avg_heart_rate", "Average heart rate (Fitbit)", "tir", "time in range", "time_in_range", True),
    ]
    out = []
    for oura_field, oura_label, g_field, g_label, category, positive_good in axes:
        pairs = []
        for day, g in glucose.items():
            o = oura_by_day.get(day, {})
            if o.get(oura_field) is not None:
                pairs.append((float(o[oura_field]), float(g[g_field])))
        r = _pearson(pairs)
        if r is None or abs(r) < R_THRESHOLD:
            continue
        favorable = (r > 0) == positive_good
        out.append(
            {
                "kind": "correlation",
                "category": category,
                "severity": "positive" if favorable else "warning",
                "x": oura_label,
                "y": g_label,
                "r": round(r, 2),
                "n": len(pairs),
            }
        )
    return out


def _group_mean(glucose: dict, days: set[str], field: str) -> float | None:
    vals = [glucose[d][field] for d in days if d in glucose]
    return sum(vals) / len(vals) if len(vals) >= 5 else None


async def analyze() -> dict[str, Any]:
    repositories = get_repositories()
    tz = ZoneInfo(config_value("app_timezone", APP_TIMEZONE))
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=WINDOW_DAYS)

    glucose, glucose_quality = _daily_glucose_metrics(tz, since, now)
    if not glucose_quality["ai_eligible"]:
        _clear_derived_insights()
        return {
            "insights": [],
            "message": "CGM coverage or freshness is below the insight-analysis threshold.",
            "quality": {"cgm": glucose_quality},
        }
    if len(glucose) < MIN_PAIRS:
        _clear_derived_insights()
        return {
            "insights": [],
            "message": f"Not enough full days of glucose data ({len(glucose)}).",
            "quality": {"cgm": glucose_quality},
        }

    oura_rows = repositories.oura_daily.query(
        {"owner_email": OWNER_EMAIL}, "-date", 400
    )
    oura_by_day = {r.get("date"): dict(r) for r in oura_rows if r.get("date")}

    # Merge Fitbit daily metrics into the wearable-by-day map. Oura fields win
    # where both exist; Fitbit contributes its unique fields and fills gaps.
    for f in repositories.fitbit_daily.query(
        {"owner_email": OWNER_EMAIL}, "-date", 400
    ):
        day = f.get("date")
        if not day:
            continue
        merged = oura_by_day.setdefault(day, {})
        for src_key, dst_key in (
            ("resting_heart_rate", "resting_heart_rate"),
            ("breathing_rate", "breathing_rate"),
            ("skin_temp_deviation", "skin_temp_deviation"),
            ("sleep_efficiency", "sleep_efficiency_fitbit"),
            ("steps", "activity_steps"),  # fallback if Oura steps absent
            ("spo2_avg", "spo2_average"),
            ("hrv", "hrv"),
            ("nonrem_heart_rate", "nonrem_heart_rate"),
        ):
            if f.get(src_key) is not None and merged.get(dst_key) is None:
                merged[dst_key] = f[src_key]

    # Daily average HR from the intraday minute-buckets (FitbitHeartRate). Only
    # covers recent/backfilled days, so it joins in once ≥MIN_PAIRS accrue.
    hr_by_day: dict[str, list[float]] = {}
    for hr in repositories.fitbit_heart_rate.query(
        {"owner_email": OWNER_EMAIL, "timestamp": {"$gte": since.isoformat().replace("+00:00", "Z")}},
        "timestamp",
        200000,
    ):
        ts = _parse_ts(hr.get("timestamp"))
        if ts is None or hr.get("bpm") is None:
            continue
        hr_by_day.setdefault(ts.astimezone(tz).date().isoformat(), []).append(float(hr["bpm"]))
    for day, bpms in hr_by_day.items():
        if len(bpms) >= 60:  # ≥1h of minute-buckets → a representative daily mean
            oura_by_day.setdefault(day, {}).setdefault("avg_heart_rate", round(sum(bpms) / len(bpms), 1))

    local_today = now.astimezone(tz).date()
    wearable_quality = assess_daily(
        "wearables",
        [{"date": day, **values} for day, values in oura_by_day.items()],
        tz,
        start_date=local_today - timedelta(days=WINDOW_DAYS - 1),
        end_date=local_today,
        as_of=local_today,
        required_fields=(
            "sleep_score", "readiness_score", "activity_steps", "resting_heart_rate",
            "breathing_rate", "hrv", "sleep_efficiency_fitbit", "avg_heart_rate",
        ),
    )

    period_rows = repositories.entity("PeriodLog").query(
        {"owner_email": OWNER_EMAIL}, "-date", 400
    )
    phase_days: dict[str, set[str]] = {}
    for p in period_rows:
        if p.get("date") and p.get("phase"):
            phase_days.setdefault(p["phase"], set()).add(p["date"])

    cycle_quality = assess_daily(
        "cycle", period_rows, tz,
        start_date=local_today - timedelta(days=WINDOW_DAYS - 1),
        end_date=local_today, as_of=local_today, required_fields=("phase",),
        limitations=("Cycle coverage reflects days with a recorded or inferred phase.",),
    )

    candidates = _correlation_candidates(glucose, oura_by_day) if wearable_quality["ai_eligible"] else []

    # Cycle phase comparisons (phases come from manual logs, Lively imports,
    # or the Oura-temperature inference — all land in PeriodLog)
    phase_tir = {ph: _group_mean(glucose, days, "tir") for ph, days in phase_days.items()}
    phase_tir = {ph: v for ph, v in phase_tir.items() if v is not None}
    if cycle_quality["ai_eligible"] and len(phase_tir) >= 2:
        best = max(phase_tir, key=phase_tir.get)
        worst = min(phase_tir, key=phase_tir.get)
        if phase_tir[best] - phase_tir[worst] >= 5:
            candidates.append(
                {
                    "kind": "cycle_comparison",
                    "category": "comparison",
                    "severity": "info",
                    "best_phase": best,
                    "best_tir": round(phase_tir[best], 1),
                    "worst_phase": worst,
                    "worst_tir": round(phase_tir[worst], 1),
                    "all_phases_tir": {ph: round(v, 1) for ph, v in phase_tir.items()},
                }
            )

    # Per-phase average glucose
    phase_avg = {ph: _group_mean(glucose, days, "avg") for ph, days in phase_days.items()}
    phase_avg = {ph: v for ph, v in phase_avg.items() if v is not None}
    if cycle_quality["ai_eligible"] and len(phase_avg) >= 2:
        hi = max(phase_avg, key=phase_avg.get)
        lo = min(phase_avg, key=phase_avg.get)
        if phase_avg[hi] - phase_avg[lo] >= 10:
            candidates.append(
                {
                    "kind": "cycle_glucose",
                    "category": "comparison",
                    "severity": "info",
                    "highest_phase": hi,
                    "highest_avg": round(phase_avg[hi]),
                    "lowest_phase": lo,
                    "lowest_avg": round(phase_avg[lo]),
                    "all_phases_avg": {ph: round(v) for ph, v in phase_avg.items()},
                }
            )

    # Per-phase complete daily insulin. Pump-reported totals are preferred;
    # calculations are used only when delivered basal coverage is complete.
    treatment_rows = repositories.treatments.query(
        {"owner_email": OWNER_EMAIL, "timestamp": {"$gte": since.isoformat().replace("+00:00", "Z")}},
        "timestamp", 100000,
    )
    reconciliation = reconcile_treatments(treatment_rows, tz)
    pump_quality = assess_pump_tdd(
        reconciliation, start_date=local_today - timedelta(days=WINDOW_DAYS - 1),
        end_date=local_today, as_of=local_today,
    )
    insulin_by_day: dict[str, float] = {}
    for reconciled_day in reconciliation["days"]:
        reported = reconciled_day["pump_reported"]["selected"]
        calculated = reconciled_day["calculated"]["total_units"]
        if reported is not None:
            insulin_by_day[reconciled_day["date"]] = float(reported["total_units"])
        elif calculated is not None:
            insulin_by_day[reconciled_day["date"]] = float(calculated)

    def _phase_insulin_mean(days: set[str]) -> float | None:
        vals = [insulin_by_day[d] for d in days if insulin_by_day.get(d)]
        return sum(vals) / len(vals) if len(vals) >= 5 else None

    phase_insulin = {ph: _phase_insulin_mean(days) for ph, days in phase_days.items()}
    phase_insulin = {ph: v for ph, v in phase_insulin.items() if v is not None}
    if cycle_quality["ai_eligible"] and pump_quality["ai_eligible"] and len(phase_insulin) >= 2:
        hi = max(phase_insulin, key=phase_insulin.get)
        lo = min(phase_insulin, key=phase_insulin.get)
        if phase_insulin[lo] > 0 and (phase_insulin[hi] - phase_insulin[lo]) / phase_insulin[lo] >= 0.10:
            candidates.append(
                {
                    "kind": "cycle_insulin",
                    "category": "comparison",
                    "severity": "info",
                    "highest_phase": hi,
                    "highest_units_per_day": round(phase_insulin[hi], 1),
                    "lowest_phase": lo,
                    "lowest_units_per_day": round(phase_insulin[lo], 1),
                    "pct_difference": round((phase_insulin[hi] - phase_insulin[lo]) / phase_insulin[lo] * 100),
                    "all_phases_units": {ph: round(v, 1) for ph, v in phase_insulin.items()},
                }
            )

    # Weekday vs weekend
    weekday_days = {d for d in glucose if datetime.fromisoformat(d).weekday() < 5}
    weekend_days = set(glucose) - weekday_days
    wd, we = _group_mean(glucose, weekday_days, "tir"), _group_mean(glucose, weekend_days, "tir")
    if wd is not None and we is not None and abs(wd - we) >= 5:
        candidates.append(
            {
                "kind": "weekday_weekend",
                "category": "comparison",
                "severity": "info",
                "weekday_tir": round(wd, 1),
                "weekend_tir": round(we, 1),
            }
        )

    # Trend: last 30 days vs prior 30 days
    days_sorted = sorted(glucose)
    if len(days_sorted) >= 40:
        recent, prior = days_sorted[-30:], days_sorted[-60:-30]
        r_tir = _group_mean(glucose, set(recent), "tir")
        p_tir = _group_mean(glucose, set(prior), "tir")
        if r_tir is not None and p_tir is not None and abs(r_tir - p_tir) >= 3:
            candidates.append(
                {
                    "kind": "trend",
                    "category": "time_in_range",
                    "severity": "positive" if r_tir > p_tir else "warning",
                    "recent_tir": round(r_tir, 1),
                    "prior_tir": round(p_tir, 1),
                }
            )

    if not candidates:
        _clear_derived_insights()
        return {
            "insights": [],
            "message": "No statistically notable cross-domain relationships found yet.",
            "quality": {
                "cgm": glucose_quality,
                "wearables": wearable_quality,
                "cycle": cycle_quality,
                "pump_tdd": pump_quality,
            },
        }

    # LLM narrative (best-effort)
    titles: dict[int, dict] = {}
    try:
        result = await invoke_llm(
            f"""You are a diabetes data analyst (NOT a doctor). For EACH detected relationship below, write:
- title: short, concrete (e.g. "Better sleep lines up with more time in range")
- description: 2-3 sentences: what the data shows, in plain language with the actual numbers, and a gentle educational note. Correlation is not causation; frame action ideas as "worth discussing with your healthcare team".

Detected relationships (r = Pearson correlation over n days; TIR = % time 70-180 mg/dL):
{json.dumps([{**c, 'index': i} for i, c in enumerate(candidates)], indent=2)}""",
            response_json_schema={
                "type": "object",
                "properties": {
                    "insights": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "index": {"type": "integer"},
                                "title": {"type": "string"},
                                "description": {"type": "string"},
                            },
                        },
                    }
                },
            },
        )
        for item in (result or {}).get("insights", []):
            if isinstance(item.get("index"), int):
                titles[item["index"]] = item
    except Exception:
        pass

    def fallback_title(c: dict) -> str:
        if c["kind"] == "correlation":
            direction = "higher" if c["r"] > 0 else "lower"
            return f"{c['x']} vs {c['y']}: r={c['r']} ({direction} together, {c['n']} days)"
        if c["kind"] == "cycle_comparison":
            return f"TIR differs by cycle phase: {c['best_phase']} {c['best_tir']}% vs {c['worst_phase']} {c['worst_tir']}%"
        if c["kind"] == "cycle_glucose":
            return f"Average glucose by phase: {c['highest_phase']} {c['highest_avg']} vs {c['lowest_phase']} {c['lowest_avg']} mg/dL"
        if c["kind"] == "cycle_insulin":
            return f"Insulin needs by phase: {c['highest_phase']} {c['highest_units_per_day']} vs {c['lowest_phase']} {c['lowest_units_per_day']} U/day (+{c['pct_difference']}%)"
        if c["kind"] == "weekday_weekend":
            return f"Weekday TIR {c['weekday_tir']}% vs weekend {c['weekend_tir']}%"
        return f"TIR trend: {c['recent_tir']}% last 30d vs {c['prior_tir']}% prior 30d"

    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def candidate_quality(candidate: dict[str, Any]) -> dict[str, Any]:
        quality = {"cgm": glucose_quality}
        if candidate["kind"] == "correlation":
            quality["wearables"] = wearable_quality
        elif candidate["kind"] in {"cycle_comparison", "cycle_glucose"}:
            quality["cycle"] = cycle_quality
        elif candidate["kind"] == "cycle_insulin":
            quality.update({"cycle": cycle_quality, "pump_tdd": pump_quality})
        return quality

    to_create = [
        {
            "title": titles.get(i, {}).get("title") or fallback_title(c),
            "description": titles.get(i, {}).get("description") or "",
            "category": c["category"],
            "severity": c["severity"],
            "date_generated": now,
            "supporting_data": json.dumps(c),
            "data_quality": candidate_quality(c),
            "is_read": False,
            "owner_email": OWNER_EMAIL,
        }
        for i, c in enumerate(candidates)
    ]
    # Replace the derived set atomically so readers never observe a partial set.
    with unit_of_work() as work:
        insight_repository = work.repositories.entity("Insight")
        insight_repository.delete_where({"owner_email": OWNER_EMAIL})
        if to_create:
            insight_repository.create_many(to_create)
        work.commit()
    return {
        "success": True,
        "insightsFound": len(to_create),
        "insights": [{"title": t["title"], "severity": t["severity"]} for t in to_create],
        "quality": {
            "cgm": glucose_quality,
            "wearables": wearable_quality,
            "cycle": cycle_quality,
            "pump_tdd": pump_quality,
        },
    }
