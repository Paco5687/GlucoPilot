"""Seed a demo instance with realistic-but-synthetic data (no real PHI).

Populates ~90 days of glucose, treatments, Oura + intraday HR, Fitbit, cycle
phases, labs, patterns, insights, and a sample AI chat so every page looks
alive for screenshots / a public demo. Deterministic (seeded RNG).

    python -m server.seed_demo            # seed only if empty
    python -m server.seed_demo --force    # wipe demo data and reseed
"""

import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone

from . import db
from .config import OWNER_EMAIL
from .readings import persist_readings_deduped
from .repositories import get_repositories

RNG = random.Random(20260718)
NOW = datetime(2026, 7, 18, 20, 0, tzinfo=timezone.utc)
DAYS = 90
CYCLE_LEN = 28  # days, for the temperature/phase model

SEED_TYPES = (
    "GlucoseReading", "Treatment", "OuraDaily", "OuraHeartRate", "FitbitDaily",
    "PeriodLog", "MedicalRecord", "LabResult", "Pattern", "Insight", "AIConversation",
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _cycle_day(dt: datetime) -> int:
    # Day 0 = a menses onset; anchored so "today" lands mid-cycle.
    return int((dt - (NOW - timedelta(days=6))).total_seconds() // 86400) % CYCLE_LEN


def _luteal_offset(dt: datetime) -> float:
    # Mild insulin-resistance bump in the luteal phase (days ~15-27).
    d = _cycle_day(dt)
    return 12.0 if 15 <= d <= 27 else 0.0


def _glucose_at(dt: datetime, meals: list[tuple[float, float]]) -> float:
    """A tractable glucose model: baseline + dawn + decaying meal spikes + noise."""
    hour = dt.hour + dt.minute / 60
    base = 105 + _luteal_offset(dt)
    # dawn phenomenon 3-8am
    if 3 <= hour <= 8:
        base += 28 * math.sin((hour - 3) / 5 * math.pi)
    # meal impulses (spike then exponential decay as insulin acts)
    for meal_hour, carbs in meals:
        dh = hour - meal_hour
        if 0 <= dh <= 4:
            peak = carbs * 1.7
            base += peak * math.exp(-((dh - 0.75) ** 2) / 0.9) * (1 - dh / 6)
    base += RNG.gauss(0, 9)
    # occasional overnight/post-correction low
    if RNG.random() < 0.015:
        base -= RNG.uniform(20, 45)
    return max(45, min(340, base))


def _seed_glucose_and_treatments() -> tuple[int, int]:
    readings, treatments = [], []
    start = NOW - timedelta(days=DAYS)
    day = start
    while day < NOW:
        meals = [(7.5, 40 + RNG.randint(-8, 12)), (12.5, 55 + RNG.randint(-10, 15)), (18.5, 65 + RNG.randint(-12, 18))]
        if RNG.random() < 0.4:
            meals.append((15.5, 18 + RNG.randint(-5, 10)))  # snack

        # boluses + carbs at each meal
        for meal_hour, carbs in meals:
            t = day.replace(hour=int(meal_hour), minute=int((meal_hour % 1) * 60), second=0, microsecond=0)
            if t >= NOW:
                continue
            dose = round(carbs / 10 + RNG.uniform(-0.3, 0.5), 1)
            treatments.append({
                "type": "insulin", "event_type": "Bolus", "timestamp": _iso(t),
                "amount": dose, "insulin_type": "rapid", "notes": f"Carbs: {carbs}g",
                "source": "demo", "owner_email": OWNER_EMAIL,
            })
            treatments.append({
                "type": "carb", "event_type": "Carbs", "timestamp": _iso(t),
                "amount": float(carbs), "source": "demo", "owner_email": OWNER_EMAIL,
            })

        # Control-IQ style temp-basal segments through the day
        for seg in range(0, 24, 2):
            t = day.replace(hour=seg, minute=0, second=0, microsecond=0)
            if t >= NOW:
                continue
            rate = round(max(0.0, RNG.gauss(0.85, 0.35)), 2)
            treatments.append({
                "type": "tempbasal", "event_type": "Temp Basal", "timestamp": _iso(t),
                "absolute": rate, "duration": 120.0, "source": "demo", "owner_email": OWNER_EMAIL,
            })
        if RNG.random() < 0.08:  # occasional suspend
            t = day.replace(hour=RNG.randint(1, 4), minute=0)
            treatments.append({
                "type": "suspension", "event_type": "Basal Suspension", "timestamp": _iso(t),
                "notes": "control-iq", "source": "demo", "owner_email": OWNER_EMAIL,
            })

        # 5-min CGM
        t = day
        while t < day + timedelta(days=1) and t < NOW:
            readings.append({
                "value": round(_glucose_at(t, meals)), "timestamp": _iso(t),
                "trend": "Flat", "source": "demo", "owner_email": OWNER_EMAIL,
            })
            t += timedelta(minutes=5)
        day += timedelta(days=1)

    readings_created, _ = persist_readings_deduped(readings)
    for i in range(0, len(treatments), 1000):
        db.bulk_create_entities("Treatment", treatments[i:i + 1000])
    return readings_created, len(treatments)


def _seed_oura_and_fitbit() -> None:
    oura, fitbit, hr = [], [], []
    start = NOW - timedelta(days=DAYS)
    for d in range(DAYS):
        day = (start + timedelta(days=d))
        date = day.date().isoformat()
        cd = _cycle_day(day)
        # biphasic nightly temperature: low follicular, +0.3-0.5 luteal
        temp = round(RNG.gauss(0.35 if 15 <= cd <= 27 else -0.15, 0.08), 2)
        sleep_score = max(55, min(97, round(RNG.gauss(83, 8))))
        oura.append({
            "date": date, "sleep_score": sleep_score,
            "readiness_score": max(55, min(95, round(RNG.gauss(81, 9)))),
            "readiness_temperature_deviation": temp,
            "readiness_hrv_balance": max(20, min(95, round(RNG.gauss(70, 12)))),
            "lowest_heart_rate": round(RNG.gauss(56, 3)),
            "average_heart_rate": round(RNG.gauss(68, 4)),
            "spo2_average": round(RNG.gauss(96.5, 0.8), 1),
            "owner_email": OWNER_EMAIL,
        })
        fitbit.append({
            "date": date, "steps": max(1500, round(RNG.gauss(8200, 2600))),
            "resting_heart_rate": round(RNG.gauss(58, 3)),
            "sleep_minutes": round(RNG.gauss(430, 45)),
            "spo2_avg": round(RNG.gauss(96, 1), 1),
            "owner_email": OWNER_EMAIL,
        })
    # intraday HR for the last 7 days (dashboard HR chart)
    t = NOW - timedelta(days=7)
    while t < NOW:
        hour = t.hour + t.minute / 60
        bpm = round(58 + 18 * max(0, math.sin((hour - 6) / 18 * math.pi)) + RNG.gauss(0, 6))
        hr.append({"timestamp": _iso(t), "bpm": max(45, bpm), "source": "oura", "owner_email": OWNER_EMAIL})
        t += timedelta(minutes=5)

    repositories = get_repositories()
    for i in range(0, len(oura), 1000):
        repositories.oura_daily.create_many(oura[i:i + 1000])
    for i in range(0, len(fitbit), 1000):
        repositories.fitbit_daily.create_many(fitbit[i:i + 1000])
    for i in range(0, len(hr), 1000):
        repositories.oura_heart_rate.create_many(hr[i:i + 1000])


def _seed_cycle() -> None:
    start = NOW - timedelta(days=DAYS)
    rows = []
    for d in range(DAYS):
        day = (start + timedelta(days=d))
        cd = _cycle_day(day)
        phase = ("menstrual" if cd < 5 else "follicular" if cd < 13 else "ovulation" if cd < 16 else "luteal")
        rows.append({
            "date": day.date().isoformat(), "phase": phase, "source": "oura_inferred",
            "notes": "Inferred from Oura nightly temperature", "owner_email": OWNER_EMAIL,
        })
    db.bulk_create_entities("PeriodLog", rows)


def _seed_labs() -> None:
    # Three draws over the window with improving A1c and a small lipid panel.
    panels = [
        ("Diabetes", [("HbA1c", [7.4, 7.1, 6.8], "%", 4.0, 5.6),
                      ("Glucose", [138, 129, 118], "mg/dL", 70, 99)]),
        ("Lipids", [("LDL Cholesterol", [118, 108, 96], "mg/dL", 0, 100),
                    ("HDL Cholesterol", [46, 49, 53], "mg/dL", 40, 200),
                    ("Triglycerides", [155, 138, 121], "mg/dL", 0, 150)]),
        ("Thyroid", [("TSH", [2.8, 2.4, 2.1], "mIU/L", 0.4, 4.5)]),
        ("Metabolic", [("Creatinine", [0.9, 0.9, 0.8], "mg/dL", 0.5, 1.1),
                       ("ALT", [28, 25, 22], "U/L", 7, 56)]),
    ]
    draw_dates = [(NOW - timedelta(days=88)), (NOW - timedelta(days=44)), (NOW - timedelta(days=4))]
    for i, draw in enumerate(draw_dates):
        rec = db.create_entity("MedicalRecord", {
            "filename": f"lab_report_{draw.date().isoformat()}.pdf",
            "status": "processed", "doc_type": "lab_report",
            "record_date": draw.date().isoformat(),
            "summary": "Routine labs — A1c improving; lipids trending toward target; thyroid stable.",
            "page_count": 1, "lab_count": sum(len(t) for _, t in panels),
            "uploaded_at": _iso(draw), "owner_email": OWNER_EMAIL,
        })
        labs = []
        for category, tests in panels:
            for name, series, unit, lo, hi in tests:
                v = series[i]
                flag = "high" if (hi and v > hi) else "low" if (lo and v < lo) else "normal"
                labs.append({
                    "test_name": name, "value": float(v), "unit": unit,
                    "reference_low": lo, "reference_high": hi, "flag": flag,
                    "collected_date": draw.date().isoformat(), "category": category,
                    "record_id": rec["id"], "owner_email": OWNER_EMAIL,
                })
        db.bulk_create_entities("LabResult", labs)


def _seed_patterns_and_insights() -> None:
    now = _iso(NOW)
    patterns = [
        ("Dawn phenomenon most mornings", "Glucose rises about 40 mg/dL between 3–7am on most days without food or insulin, a common overnight hormonal pattern. Worth discussing overnight basal timing with your care team.", "dawn_phenomenon", "high", "morning", 22),
        ("Afternoon highs recurring", "Readings run above 180 mg/dL through mid-afternoon on more than half of days. Could relate to lunch dosing or timing — one to review with your clinician.", "recurring_high", "medium", "afternoon", 31),
        ("Post-meal spikes above target", "Dinner is followed by spikes over 60 mg/dL within two hours several times a week. Pre-bolus timing is a common lever to explore.", "post_meal_spike", "high", "all_day", 14),
    ]
    for title, expl, ptype, conf, tod, occ in patterns:
        db.create_entity("Pattern", {
            "title": title, "explanation": expl, "pattern_type": ptype, "confidence": conf,
            "time_of_day": tod, "occurrences": occ, "supporting_evidence": json.dumps({"demo": True}),
            "first_detected": now, "last_detected": now, "is_active": True, "is_dismissed": False,
            "owner_email": OWNER_EMAIL,
        })
    insights = [
        ("Better sleep aligns with more time in range", "On nights with a sleep score above 85, the next day's time in range averaged 8 points higher. Correlation isn't causation, but sleep is worth tracking alongside glucose.", "time_in_range", "positive"),
        ("Time in range dips during the luteal phase", "Average time in range was lower in the luteal phase (78%) than the follicular phase (86%), consistent with cycle-related insulin resistance to discuss with your care team.", "comparison", "info"),
        ("Higher-step days show lower average glucose", "Days above 9,000 steps averaged 12 mg/dL lower than low-activity days.", "general", "positive"),
        ("Overnight lows cluster after evening activity", "Several sub-70 mg/dL stretches followed higher-activity evenings — a pattern to review for basal adjustments.", "patterns", "warning"),
    ]
    for title, desc, cat, sev in insights:
        db.create_entity("Insight", {
            "title": title, "description": desc, "category": cat, "severity": sev,
            "date_generated": now, "supporting_data": json.dumps({"demo": True}), "is_read": False,
            "owner_email": OWNER_EMAIL,
        })


def _seed_chat() -> None:
    msgs = [
        {"role": "user", "content": "How did my mornings look this week?"},
        {"role": "assistant", "content": "Your mornings show a consistent dawn rise — glucose climbed from around 105 mg/dL at 3am to about 145 mg/dL by 7am on 5 of the last 7 days, before breakfast. Time in range overnight was 82%. This is a common hormonal pattern; adjusting overnight basal timing is something worth raising with your care team."},
    ]
    db.create_entity("AIConversation", {
        "title": "How did my mornings look this week?", "messages": json.dumps(msgs),
        "context_summary": "Demo conversation", "is_archived": False, "owner_email": OWNER_EMAIL,
    })


def is_seeded() -> bool:
    return db.get_setting("demo_seeded") == "1"


def seed(force: bool = False) -> dict:
    db.init_db()
    if is_seeded() and not force:
        return {"ok": True, "skipped": "already seeded"}
    if force:
        _wipe()
    r, t = _seed_glucose_and_treatments()
    _seed_oura_and_fitbit()
    _seed_cycle()
    _seed_labs()
    _seed_patterns_and_insights()
    _seed_chat()
    db.set_setting("demo_seeded", "1")
    return {"ok": True, "readings": r, "treatments": t}


def _wipe() -> None:
    for t in SEED_TYPES:
        db.delete_entities_where(t, {"owner_email": OWNER_EMAIL})


def clear_if_demo_leftover() -> bool:
    """Called on NON-demo startup. If this volume was previously seeded as a
    demo, purge all synthetic data so real usage starts clean (first-run setup
    then runs normally, since demo mode never set an admin password)."""
    db.init_db()
    if db.get_setting("demo_seeded") != "1":
        return False
    _wipe()
    db.set_setting("demo_seeded", "")
    return True


if __name__ == "__main__":
    print(seed(force="--force" in sys.argv))
