"""Visit Report correctness: weight, hypoglycemia inputs, wearable averages,
patient header, narrative budgeting/failure, lab hygiene, and insulin tiles.

All fixtures are synthetic.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from server import db, profile, report
from server.migrations import run_migrations

OWNER = "owner@glucopilot.local"


@pytest.fixture
def report_db(tmp_path, monkeypatch):
    database = tmp_path / "app.sqlite3"
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setenv("CANONICAL_TIME_ENABLED", "false")
    return database


# ── 1. weight ────────────────────────────────────────────────────────────


def test_weight_block_reports_series_window_change_and_change_since_first(report_db):
    for day, kg in (
        ("2026-03-01", 70.0),
        ("2026-06-15", 66.0),
        ("2026-08-01", 64.0),
        ("2026-09-15", 62.0),
        ("2026-10-15", 61.0),
        ("2026-10-15", 51.0 + 10),  # same-day rewrite keeps one point
        ("2026-10-16", "not a number"),
    ):
        db.create_entity("WeightLog", {"date": day, "weight_kg": kg, "owner_email": OWNER})

    weight = report._weight(date(2026, 7, 22))

    assert weight["available"] is True
    assert weight["entries"] == 5
    assert weight["latest"] == {"date": "2026-10-15", "weight_kg": 61.0}
    assert weight["first"] == {"date": "2026-03-01", "weight_kg": 70.0}
    # Baseline for the window is the last weight on or before its start.
    assert weight["window_change"]["from_date"] == "2026-06-15"
    assert weight["window_change"]["change_kg"] == -5.0
    assert weight["since_first_change"]["change_kg"] == -9.0
    assert weight["since_first_change"]["change_pct"] == pytest.approx(-12.9)
    assert [point["date"] for point in weight["series"]] == [
        "2026-03-01", "2026-06-15", "2026-08-01", "2026-09-15", "2026-10-15",
    ]


def test_weight_block_is_unavailable_without_entries(report_db):
    assert report._weight(date(2026, 7, 22))["available"] is False


# ── 3. wearable averages ─────────────────────────────────────────────────


def test_spo2_and_other_wellness_averages_ignore_zero_and_implausible_days(report_db):
    today = datetime.now(timezone.utc).date()
    for offset, values in enumerate((
        {"spo2_average": 97.0, "sleep_score": 80, "lowest_heart_rate": 60, "readiness_score": 70},
        {"spo2_average": 95.0, "sleep_score": 0, "lowest_heart_rate": 0, "readiness_score": 0},
        {"spo2_average": 0.0, "sleep_score": 70, "lowest_heart_rate": 50, "readiness_score": 80},
        {"spo2_average": 12.0},
    )):
        db.create_entity("OuraDaily", {
            "date": (today - timedelta(days=offset)).isoformat(), "owner_email": OWNER, **values,
        })
    for offset, values in enumerate((
        {"steps": 8000, "sleep_minutes": 420, "resting_heart_rate": 62, "spo2_avg": 96.0},
        {"steps": 0, "sleep_minutes": 0, "resting_heart_rate": 0, "spo2_avg": 0.0},
    )):
        db.create_entity("FitbitDaily", {
            "date": (today - timedelta(days=offset)).isoformat(), "owner_email": OWNER, **values,
        })

    wellness = report._wellness(30)

    assert wellness["oura"]["avg_spo2"] == 96.0  # not (97+95+0+12)/4
    assert wellness["oura"]["avg_sleep_score"] == 75
    assert wellness["oura"]["avg_resting_hr"] == 55
    assert wellness["oura"]["avg_readiness_score"] == 75
    assert wellness["fitbit"]["avg_steps"] == 8000
    assert wellness["fitbit"]["avg_sleep_hours"] == 7.0
    assert wellness["fitbit"]["avg_resting_hr"] == 62
    assert wellness["fitbit"]["avg_spo2"] == 96.0


def test_wellness_average_is_none_when_every_value_is_missing():
    assert report._mean([{"spo2_average": 0.0}, {"spo2_average": None}], "spo2_average", 1) is None


# ── 4. patient header ────────────────────────────────────────────────────


def test_patient_block_reads_name_and_birth_date_from_health_profile(report_db):
    profile.save(profile.ProfileBody(
        full_name="  Synthetic   Patient ", date_of_birth="1990-02-03", sex="female", weight_kg=60,
    ))

    patient = report._patient()

    assert patient["name"] == "Synthetic Patient"
    assert patient["date_of_birth"] == "1990-02-03"
    assert patient["sex"] == "female"
    assert patient["age"] >= 36

    # A save from a client that doesn't send a name keeps the stored one.
    profile.save(profile.ProfileBody(date_of_birth="1990-02-03", weight_kg=60))
    assert report._patient()["name"] == "Synthetic Patient"


def test_patient_name_falls_back_to_owner_account_name_but_never_username(report_db):
    assert report._patient()["name"] is None
    db.set_setting("user_profile", json.dumps({"full_name": "Synthetic Owner"}))
    assert report._patient()["name"] == "Synthetic Owner"


# ── 6. labs ──────────────────────────────────────────────────────────────


def _lab(name, value, collected, **extra):
    db.create_entity("LabResult", {
        "test_name": name, "value": value, "unit": extra.pop("unit", ""),
        "collected_date": collected, "category": extra.pop("category", "Synthetic"),
        "source": "medical_record", "owner_email": OWNER, **extra,
    })


@pytest.mark.parametrize(("text", "expected"), [
    ("2026-05-16", date(2026, 5, 16)),
    ("2026-05-16T08:00:00Z", date(2026, 5, 16)),
    ("05/16/2026", date(2026, 5, 16)),
    ("5/16/26", date(2026, 5, 16)),
    ("2026/05/16", date(2026, 5, 16)),
    ("May 16, 2026", date(2026, 5, 16)),
    ("Sept 3 2025", date(2025, 9, 3)),
    ("5/16 PM", None),
    ("13/45/2026", None),
    ("", None),
    (None, None),
])
def test_lab_dates_parse_as_calendar_dates(text, expected):
    assert report._lab_date(text) == expected


def test_labs_sort_by_real_date_merge_synonyms_and_drop_duplicate_rows(report_db):
    # A1c reported under two names; the April draw is the newest.
    _lab("HbA1c", 7.1, "2026-01-10", unit="%", reference_high=5.6)
    _lab("Hemoglobin A1c", 6.8, "04/02/2026", unit="%", reference_high=5.6)
    # The same cortisol draw twice: once with its date, once with a damaged one.
    _lab("Cortisol A.M.", 15.5, "2026-05-16", unit="ug/dL", reference_low=6.2, reference_high=19.4)
    _lab("Cortisol, AM", 15.5, "5/16 PM", unit="ug/dL", reference_low=6.2, reference_high=19.4)
    # TPO under several names, including an identical repeat.
    _lab("TPO Ab", 120, "2025-11-01", unit="IU/mL", reference_high=34, flag="high")
    _lab("Thyroid Peroxidase (TPO) Ab", 120, "2025-11-01", unit="IU/mL", reference_high=34, flag="high")
    _lab("Anti-TPO", 150, "2026-03-01", unit="IU/mL", reference_high=34, flag="high")
    _lab("TPO Antibodies", 150, "03/01/2026", unit="IU/mL", reference_high=34, flag="high")
    # A junk date that sorts above every ISO date as text must not become "latest".
    _lab("TSH", 2.0, "2026-01-05", unit="mIU/L", reference_low=0.4, reference_high=4.0)
    _lab("TSH", 9.9, "5/16 PM", unit="mIU/L", reference_low=0.4, reference_high=4.0)
    _lab("Synthetic marker", 1.0, "2026-02-02", category="Other")

    labs = report._labs()
    tests = {entry["test_name"]: entry for entries in labs["categories"].values() for entry in entries}

    assert "HbA1c" not in tests
    a1c = tests["Hemoglobin A1c"]
    assert (a1c["value"], a1c["date"], a1c["count"]) == (6.8, "2026-04-02", 2)
    assert a1c["reported_names"] == ["HbA1c"]
    assert a1c["flag"] == "high"  # derived from the reference range
    assert a1c["trend"] == "down"

    cortisol = tests["Cortisol (AM)"]
    assert (cortisol["value"], cortisol["count"], cortisol["flag"]) == (15.5, 1, "normal")

    tpo = tests["TPO antibodies"]
    assert (tpo["value"], tpo["count"], tpo["date"]) == (150, 2, "2026-03-01")
    assert [point["value"] for point in tpo["history"]] == [120, 150]

    tsh = tests["TSH"]
    assert (tsh["value"], tsh["date"]) == (2.0, "2026-01-05")
    assert labs["undated_results"] == 1

    assert labs["duplicates_removed"] == 3
    assert [entry["test_name"] for entry in labs["key_results"]] == [
        "Hemoglobin A1c", "TSH", "TPO antibodies", "Cortisol (AM)",
    ]
    assert all(entry["flag"] for entry in labs["key_results"])
    assert "Synthetic marker" not in [entry["test_name"] for entry in labs["key_results"]]


def test_verified_copy_wins_when_duplicates_collapse(report_db):
    _lab("TSH", 2.0, "2026-01-05", unit="mIU/L")
    _lab("Thyroid Stimulating Hormone", 2.0, "2026-01-05", unit="mIU/L", verification_status="approved")

    entry = report._labs()["key_results"][0]

    assert entry["count"] == 1
    assert entry["verification"]["verification_status"] == "approved"


# ── 7. insulin tiles ─────────────────────────────────────────────────────


def _daily_total(day, total, bolus=0.0):
    return {
        "type": "insulin", "event_type": "Daily Total", "timestamp": f"{day}T12:00:00.000Z",
        "notes": f"Bolus: {bolus}U | Basal: {round(total - bolus, 2)}U | Total: {total}U",
        "source": "glooko",
    }


def test_insulin_flags_incomplete_bolus_stream_and_counts_temp_basal_corrections(monkeypatch):
    treatments = [
        _daily_total("2026-01-10", 38.0),
        _daily_total("2026-01-11", 37.8),
        {"type": "insulin", "event_type": "Bolus", "amount": 0.2, "timestamp": "2026-01-10T08:00:00Z", "source": "glooko"},
        # +95% for 90 min at 4.58 U/h: 4.58 * (1 - 1/1.95) * 1.5 = 3.35 U extra
        {"type": "tempbasal_correction", "absolute": 4.58, "multiplier": 1.95, "duration": 90,
         "timestamp": "2026-01-10T14:00:00Z", "source": "glooko"},
        {"type": "tempbasal_correction", "absolute": 4.58, "multiplier": 1.95, "duration": 90,
         "timestamp": "2026-01-10T14:00:00Z", "source": "glooko"},  # duplicate row
        {"type": "tempbasal_correction", "absolute": 2.0, "multiplier": 1.0, "duration": 60,
         "timestamp": "2026-01-11T14:00:00Z", "source": "glooko"},  # no increase
        {"type": "carb", "amount": 8, "timestamp": "2026-01-10T12:00:00Z", "source": "glooko"},
    ]
    monkeypatch.setattr(report, "_paged", lambda _entity, _since: treatments)

    insulin = report._insulin(ZoneInfo("UTC"), "2026-01-10T00:00:00Z", 2)

    assert insulin["pump_reported_avg_tdd"] == pytest.approx(37.9)
    assert insulin["bolus_stream_incomplete"] is True
    assert insulin["bolus_source"] == "Glooko bolus event stream"
    corrections = insulin["temp_basal_corrections"]
    assert corrections["count"] == 1
    assert corrections["extra_units_total"] == pytest.approx(3.3, abs=0.1)
    assert corrections["extra_units_per_day"] == pytest.approx(1.7, abs=0.1)
    assert insulin["carbs_representative"] is False


def test_bolus_stream_is_complete_when_boluses_cover_a_normal_share_of_tdd(monkeypatch):
    treatments = [
        _daily_total("2026-01-10", 40.0, bolus=18.0),
        *[
            {"type": "insulin", "event_type": "Bolus", "amount": 6.0,
             "timestamp": f"2026-01-10T{hour:02d}:00:00Z", "source": "glooko"}
            for hour in (8, 12, 18)
        ],
    ]
    monkeypatch.setattr(report, "_paged", lambda _entity, _since: treatments)

    insulin = report._insulin(ZoneInfo("UTC"), "2026-01-10T00:00:00Z", 1)

    assert insulin["bolus_stream_incomplete"] is False
    assert insulin["temp_basal_corrections"]["count"] == 0


# ── 5. narrative ─────────────────────────────────────────────────────────


def _narrative_payload(item_count=600, note_chars=8000):
    good = {"ai_eligible": True, "coverage_pct": 100.0, "reliability": "high", "exclusion_reasons": []}
    items = [
        {
            "id": f"urn:synthetic:evidence:{index}",
            "domain": "records",
            "entity_type": "MedicalRecord",
            "entity_id": f"rec{index}",
            "observed_at": "2026-05-01",
            "data": {"title": f"Synthetic record {index}", "notes": "x" * note_chars},
            "confidence": None,
            "source_links": [{"kind": "record", "href": f"/records/rec{index}"}],
            "claim": None,
        }
        for index in range(item_count)
    ]
    return {
        "days": 90,
        "patient": {"name": "Synthetic Patient", "age": 36, "sex": "female"},
        "weight": {
            "available": True, "entries": 3,
            "latest": {"date": "2026-10-15", "weight_kg": 61.0},
            "first": {"date": "2026-03-01", "weight_kg": 70.0},
            "window_change": {"change_kg": -5.0, "change_pct": -7.6},
            "since_first_change": {"change_kg": -9.0, "change_pct": -12.9},
            "series": [{"date": "2026-03-01", "weight_kg": 70.0}] * 50,
        },
        "glucose": {
            "available": True, "avg": 140, "gmi": 6.7, "cv": 38.0, "tir": 72.0,
            "tbr70": 10.3, "tbr54": 3.1, "tar180": 17.7, "tar250": 4.0, "days": 90, "quality": good,
        },
        "insulin": {
            "available": True, "quality": good, "nutrition_quality": good,
            "avg_tdd_est": 37.9, "avg_daily_bolus": 0.1, "boluses_per_day": 0.1,
            "bolus_stream_incomplete": True, "avg_daily_carbs": 4, "carbs_representative": False,
            "discrepancies": [{"date": "2026-01-01"}] * 500,
        },
        "cycle": {"available": False, "quality": {}},
        "wellness": {"oura": None, "fitbit": None, "quality": {}},
        "labs": {"key_results": [{
            "test_name": "Hemoglobin A1c", "value": 6.8, "unit": "%", "date": "2026-04-02",
            "flag": "high", "verification": {"verification_status": "unverified"},
        }]},
        "_evidence_reasoning": {
            "bundle_id": "urn:synthetic:bundle",
            "items": items,
            "contradictions": [],
            "missing_data_caveats": [],
            "source_diagnostics": [{"detail": "y" * 50_000}],
        },
    }


def test_narrative_prompt_is_compact_and_carries_hypoglycemia_and_weight(monkeypatch):
    captured = {}

    async def no_limit(*_args, **_kwargs):
        return None

    async def fake_llm(prompt, **_kwargs):
        captured["prompt"] = prompt
        return {
            "headline": "Synthetic headline", "glucose_summary": "Synthetic.",
            "discussion_points": [], "evidence_item_ids": ["N1", "N999"],
        }

    monkeypatch.setattr(report, "context_limit", no_limit)
    monkeypatch.setattr(report, "invoke_llm", fake_llm)
    payload = _narrative_payload()

    narrative = asyncio.run(report._narrative(payload))

    prompt = captured["prompt"]
    # ~4.8M characters of evidence in; a bounded prompt out.
    assert len(prompt) < 60_000
    data = json.loads(prompt.split("(JSON):\n", 1)[1].split("\n\nWrite a concise", 1)[0])
    metrics = data["deterministic_metrics"]
    assert metrics["glucose"]["tbr70"] == 10.3
    assert metrics["glucose"]["tbr54"] == 3.1
    assert metrics["glucose"]["consensus_targets_pct"] == {"tbr70_below": 4.0, "tbr54_below": 1.0, "tir_above": 70}
    assert metrics["weight"]["since_first_change"]["change_kg"] == -9.0
    assert "series" not in metrics["weight"]
    assert metrics["key_labs"][0]["test"] == "Hemoglobin A1c"
    assert "avg_daily_bolus" not in metrics["insulin"]
    assert "avg_daily_carbs" not in metrics["insulin"]
    assert "discrepancies" not in metrics["insulin"]
    assert "Synthetic Patient" not in prompt
    evidence = data["shared_evidence_context"]
    assert len(evidence["items"]) == report.NARRATIVE_EVIDENCE_ITEMS
    assert evidence["items_truncated"] == {"kept": report.NARRATIVE_EVIDENCE_ITEMS, "available": 600}
    assert "source_diagnostics" not in evidence
    assert "under 4%" in prompt
    # Short prompt ids translate back to real evidence ids; invented ones drop.
    assert narrative["evidence_item_ids"] == ["urn:synthetic:evidence:0"]
    assert "narrative_status" not in payload


def test_narrative_sheds_evidence_to_fit_the_model_window(monkeypatch):
    sizes = []

    async def limit(*_args, **_kwargs):
        return 10_000

    async def count(text, *_args, **_kwargs):
        sizes.append(len(text))
        return len(text)

    async def fake_llm(prompt, **_kwargs):
        return {"headline": "Fits", "glucose_summary": "", "discussion_points": [], "evidence_item_ids": []}

    monkeypatch.setattr(report, "context_limit", limit)
    monkeypatch.setattr(report, "count_tokens", count)
    monkeypatch.setattr(report, "invoke_llm", fake_llm)

    assert asyncio.run(report._narrative(_narrative_payload(note_chars=300)))["headline"] == "Fits"
    assert sizes == sorted(sizes, reverse=True) and len(sizes) > 1


def test_narrative_failure_is_logged_and_reported_not_swallowed(monkeypatch, caplog):
    async def no_limit(*_args, **_kwargs):
        return None

    async def failing_llm(_prompt, **_kwargs):
        raise RuntimeError("synthetic provider outage")

    monkeypatch.setattr(report, "context_limit", no_limit)
    monkeypatch.setattr(report, "invoke_llm", failing_llm)
    payload = _narrative_payload(item_count=3, note_chars=10)

    with caplog.at_level(logging.ERROR, logger="glucopilot.report"):
        assert asyncio.run(report._narrative(payload)) is None

    assert payload["narrative_status"]["available"] is False
    assert "language model request failed" in payload["narrative_status"]["reason"]
    assert any("visit report narrative failed (RuntimeError)" in record.getMessage() for record in caplog.records)


def test_narrative_that_cannot_fit_is_reported_unavailable(monkeypatch):
    async def tiny(*_args, **_kwargs):
        return 2000

    async def count(text, *_args, **_kwargs):
        return len(text)

    async def never_called(*_args, **_kwargs):
        raise AssertionError("the model must not be called with an oversized prompt")

    monkeypatch.setattr(report, "context_limit", tiny)
    monkeypatch.setattr(report, "count_tokens", count)
    monkeypatch.setattr(report, "invoke_llm", never_called)
    payload = _narrative_payload(item_count=3, note_chars=10)

    assert asyncio.run(report._narrative(payload)) is None
    assert "context window" in payload["narrative_status"]["reason"]


# ── end to end ───────────────────────────────────────────────────────────


def test_visit_report_payload_carries_patient_weight_and_narrative_status(report_db, monkeypatch):
    profile.save(profile.ProfileBody(full_name="Synthetic Patient", date_of_birth="1990-02-03", weight_kg=70))
    db.create_entity("WeightLog", {
        "date": (datetime.now(timezone.utc).date() - timedelta(days=200)).isoformat(),
        "weight_kg": 78.0, "owner_email": OWNER,
    })

    async def unavailable(payload):
        payload["narrative_status"] = {"available": False, "reason": "The written summary is unavailable: synthetic."}
        return None

    monkeypatch.setattr(report, "_narrative", unavailable)

    visit = asyncio.run(report.visit_report(report.ReportBody(days=90)))

    assert visit["patient"]["name"] == "Synthetic Patient"
    assert visit["patient"]["date_of_birth"] == "1990-02-03"
    assert visit["weight"]["since_first_change"]["change_kg"] == -8.0
    assert visit["narrative"] is None
    assert visit["narrative_status"]["available"] is False
    assert visit["narrative_status"]["reason"].endswith("synthetic.")
    assert "unaffected" in visit["narrative_status"]["note"]
    assert "_evidence_reasoning" not in visit
