from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from server import report
from server.data_quality import (
    QUALITY_VERSION,
    assess_cgm,
    assess_daily,
    assess_nutrition,
    assess_pump_tdd,
    build_envelope,
)


pytestmark = pytest.mark.risk_critical
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "golden" / "data_quality.json"


@pytest.fixture(scope="module")
def quality_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_quality_fixture_is_explicitly_synthetic_and_public_safe(quality_fixture):
    encoded = json.dumps(quality_fixture, sort_keys=True)
    assert quality_fixture["synthetic"] is True
    assert quality_fixture["subject"]["id"].startswith("synthetic-")
    assert set(re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", encoded, re.I)) == {
        "owner@glucopilot.local"
    }
    assert not re.search(r"access[_-]?token|refresh[_-]?token|api[_-]?key|client[_-]?secret|password", encoded, re.I)


def test_complete_cgm_window_has_stable_versioned_envelope(quality_fixture):
    fixture = quality_fixture["cgm"]
    start = datetime.fromisoformat(fixture["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(fixture["end"].replace("Z", "+00:00"))
    rows = [
        {
            "timestamp": (start + timedelta(minutes=index * fixture["interval_minutes"]))
            .isoformat()
            .replace("+00:00", "Z"),
            "value": 100 + index,
        }
        for index in range(fixture["complete_readings"])
    ]

    actual = assess_cgm(rows, ZoneInfo("UTC"), start=start, end=end, as_of=date.fromisoformat(quality_fixture["as_of"]))

    assert actual["quality_version"] == QUALITY_VERSION
    assert {key: actual[key] for key in fixture["expected"]} == fixture["expected"]
    assert actual["data_through"] == quality_fixture["as_of"]
    assert actual["input_data_version"].startswith("sha256:")
    assert actual == assess_cgm(
        list(reversed(rows)), ZoneInfo("UTC"), start=start, end=end,
        as_of=date.fromisoformat(quality_fixture["as_of"]),
    )


def test_cgm_duplicates_are_counted_once_and_out_of_window_rows_are_not_duplicates(quality_fixture):
    fixture = quality_fixture["cgm"]
    start = datetime.fromisoformat(fixture["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(fixture["end"].replace("Z", "+00:00"))
    duplicate = {"timestamp": fixture["start"], "value": 101}
    rows = [
        duplicate,
        dict(duplicate),
        {"timestamp": (start - timedelta(days=1)).isoformat(), "value": 90},
        {"timestamp": "not-a-time", "value": 95},
        {"timestamp": fixture["end"], "value": "NaN"},
    ]

    actual = assess_cgm(rows, ZoneInfo("UTC"), start=start, end=end, as_of=date.fromisoformat(quality_fixture["as_of"]))

    assert actual["observed"] == 1
    assert any("1 duplicate timestamp" in note for note in actual["limitations"])
    assert not any("2 duplicate" in note for note in actual["limitations"])
    assert actual["ai_eligible"] is False


def test_future_data_through_is_not_treated_as_current(quality_fixture):
    as_of = date.fromisoformat(quality_fixture["as_of"])
    actual = build_envelope(
        "wearables", observed=5, expected=5, unit="days",
        data_through=as_of + timedelta(days=1), as_of=as_of,
    )

    assert actual["complete"] is False
    assert actual["ai_eligible"] is False
    assert "data-through date is in the future" in actual["exclusion_reasons"]


def test_daily_domain_calculators_match_golden_coverage(quality_fixture):
    fixture = quality_fixture["daily_window"]
    start, end = date.fromisoformat(fixture["start"]), date.fromisoformat(fixture["end"])
    as_of = date.fromisoformat(quality_fixture["as_of"])
    tz = ZoneInfo("UTC")

    wearables = assess_daily(
        "wearables", [{"date": day, "steps": 1000} for day in fixture["wearable_dates"]], tz,
        start_date=start, end_date=end, as_of=as_of, required_fields=("steps",),
    )
    cycle = assess_daily(
        "cycle", [{"date": day, "phase": "synthetic"} for day in fixture["cycle_dates"]], tz,
        start_date=start, end_date=end, as_of=as_of, required_fields=("phase",),
    )
    nutrition = assess_nutrition(
        [{"timestamp": f"{day}T12:00:00Z", "type": "carb", "amount": 25} for day in fixture["nutrition_dates"]],
        tz, start_date=start, end_date=end, as_of=as_of,
    )

    assert wearables["coverage_pct"] == fixture["expected"]["wearables_coverage_pct"]
    assert cycle["coverage_pct"] == fixture["expected"]["cycle_coverage_pct"]
    assert nutrition["coverage_pct"] == fixture["expected"]["nutrition_coverage_pct"]
    assert wearables["ai_eligible"] is True
    assert cycle["ai_eligible"] is True
    assert nutrition["reliability"] == "low"
    assert nutrition["ai_eligible"] is False
    assert any("reliability score" in reason for reason in nutrition["exclusion_reasons"])
    assert "logged entries only" in " ".join(nutrition["limitations"])


def test_uninterpretable_carbs_and_incomplete_tdd_are_explicit(quality_fixture):
    fixture = quality_fixture["daily_window"]
    start, end = date.fromisoformat(fixture["start"]), date.fromisoformat(fixture["end"])
    as_of = date.fromisoformat(quality_fixture["as_of"])
    nutrition = assess_nutrition(
        [{"timestamp": f"{fixture['end']}T12:00:00Z", "type": "carb", "amount": "unknown"}],
        ZoneInfo("UTC"), start_date=start, end_date=end, as_of=as_of,
    )
    assert nutrition["ai_eligible"] is False
    assert "carbohydrate records are uninterpretable" in nutrition["exclusion_reasons"]

    pump_fixture = quality_fixture["pump_tdd"]
    complete = set(pump_fixture["complete_dates"])
    reconciliation = {
        "days": [
            {
                "date": day,
                "pump_reported": {"selected": {"total_units": 20} if day in complete else None},
                "calculated": {"total_units": None},
                "completeness": "pump_reported" if day in complete else "bolus_without_basal",
            }
            for day in pump_fixture["complete_dates"] + pump_fixture["incomplete_dates"]
        ],
        "summary": {"limitations": []},
    }
    quality = assess_pump_tdd(reconciliation, start_date=start, end_date=end, as_of=as_of)
    assert {key: quality[key] for key in ("coverage_pct", "observed", "expected", "ai_eligible")} == {
        key: pump_fixture["expected"][key] for key in ("coverage_pct", "observed", "expected", "ai_eligible")
    }
    assert pump_fixture["expected"]["incomplete_warning"] in quality["limitations"]


def test_insulin_response_requires_the_golden_minimum_sample_count(quality_fixture):
    fixture = quality_fixture["insulin_response"]
    as_of = date.fromisoformat(quality_fixture["as_of"])
    quality = build_envelope(
        "insulin_response", observed=fixture["below_minimum_samples"],
        expected=fixture["minimum_samples"], unit="clean_correction_boluses",
        data_through=as_of, as_of=as_of,
    )

    assert {key: quality[key] for key in fixture["expected"]} == fixture["expected"]
    assert any("coverage" in reason for reason in quality["exclusion_reasons"])


def test_report_ai_prompt_omits_low_quality_metric_values(monkeypatch):
    as_of = date(2026, 7, 20)
    low_cgm = build_envelope(
        "cgm", observed=1, expected=100, unit="readings", data_through=as_of, as_of=as_of
    )
    good_pump = build_envelope(
        "pump_tdd", observed=5, expected=5, unit="complete_days", data_through=as_of, as_of=as_of
    )
    bad_nutrition = build_envelope(
        "nutrition", observed=0, expected=5, unit="days_with_logs", data_through=None, as_of=as_of,
        blocking_reasons=("carbohydrate records are uninterpretable",),
    )
    low_cycle = build_envelope(
        "cycle", observed=0, expected=5, unit="days", data_through=None, as_of=as_of
    )
    low_wearable = build_envelope(
        "wearables", observed=0, expected=5, unit="days", data_through=None, as_of=as_of
    )
    captured = {}

    async def fake_llm(prompt, **_kwargs):
        captured["prompt"] = prompt
        return {}

    monkeypatch.setattr(report, "invoke_llm", fake_llm)
    payload = {
        "days": 5,
        "glucose": {"available": True, "avg": 999, "quality": low_cgm},
        "insulin": {
            "available": True, "avg_tdd_est": 25, "avg_daily_carbs": 777,
            "quality": good_pump, "nutrition_quality": bad_nutrition,
        },
        "cycle": {"available": True, "cycles_detected": 1, "quality": low_cycle},
        "wellness": {"oura": {"avg_sleep_score": 999}, "fitbit": None, "quality": {"oura": low_wearable, "fitbit": low_wearable}},
        "labs": {"flagged": []},
    }

    asyncio.run(report._narrative(payload))

    prompt = captured["prompt"]
    assert '"avg": 999' not in prompt
    assert '"avg_daily_carbs": 777' not in prompt
    assert '"avg_sleep_score": 999' not in prompt
    assert "carbohydrate records are uninterpretable" in prompt


def test_demo_fallback_cannot_bypass_quality_exclusions():
    as_of = date(2026, 7, 20)
    good = build_envelope(
        "cgm", observed=100, expected=100, unit="readings", data_through=as_of, as_of=as_of
    )
    low = build_envelope(
        "pump_tdd", observed=0, expected=5, unit="complete_days", data_through=None, as_of=as_of
    )
    payload = {
        "glucose": {"quality": good},
        "insulin": {"quality": low, "nutrition_quality": {**good, "domain": "nutrition"}},
        "cycle": {"quality": {**good, "domain": "cycle"}},
        "wellness": {"quality": {"oura": {**good, "domain": "wearables"}}},
    }

    assert report._demo_narrative_allowed(payload) is False
    payload["insulin"]["quality"] = {**good, "domain": "pump_tdd"}
    assert report._demo_narrative_allowed(payload) is True
