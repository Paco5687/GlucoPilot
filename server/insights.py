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

from .analytics_confidence import (
    comparison_confidence,
    correlation_confidence,
    phase_provenance,
    safe_analytics_text,
)
from .claims import (
    CLAIM_CONTRACT_VERSION,
    claim_limitations,
    evidence_input_version,
    semantic_claim_key,
)
from .config import APP_TIMEZONE, OWNER_EMAIL
from .data_quality import assess_cgm, assess_daily, assess_pump_tdd, cgm_points
from .insulin_reconciliation import reconcile_treatments
from .db import config_value
from .llm import invoke_llm
from .evidence_sets import evidence_set_writes_enabled
from .repositories import get_repositories
from .unit_of_work import unit_of_work

WINDOW_DAYS = 90
MIN_ANALYSIS_DAYS = 14
MIN_CORRELATION_PAIRS = 7
R_THRESHOLD = 0.3


def _clear_derived_insights() -> None:
    with unit_of_work() as work:
        repository = work.repositories.entity("Insight")
        for insight in repository.query({"owner_email": OWNER_EMAIL}):
            if insight.get("is_active", True):
                repository.update(insight["id"], {
                    "is_active": False,
                    "assertion_status": "superseded",
                })
        if evidence_set_writes_enabled():
            work.repositories.typed_claims.supersede_except(
                owner_email=OWNER_EMAIL,
                claim_type="Insight",
                current_claim_version_ids=[],
            )
        work.commit()


def _parse_ts(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _daily_glucose_metrics(
    tz: ZoneInfo, since: datetime, now: datetime
) -> tuple[dict[str, dict[str, float]], dict[str, Any], list[dict[str, Any]]]:
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
    return metrics, quality, readings


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
        pairs: list[tuple[str, float, float]] = []
        source_types: set[str] = set()
        for day, g in glucose.items():
            o = oura_by_day.get(day, {})
            if o.get(oura_field) is not None:
                pairs.append((day, float(o[oura_field]), float(g[g_field])))
                field_source = (o.get("_field_sources") or {}).get(oura_field)
                if field_source:
                    source_types.add(field_source)
        if not source_types:
            source_types.add(
                "FitbitHeartRate"
                if oura_field == "avg_heart_rate"
                else "FitbitDaily"
                if "Fitbit" in oura_label
                else "OuraDaily"
            )
        r, confidence = correlation_confidence(
            pairs,
            expected_days=WINDOW_DAYS,
            temporal_direction="same-day contemporaneous association",
            effect_threshold=R_THRESHOLD,
        )
        if len(pairs) < MIN_CORRELATION_PAIRS or r is None or abs(r) < R_THRESHOLD:
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
                "analytics_confidence": confidence,
                "_evidence_types": ["GlucoseReading", *sorted(source_types)],
            }
        )
    return out


def _group_mean(glucose: dict, days: set[str], field: str) -> float | None:
    vals = _group_values(glucose, days, field)
    return sum(vals) / len(vals) if len(vals) >= 5 else None


def _group_values(glucose: dict, days: set[str], field: str) -> list[float]:
    return [float(glucose[d][field]) for d in days if d in glucose]


async def analyze() -> dict[str, Any]:
    repositories = get_repositories()
    tz = ZoneInfo(config_value("app_timezone", APP_TIMEZONE))
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=WINDOW_DAYS)

    glucose, glucose_quality, glucose_rows = _daily_glucose_metrics(tz, since, now)
    if not glucose_quality["ai_eligible"]:
        _clear_derived_insights()
        return {
            "insights": [],
            "message": "CGM coverage or freshness is below the insight-analysis threshold.",
            "quality": {"cgm": glucose_quality},
        }
    if len(glucose) < MIN_ANALYSIS_DAYS:
        _clear_derived_insights()
        return {
            "insights": [],
            "message": f"Not enough full days of glucose data ({len(glucose)}).",
            "quality": {"cgm": glucose_quality},
        }

    oura_rows = repositories.oura_daily.query(
        {"owner_email": OWNER_EMAIL}, "-date", 400
    )
    oura_by_day = {
        r.get("date"): {
            **dict(r),
            "_field_sources": {
                key: "OuraDaily"
                for key, value in r.items()
                if value is not None and key not in {"id", "owner_email", "date"}
            },
        }
        for r in oura_rows
        if r.get("date")
    }

    # Merge Fitbit daily metrics into the wearable-by-day map. Oura fields win
    # where both exist; Fitbit contributes its unique fields and fills gaps.
    fitbit_rows = repositories.fitbit_daily.query(
        {"owner_email": OWNER_EMAIL}, "-date", 400
    )
    for f in fitbit_rows:
        day = f.get("date")
        if not day:
            continue
        merged = oura_by_day.setdefault(day, {"_field_sources": {}})
        field_sources = merged.setdefault("_field_sources", {})
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
                field_sources[dst_key] = "FitbitDaily"

    # Daily average HR from the intraday minute-buckets (FitbitHeartRate). Only
    # covers recent/backfilled days, so it joins once the shared confidence
    # framework has enough pairs to label the result exploratory.
    hr_by_day: dict[str, list[float]] = {}
    fitbit_hr_rows = repositories.fitbit_heart_rate.query(
        {"owner_email": OWNER_EMAIL, "timestamp": {"$gte": since.isoformat().replace("+00:00", "Z")}},
        "timestamp",
        200000,
    )
    for hr in fitbit_hr_rows:
        ts = _parse_ts(hr.get("timestamp"))
        if ts is None or hr.get("bpm") is None:
            continue
        hr_by_day.setdefault(ts.astimezone(tz).date().isoformat(), []).append(float(hr["bpm"]))
    for day, bpms in hr_by_day.items():
        if len(bpms) >= 60:  # ≥1h of minute-buckets → a representative daily mean
            merged = oura_by_day.setdefault(day, {"_field_sources": {}})
            if merged.get("avg_heart_rate") is None:
                merged["avg_heart_rate"] = round(sum(bpms) / len(bpms), 1)
                merged.setdefault("_field_sources", {})["avg_heart_rate"] = "FitbitHeartRate"

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
    cycle_provenance = phase_provenance(period_rows)

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
            best_values = _group_values(glucose, phase_days[best], "tir")
            worst_values = _group_values(glucose, phase_days[worst], "tir")
            included = (phase_days[best] | phase_days[worst]) & set(glucose)
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
                    "analytics_confidence": comparison_confidence(
                        best_values,
                        worst_values,
                        valid_days=len(included),
                        expected_days=WINDOW_DAYS,
                        unit="percentage_points",
                        phase_provenance=phase_provenance(
                            period_rows, included_dates=included
                        ),
                    ),
                    "_evidence_types": ["GlucoseReading", "PeriodLog"],
                }
            )

    # Per-phase average glucose
    phase_avg = {ph: _group_mean(glucose, days, "avg") for ph, days in phase_days.items()}
    phase_avg = {ph: v for ph, v in phase_avg.items() if v is not None}
    if cycle_quality["ai_eligible"] and len(phase_avg) >= 2:
        hi = max(phase_avg, key=phase_avg.get)
        lo = min(phase_avg, key=phase_avg.get)
        if phase_avg[hi] - phase_avg[lo] >= 10:
            high_values = _group_values(glucose, phase_days[hi], "avg")
            low_values = _group_values(glucose, phase_days[lo], "avg")
            included = (phase_days[hi] | phase_days[lo]) & set(glucose)
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
                    "analytics_confidence": comparison_confidence(
                        high_values,
                        low_values,
                        valid_days=len(included),
                        expected_days=WINDOW_DAYS,
                        unit="mg/dL",
                        phase_provenance=phase_provenance(
                            period_rows, included_dates=included
                        ),
                    ),
                    "_evidence_types": ["GlucoseReading", "PeriodLog"],
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
        vals = [insulin_by_day[d] for d in days if insulin_by_day.get(d) is not None]
        return sum(vals) / len(vals) if len(vals) >= 5 else None

    phase_insulin = {ph: _phase_insulin_mean(days) for ph, days in phase_days.items()}
    phase_insulin = {ph: v for ph, v in phase_insulin.items() if v is not None}
    if cycle_quality["ai_eligible"] and pump_quality["ai_eligible"] and len(phase_insulin) >= 2:
        hi = max(phase_insulin, key=phase_insulin.get)
        lo = min(phase_insulin, key=phase_insulin.get)
        if phase_insulin[lo] > 0 and (phase_insulin[hi] - phase_insulin[lo]) / phase_insulin[lo] >= 0.10:
            high_values = [insulin_by_day[d] for d in phase_days[hi] if d in insulin_by_day]
            low_values = [insulin_by_day[d] for d in phase_days[lo] if d in insulin_by_day]
            included = (phase_days[hi] | phase_days[lo]) & set(insulin_by_day)
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
                    "analytics_confidence": comparison_confidence(
                        high_values,
                        low_values,
                        valid_days=len(included),
                        expected_days=WINDOW_DAYS,
                        unit="units/day",
                        phase_provenance=phase_provenance(
                            period_rows, included_dates=included
                        ),
                    ),
                    "_evidence_types": ["GlucoseReading", "PeriodLog", "Treatment"],
                }
            )

    # Weekday vs weekend
    weekday_days = {d for d in glucose if datetime.fromisoformat(d).weekday() < 5}
    weekend_days = set(glucose) - weekday_days
    wd, we = _group_mean(glucose, weekday_days, "tir"), _group_mean(glucose, weekend_days, "tir")
    if wd is not None and we is not None and abs(wd - we) >= 5:
        weekday_values = _group_values(glucose, weekday_days, "tir")
        weekend_values = _group_values(glucose, weekend_days, "tir")
        candidates.append(
            {
                "kind": "weekday_weekend",
                "category": "comparison",
                "severity": "info",
                "weekday_tir": round(wd, 1),
                "weekend_tir": round(we, 1),
                "analytics_confidence": comparison_confidence(
                    weekday_values,
                    weekend_values,
                    valid_days=len(weekday_days | weekend_days),
                    expected_days=WINDOW_DAYS,
                    unit="percentage_points",
                ),
                "_evidence_types": ["GlucoseReading"],
            }
        )

    # Trend: last 30 days vs prior 30 days
    days_sorted = sorted(glucose)
    if len(days_sorted) >= 40:
        recent, prior = days_sorted[-30:], days_sorted[-60:-30]
        r_tir = _group_mean(glucose, set(recent), "tir")
        p_tir = _group_mean(glucose, set(prior), "tir")
        if r_tir is not None and p_tir is not None and abs(r_tir - p_tir) >= 3:
            recent_values = _group_values(glucose, set(recent), "tir")
            prior_values = _group_values(glucose, set(prior), "tir")
            candidates.append(
                {
                    "kind": "trend",
                    "category": "time_in_range",
                    "severity": "positive" if r_tir > p_tir else "warning",
                    "recent_tir": round(r_tir, 1),
                    "prior_tir": round(p_tir, 1),
                    "analytics_confidence": comparison_confidence(
                        recent_values,
                        prior_values,
                        valid_days=len(set(recent) | set(prior)),
                        expected_days=60,
                        temporal_direction="prior-period to recent-period",
                        unit="percentage_points",
                    ),
                    "_evidence_types": ["GlucoseReading"],
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

Each relationship includes governed numerical confidence metadata and a required
language lead. Preserve its discovery status, do not use definitive/causal
language, and distinguish confirmed from inferred cycle phase days.

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

    generated_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def candidate_quality(candidate: dict[str, Any]) -> dict[str, Any]:
        quality = {"cgm": glucose_quality}
        if candidate["kind"] == "correlation":
            quality["wearables"] = wearable_quality
        elif candidate["kind"] in {"cycle_comparison", "cycle_glucose"}:
            quality["cycle"] = cycle_quality
        elif candidate["kind"] == "cycle_insulin":
            quality.update({"cycle": cycle_quality, "pump_tdd": pump_quality})
        return quality

    candidate_evidence_types = [candidate["_evidence_types"] for candidate in candidates]
    to_create = [
        {
            "title": titles.get(i, {}).get("title") or fallback_title(c),
            "description": safe_analytics_text(
                titles.get(i, {}).get("description"),
                c["analytics_confidence"],
                fallback_title(c),
            ),
            "category": c["category"],
            "severity": c["severity"],
            "date_generated": generated_at,
            "supporting_data": json.dumps({
                key: value for key, value in c.items() if not key.startswith("_")
            }),
            "analytics_confidence": c["analytics_confidence"],
            "data_quality": candidate_quality(c),
            "is_read": False,
            "is_active": True,
            "owner_email": OWNER_EMAIL,
        }
        for i, c in enumerate(candidates)
    ]
    # Publish a new immutable generation. Previous rows remain auditable and are
    # marked superseded instead of being deleted.
    with unit_of_work() as work:
        insight_repository = work.repositories.entity("Insight")
        old_insights = [
            insight
            for insight in insight_repository.query({"owner_email": OWNER_EMAIL})
            if insight.get("is_active", True)
        ]
        for old in old_insights:
            insight_repository.update(old["id"], {"is_active": False})
        created = insight_repository.create_many(to_create) if to_create else []
        if created and evidence_set_writes_enabled():
            observations_by_type = {
                "GlucoseReading": ("timestamp", "instant", glucose_rows, {"data_quality": glucose_quality}),
                "OuraDaily": ("date", "date", oura_rows, {"data_quality": wearable_quality}),
                "FitbitDaily": ("date", "date", fitbit_rows, {"data_quality": wearable_quality}),
                "FitbitHeartRate": (
                    "timestamp", "instant", fitbit_hr_rows, {"data_quality": wearable_quality}
                ),
                "PeriodLog": ("date", "date", period_rows, {"data_quality": cycle_quality}),
                "Treatment": ("timestamp", "instant", treatment_rows, {"data_quality": pump_quality}),
            }
            required_types = {
                entity_type
                for entity_types in candidate_evidence_types
                for entity_type in entity_types
            }
            windows_by_type: dict[str, list[dict[str, Any]]] = {}
            for entity_type in sorted(required_types):
                time_field, time_kind, observations, summary = observations_by_type[entity_type]
                if not observations:
                    continue
                ranges = [(since, now)]
                if entity_type == "FitbitHeartRate":
                    ranges = []
                    cursor = since
                    while cursor <= now:
                        chunk_end = min(now, cursor + timedelta(days=30) - timedelta(milliseconds=1))
                        ranges.append((cursor, chunk_end))
                        cursor = chunk_end + timedelta(milliseconds=1)
                for window_start, window_end in ranges:
                    if time_kind == "date":
                        has_observation = any(
                            window_start.date().isoformat() <= str(item.get(time_field) or "")
                            <= window_end.date().isoformat()
                            for item in observations
                        )
                    else:
                        has_observation = any(
                            (observed := _parse_ts(item.get(time_field))) is not None
                            and window_start <= observed <= window_end
                            for item in observations
                        )
                    if not has_observation:
                        continue
                    window = work.repositories.typed_evidence.capture_window(
                        owner_email=OWNER_EMAIL,
                        entity_type=entity_type,
                        time_field=time_field,
                        time_kind=time_kind,
                        window_start=window_start.isoformat().replace("+00:00", "Z"),
                        window_end=window_end.isoformat().replace("+00:00", "Z"),
                        observations=observations,
                        filters={"owner_email": OWNER_EMAIL},
                        summary=summary,
                    )
                    windows_by_type.setdefault(entity_type, []).append(window)

            current_versions = []
            retired_entity_ids: set[str] = set()
            for insight, source, evidence_types in zip(
                created, to_create, candidate_evidence_types
            ):
                windows = [
                    window
                    for entity_type in evidence_types
                    for window in windows_by_type.get(entity_type, [])
                ]
                if not windows:
                    raise RuntimeError("an evidence-backed Insight requires source observations")
                input_data_version = evidence_input_version(windows)
                claim_key = semantic_claim_key("Insight", source)
                claim_version, predecessors = work.repositories.typed_claims.create_version(
                    owner_email=OWNER_EMAIL,
                    claim_type="Insight",
                    claim_entity_id=insight["id"],
                    claim_key=claim_key,
                    content=source,
                    input_data_version=input_data_version,
                    analytics_confidence=source["analytics_confidence"],
                )
                evidence = work.repositories.typed_evidence.create_set(
                    owner_email=OWNER_EMAIL,
                    claim_type="Insight",
                    claim_id=insight["id"],
                    window_ids=[window["id"] for window in windows],
                    summary={
                        **json.loads(source["supporting_data"]),
                        "analytics_confidence": source["analytics_confidence"],
                    },
                    input_data_version=input_data_version,
                    window_rationales={
                        window["id"]: f"{window['entity_type']} observations used by this analysis."
                        for window in windows
                    },
                    limitations=claim_limitations(
                        source["analytics_confidence"], source["data_quality"]
                    ),
                )
                work.repositories.typed_claims.attach_evidence(claim_version["id"], evidence["id"])
                insight_repository.update(insight["id"], {
                    "claim_contract_version": CLAIM_CONTRACT_VERSION,
                    "claim_version_id": claim_version["id"],
                    "claim_key": claim_key,
                    "claim_version": claim_version["version_number"],
                    "assertion_kind": claim_version["assertion_kind"],
                    "assertion_status": "provisional",
                    "algorithm_id": claim_version["algorithm_id"],
                    "algorithm_version": claim_version["algorithm_version"],
                    "input_data_version": input_data_version,
                    "evidence_set_id": evidence["id"],
                    "supersedes_claim_id": predecessors[0] if predecessors else None,
                })
                current_versions.append(claim_version["id"])
                retired_entity_ids.update(predecessors)
                for predecessor in predecessors:
                    insight_repository.update(predecessor, {
                        "is_active": False,
                        "assertion_status": "superseded",
                        "superseded_by_claim_id": insight["id"],
                    })
            retired_entity_ids.update(work.repositories.typed_claims.supersede_except(
                owner_email=OWNER_EMAIL,
                claim_type="Insight",
                current_claim_version_ids=current_versions,
            ))
            for retired_id in retired_entity_ids:
                insight_repository.update(retired_id, {
                    "is_active": False,
                    "assertion_status": "superseded",
                })
        elif evidence_set_writes_enabled():
            retired = work.repositories.typed_claims.supersede_except(
                owner_email=OWNER_EMAIL,
                claim_type="Insight",
                current_claim_version_ids=[],
            )
            for retired_id in retired:
                insight_repository.update(retired_id, {
                    "is_active": False,
                    "assertion_status": "superseded",
                })
        work.commit()
    return {
        "success": True,
        "insightsFound": len(to_create),
        "insights": [
            {
                "title": t["title"],
                "severity": t["severity"],
                "discovery_status": t["analytics_confidence"]["discovery_status"],
            }
            for t in to_create
        ],
        "quality": {
            "cgm": glucose_quality,
            "wearables": wearable_quality,
            "cycle": cycle_quality,
            "pump_tdd": pump_quality,
        },
        "cycle_phase_provenance": cycle_provenance,
    }
