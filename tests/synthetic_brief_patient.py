"""A synthetic patient that reproduces every Clinician Brief audit finding.

No real PHI: names, values and documents are invented. Used by
tests/test_clinician_briefs.py and to render the sample brief for review.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

from server import db

OWNER = "owner@glucopilot.local"


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def seed(end: datetime, *, cgm_days: int = 10, pump_days: int = 30) -> dict[str, str]:
    """Write the synthetic record set; return ids the tests assert against."""
    ids: dict[str, str] = {}
    today = end.date()

    def create(entity_type: str, data: dict) -> dict:
        return db.create_entity(entity_type, {"owner_email": OWNER, **data})

    # Standing record. Diagnoses predate any brief window.
    ids["t1d"] = create("Diagnosis", {"name": "Type 1 diabetes", "status": "active", "diagnosed_date": "2009-03-01"})["id"]
    ids["thyroiditis"] = create("Diagnosis", {"name": "Autoimmune thyroiditis", "status": "active", "diagnosed_date": "2016-05-10"})["id"]
    ids["tentative_dx"] = create("Diagnosis", {"name": "Suspected Graves disease", "status": "active", "notes": "Raised by patient; no antibody testing yet."})["id"]
    create("Diagnosis", {"name": "Polycystic ovary syndrome", "status": "suspected", "notes": "Legacy suspected entry."})
    ids["insulin"] = create("Medication", {"name": "Insulin aspart (pump)", "kind": "medication", "dose": "per pump settings", "status": "active"})["id"]
    ids["levo"] = create("Medication", {"name": "Levothyroxine", "kind": "medication", "dose": "75 mcg", "frequency": "daily", "status": "active"})["id"]
    create("Medication", {"name": "Vitamin D3", "kind": "supplement", "dose": "2000 IU", "frequency": "daily", "status": "active"})
    create("Medication", {"name": "Metformin", "kind": "medication", "dose": "500 mg", "status": "stopped"})
    ids["allergy"] = create("Allergy", {"allergen": "Penicillin", "reaction": "Hives", "severity": "moderate"})["id"]
    create("HealthProfile", {"diabetes_type": "type1", "sex": "female", "height_cm": 165, "date_of_birth": "1990-01-01"})

    # 18.6 kg (41 lb) down over roughly eleven months.
    for month in range(12):
        day = today - timedelta(days=335 - month * 30)
        create("WeightLog", {"date": day.isoformat(), "weight_kg": round(79.8 - 18.6 * month / 11, 1)})

    # Labs: 40 tests x 3 draws (120 rows, none matching any brief intent word).
    lab_record = create("MedicalRecord", {
        "doc_type": "lab_report", "status": "processed", "title": "Synthetic Labs — panel",
        "record_date": (today - timedelta(days=12)).isoformat(),
        "summary": "Comprehensive panel.", "filename": "labs.pdf",
    })
    ids["lab_record"] = lab_record["id"]
    draws = [today - timedelta(days=offset) for offset in (300, 150, 12)]
    labs = []
    for test_index in range(38):
        for draw_index, draw in enumerate(draws):
            labs.append({
                "test_name": f"Synthetic analyte {test_index:02d}", "value": 10.0 + test_index + draw_index,
                "unit": "U/L", "reference_low": 5, "reference_high": 80, "flag": "normal",
                "collected_date": draw.isoformat(), "category": "Chemistry",
                "record_id": lab_record["id"], "verification_status": "approved",
            })
    for draw_index, (draw, value, flag) in enumerate(zip(draws, (3.1, 4.4, 6.2), ("normal", "normal", "high"))):
        labs.append({
            "test_name": "TSH", "value": value, "unit": "mIU/L", "reference_low": 0.4,
            "reference_high": 4.5, "flag": flag, "collected_date": draw.isoformat(),
            "category": "Thyroid", "record_id": lab_record["id"], "verification_status": "approved",
        })
    for draw, value in zip(draws, (7.4, 7.0, 6.6)):
        labs.append({
            "test_name": "Hemoglobin A1c", "value": value, "unit": "%", "reference_low": 4.0,
            "reference_high": 5.6, "flag": "high", "collected_date": draw.isoformat(),
            "category": "Diabetes", "record_id": lab_record["id"],
        })
    db.bulk_create_entities("LabResult", [{"owner_email": OWNER, **lab} for lab in labs])
    ids["imaging"] = create("MedicalRecord", {
        "doc_type": "imaging_report", "status": "processed", "title": "Synthetic Imaging — thyroid ultrasound",
        "record_date": (today - timedelta(days=20)).isoformat(),
        "summary": "Heterogeneous gland echotexture; no discrete nodule.",
    })["id"]

    # Junk and private uploads that must never reach a clinician.
    for title, doc_type, summary in (
        ("Insurance card front", "other", "Member ID and group number for the health plan."),
        ("Card scan back", "insurance_card", "Rx BIN and PCN."),
        ("Favorite verses", "other", "A list of Bible verses."),
        ("Daily supplement protocol", "visit_summary", "Supplement protocol: magnesium, zinc and inositol each morning."),
    ):
        create("MedicalRecord", {
            "doc_type": doc_type, "status": "processed", "title": title,
            "record_date": (today - timedelta(days=30)).isoformat(), "summary": summary,
        })

    ids["symptoms"] = create("HistoryEntry", {
        "title": "Reported symptoms: shakiness, palpitations, heat intolerance",
        "kind": "note", "entry_date": (today - timedelta(days=9)).isoformat(),
        "details": "Most afternoons for three weeks.",
    })["id"]
    create("HistoryEntry", {"title": "Endocrinology visit", "kind": "appointment", "entry_date": (today - timedelta(days=40)).isoformat()})

    # Four generations of one insight (three superseded), plus one resting on SpO2.
    for generation in range(4):
        create("Insight", {
            "title": "Your sleep score lines up with your time in range",
            "description": "On nights you sleep better, your next-day time in range is higher.",
            "category": "time_in_range", "severity": "positive",
            "date_generated": _iso(end - timedelta(days=21 - generation * 7)),
            "supporting_data": json.dumps({"kind": "correlation", "x": "Sleep score (Oura)", "y": "time in range", "r": 0.4}),
            "is_active": generation == 3,
        })
    create("Insight", {
        "title": "Your blood oxygen (SpO2) tracks your average glucose",
        "description": "Lower SpO2 days line up with higher glucose for you.",
        "category": "general", "severity": "warning", "date_generated": _iso(end - timedelta(days=1)),
        "supporting_data": json.dumps({"kind": "correlation", "x": "Blood oxygen (SpO2)", "y": "average glucose", "r": -0.5}),
        "is_active": True,
    })
    for generation in range(2):
        create("Pattern", {
            "title": "Your mornings run high", "explanation": "Your glucose rises before breakfast.",
            "pattern_type": "dawn_phenomenon", "time_of_day": "morning", "occurrences": 20 + generation,
            "last_detected": _iso(end - timedelta(days=8 - generation * 7)), "is_active": generation == 1,
            "is_dismissed": False,
        })
    db.bulk_create_entities("OuraDaily", [
        {
            "owner_email": OWNER, "date": (today - timedelta(days=offset)).isoformat(),
            "sleep_score": 80, "lowest_heart_rate": 58, "hrv": 35,
            "sleep_total_seconds": 25200, "spo2_average": 0.0 if offset % 3 == 0 else 96.0,
        }
        for offset in range(1, 31)
    ])

    # CGM with an afternoon low most days (time below range well above target).
    start = end - timedelta(days=cgm_days)
    readings = []
    instant = start
    while instant < end:
        hour = instant.hour + instant.minute / 60
        value = 140 + 45 * math.sin(hour / 24 * 2 * math.pi)
        if 14 <= hour < 15.5:
            value = 52 if hour < 14.75 else 64
        readings.append({"owner_email": OWNER, "timestamp": _iso(instant), "value": round(value), "source": "synthetic-cgm"})
        instant += timedelta(minutes=5)
    db.bulk_create_entities("GlucoseReading", readings)

    # Pump rows: the raw volume that used to crowd the brief.
    treatments = []
    for offset in range(pump_days):
        day = datetime.combine(today - timedelta(days=offset + 1), datetime.min.time(), tzinfo=timezone.utc)
        for hour, carbs in ((8, 40), (12, 55), (18, 65)):
            moment = day + timedelta(hours=hour)
            treatments.append({"type": "insulin", "event_type": "Bolus", "timestamp": _iso(moment), "amount": carbs / 10, "insulin_type": "rapid", "source": "synthetic"})
            treatments.append({"type": "carb", "event_type": "Carbs", "timestamp": _iso(moment), "amount": float(carbs), "source": "synthetic"})
        for segment in range(0, 24, 2):
            treatments.append({"type": "tempbasal", "event_type": "Temp Basal", "timestamp": _iso(day + timedelta(hours=segment)), "absolute": 0.8, "duration": 120.0, "source": "synthetic"})
        treatments.append({"type": "pump_mode", "event_type": "Pump Mode", "mode": "automatic", "timestamp": _iso(day), "duration": 1080.0, "source": "synthetic"})
        treatments.append({"type": "pump_mode", "event_type": "Pump Mode", "mode": "manual", "timestamp": _iso(day + timedelta(hours=18)), "duration": 360.0, "source": "synthetic"})
        treatments.append({"type": "tempbasal_correction", "event_type": "Temp Basal", "timestamp": _iso(day + timedelta(hours=22)), "absolute": 1.6, "multiplier": 1.95, "duration": 90.0, "source": "synthetic"})
    db.bulk_create_entities("Treatment", [{"owner_email": OWNER, **row} for row in treatments])
    return ids


def window_end() -> datetime:
    return datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)

