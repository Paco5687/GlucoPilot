"""Glucose pattern detection — port of base44/functions/analyzePatterns/entry.ts."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import db
from .config import APP_TIMEZONE, OWNER_EMAIL
from .db import config_value
from .llm import invoke_llm


def _parse_ts(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _fetch_window(etype: str, since: datetime) -> list[dict[str, Any]]:
    results = []
    skip = 0
    while True:
        page = db.query_entities(etype, {"owner_email": OWNER_EMAIL}, "-timestamp", 500, skip)
        if not page:
            break
        for record in page:
            ts = _parse_ts(record.get("timestamp"))
            if ts and ts >= since:
                results.append({**record, "_time": ts})
        last_ts = _parse_ts(page[-1].get("timestamp"))
        if last_ts and last_ts < since:
            break
        if len(page) < 500:
            break
        skip += 500
    results.sort(key=lambda r: r["_time"])
    return results


def _group_consecutive(hours: list[dict]) -> list[list[dict]]:
    if not hours:
        return []
    groups = [[hours[0]]]
    for item in hours[1:]:
        if item["hour"] - groups[-1][-1]["hour"] <= 2:
            groups[-1].append(item)
        else:
            groups.append([item])
    return groups


def _time_of_day(hour: int) -> str:
    if hour < 6:
        return "overnight"
    if hour < 12:
        return "morning"
    if hour < 18:
        return "afternoon"
    return "evening"


async def analyze() -> dict[str, Any]:
    tz = ZoneInfo(config_value("app_timezone", APP_TIMEZONE))
    since = datetime.now(timezone.utc) - timedelta(days=14)

    readings = _fetch_window("GlucoseReading", since)
    if len(readings) < 50:
        return {"patterns": [], "message": "Not enough data for pattern analysis (need at least 50 readings)"}
    treatments = _fetch_window("Treatment", since)

    def local(record):
        return record["_time"].astimezone(tz)

    patterns: list[dict[str, Any]] = []

    # Rules 1 & 2: recurring highs/lows by hour of day
    hour_buckets: dict[int, list[float]] = {}
    for r in readings:
        hour_buckets.setdefault(local(r).hour, []).append(r["value"])

    high_hours, low_hours = [], []
    for h in range(24):
        vals = hour_buckets.get(h, [])
        if len(vals) < 10:
            continue
        high_pct = sum(1 for v in vals if v > 180) / len(vals)
        low_pct = sum(1 for v in vals if v < 70) / len(vals)
        avg = round(sum(vals) / len(vals))
        if high_pct > 0.5:
            high_hours.append({"hour": h, "avg": avg, "pct": high_pct, "count": sum(1 for v in vals if v > 180)})
        if low_pct > 0.15:
            low_hours.append({"hour": h, "avg": avg, "pct": low_pct, "count": sum(1 for v in vals if v < 70)})

    for group in _group_consecutive(high_hours):
        avg_pct = sum(g["pct"] for g in group) / len(group)
        patterns.append(
            {
                "pattern_type": "recurring_high",
                "time_of_day": _time_of_day(group[0]["hour"]),
                "confidence": "high" if avg_pct > 0.65 else "medium",
                "occurrences": sum(g["count"] for g in group),
                "supporting_evidence": json.dumps(
                    {
                        "fromHour": group[0]["hour"],
                        "toHour": group[-1]["hour"],
                        "avgGlucose": round(sum(g["avg"] for g in group) / len(group)),
                        "highPct": round(avg_pct * 100),
                        "hours": [g["hour"] for g in group],
                    }
                ),
            }
        )

    for group in _group_consecutive(low_hours):
        avg_pct = sum(g["pct"] for g in group) / len(group)
        patterns.append(
            {
                "pattern_type": "recurring_low",
                "time_of_day": _time_of_day(group[0]["hour"]),
                "confidence": "high" if avg_pct > 0.25 else "medium",
                "occurrences": sum(g["count"] for g in group),
                "supporting_evidence": json.dumps(
                    {
                        "fromHour": group[0]["hour"],
                        "toHour": group[-1]["hour"],
                        "lowPct": round(avg_pct * 100),
                        "hours": [g["hour"] for g in group],
                    }
                ),
            }
        )

    # Rule 3: post-meal spikes
    carb_events = [t for t in treatments if t.get("type") == "carb" and t.get("amount")]
    spikes = []
    for carb in carb_events:
        ct = carb["_time"]
        pre = [r for r in readings if ct - timedelta(minutes=15) <= r["_time"] <= ct + timedelta(minutes=5)]
        post = [r for r in readings if ct + timedelta(minutes=30) < r["_time"] <= ct + timedelta(minutes=150)]
        if pre and post:
            pre_avg = sum(r["value"] for r in pre) / len(pre)
            post_max = max(r["value"] for r in post)
            if post_max - pre_avg > 60:
                spikes.append(
                    {
                        "carbAmount": carb["amount"],
                        "preAvg": round(pre_avg),
                        "postMax": round(post_max),
                        "spike": round(post_max - pre_avg),
                        "timestamp": carb.get("timestamp"),
                    }
                )
    if len(spikes) >= 3:
        patterns.append(
            {
                "pattern_type": "post_meal_spike",
                "time_of_day": "all_day",
                "confidence": "high" if len(spikes) >= 5 else "medium",
                "occurrences": len(spikes),
                "supporting_evidence": json.dumps(
                    {"avgSpike": round(sum(s["spike"] for s in spikes) / len(spikes)), "samples": spikes[:5]}
                ),
            }
        )

    # Rules 4 & 5 share a per-local-day map
    day_map: dict[str, list[dict]] = {}
    for r in readings:
        loc = local(r)
        day_map.setdefault(loc.date().isoformat(), []).append(
            {"hour": loc.hour, "value": r["value"], "time": r["_time"]}
        )

    # Rule 4: dawn phenomenon
    dawn_count = dawn_days = 0
    for day_readings in day_map.values():
        around_3am = [r for r in day_readings if 2 <= r["hour"] <= 4]
        around_7am = [r for r in day_readings if 6 <= r["hour"] <= 8]
        if len(around_3am) >= 2 and len(around_7am) >= 2:
            dawn_days += 1
            nadir = min(r["value"] for r in around_3am)
            peak = max(r["value"] for r in around_7am)
            if peak - nadir > 30 and peak > 140:
                dawn_count += 1
    if dawn_count >= 3 and dawn_days >= 5:
        patterns.append(
            {
                "pattern_type": "dawn_phenomenon",
                "time_of_day": "morning",
                "confidence": "high" if dawn_count / dawn_days > 0.6 else "medium",
                "occurrences": dawn_count,
                "supporting_evidence": json.dumps(
                    {"dawnDays": dawn_count, "totalDays": dawn_days, "pct": round(dawn_count / dawn_days * 100)}
                ),
            }
        )

    # Rule 5: overnight drift
    drift_up = drift_down = night_days = 0
    for day_readings in day_map.values():
        night = sorted(
            (r for r in day_readings if r["hour"] >= 22 or r["hour"] <= 6), key=lambda r: r["time"]
        )
        if len(night) < 6:
            continue
        night_days += 1
        early = sum(r["value"] for r in night[:3]) / 3
        late = sum(r["value"] for r in night[-3:]) / 3
        if late - early > 30:
            drift_up += 1
        if late - early < -30:
            drift_down += 1
    for count, direction in ((drift_up, "rising"), (drift_down, "falling")):
        if count >= 3 and night_days >= 5:
            patterns.append(
                {
                    "pattern_type": "overnight_drift",
                    "time_of_day": "overnight",
                    "confidence": "high" if count / night_days > 0.5 else "medium",
                    "occurrences": count,
                    "supporting_evidence": json.dumps(
                        {"direction": direction, "nights": count, "totalNights": night_days}
                    ),
                }
            )

    # Rule 6: ineffective corrections
    insulin_events = [
        t for t in treatments if t.get("type") == "insulin" and t.get("amount") and t.get("event_type") != "Daily Total"
    ]
    ineffective = []
    for ins in insulin_events:
        it = ins["_time"]
        at_dose = [r for r in readings if it - timedelta(minutes=10) <= r["_time"] <= it + timedelta(minutes=10)]
        if not at_dose or at_dose[0]["value"] < 180:
            continue
        after = [r for r in readings if it + timedelta(minutes=90) < r["_time"] <= it + timedelta(minutes=240)]
        if after:
            after_avg = sum(r["value"] for r in after) / len(after)
            if after_avg > 170:
                ineffective.append(
                    {
                        "dose": ins["amount"],
                        "glucoseAtDose": at_dose[0]["value"],
                        "glucoseAfter": round(after_avg),
                        "timestamp": ins.get("timestamp"),
                    }
                )
    if len(ineffective) >= 3:
        patterns.append(
            {
                "pattern_type": "ineffective_correction",
                "time_of_day": "all_day",
                "confidence": "high" if len(ineffective) >= 5 else "medium",
                "occurrences": len(ineffective),
                "supporting_evidence": json.dumps({"samples": ineffective[:5]}),
            }
        )

    # AI enrichment: titles + educational explanations
    if patterns:
        values = [r["value"] for r in readings]
        avg = round(sum(values) / len(values))
        in_range = round(sum(1 for v in values if 70 <= v <= 180) / len(values) * 100)
        patterns_for_ai = [
            {
                "index": i,
                "type": p["pattern_type"],
                "time_of_day": p["time_of_day"],
                "confidence": p["confidence"],
                "occurrences": p["occurrences"],
                "evidence": p["supporting_evidence"],
            }
            for i, p in enumerate(patterns)
        ]
        try:
            ai_result = await invoke_llm(
                f"""You are a diabetes data analyst (NOT a doctor). Analyze these detected glucose patterns and provide clear, educational titles and explanations.

Context: {len(readings)} CGM readings over 14 days, average {avg} mg/dL, {in_range}% TIR.

Detected patterns:
{json.dumps(patterns_for_ai, indent=2)}

For EACH pattern, generate:
- title: A clear, concise title (e.g. "Afternoon Highs Recurring", "Post-Meal Spikes Above Target")
- explanation: 2-3 sentences explaining what was detected, the significance, and what it could suggest to discuss with their healthcare provider. Be educational, not prescriptive.

IMPORTANT: This is educational only. Always frame suggestions as "discuss with your healthcare team".""",
                response_json_schema={
                    "type": "object",
                    "properties": {
                        "patterns": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "index": {"type": "integer"},
                                    "title": {"type": "string"},
                                    "explanation": {"type": "string"},
                                },
                            },
                        }
                    },
                },
            )
            for ai in (ai_result or {}).get("patterns", []):
                idx = ai.get("index")
                if isinstance(idx, int) and 0 <= idx < len(patterns):
                    patterns[idx]["title"] = ai.get("title")
                    patterns[idx]["explanation"] = ai.get("explanation")
        except Exception:
            # LLM enrichment is best-effort; statistical patterns still save.
            pass

    # Deactivate old active patterns, insert new
    for old in db.query_entities("Pattern", {"is_active": True, "owner_email": OWNER_EMAIL}):
        db.update_entity("Pattern", old["id"], {"is_active": False})

    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    to_create = [
        {
            "title": p.get("title") or f"{p['pattern_type'].replace('_', ' ')} detected",
            "explanation": p.get("explanation") or "",
            "pattern_type": p["pattern_type"],
            "confidence": p["confidence"],
            "time_of_day": p["time_of_day"],
            "supporting_evidence": p["supporting_evidence"],
            "occurrences": p["occurrences"],
            "first_detected": now,
            "last_detected": now,
            "is_active": True,
            "is_dismissed": False,
            "owner_email": OWNER_EMAIL,
        }
        for p in patterns
    ]
    if to_create:
        db.bulk_create_entities("Pattern", to_create)

    return {
        "success": True,
        "patternsFound": len(to_create),
        "patterns": [{"title": p["title"], "type": p["pattern_type"], "confidence": p["confidence"]} for p in to_create],
    }
