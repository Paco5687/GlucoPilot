from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from server.insulin_reconciliation import RECONCILIATION_VERSION, reconcile_treatments
from server import report


pytestmark = pytest.mark.risk_critical
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "golden" / "pump_reconciliation.json"


@pytest.fixture(scope="module")
def pump_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _reconcile(case: dict) -> tuple[dict, dict]:
    result = reconcile_treatments(case["treatments"], ZoneInfo("UTC"))
    assert result["algorithm_version"] == RECONCILIATION_VERSION
    assert result["input_data_version"].startswith("sha256:")
    assert len(result["days"]) == 1
    return result, result["days"][0]


def test_fixture_is_explicitly_synthetic_and_public_safe(pump_fixture):
    encoded = json.dumps(pump_fixture, sort_keys=True)
    assert pump_fixture["synthetic"] is True
    assert pump_fixture["subject"]["id"].startswith("synthetic-")
    assert set(row["owner_email"] for case in pump_fixture["cases"].values() for row in case["treatments"]) == {
        "owner@glucopilot.local"
    }
    assert "password" not in encoded.lower()
    assert "token" not in encoded.lower()


def test_complete_reported_and_calculated_totals_remain_separate(pump_fixture):
    case = pump_fixture["cases"]["complete_and_matching"]
    result, day = _reconcile(case)
    assert result == reconcile_treatments(list(reversed(case["treatments"])), ZoneInfo("UTC"))
    expected = case["expected"]
    assert day["completeness"] == expected["completeness"]
    assert day["pump_reported"]["selected"]["total_units"] == expected["pump_reported_total"]
    assert day["calculated"]["total_units"] == expected["calculated_total"]
    assert day["calculated"]["delivered_basal_units"] == expected["delivered_basal"]
    assert day["calculated"]["delivered_basal_coverage_pct"] == expected["coverage_pct"]
    assert day["discrepancy"]["units"] == expected["discrepancy_units"]
    assert result["summary"]["pump_reported_days"] == 1
    assert result["summary"]["calculated_days"] == 1


def test_reported_vs_calculated_difference_is_explicit(pump_fixture):
    case = deepcopy(pump_fixture["cases"]["complete_and_matching"])
    case["treatments"][-1]["notes"] = "Bolus: 6U | Basal: 24U | Total: 30U"
    result, day = _reconcile(case)
    assert day["pump_reported"]["selected"]["total_units"] == 30
    assert day["calculated"]["total_units"] == 28
    assert day["discrepancy"] == {
        "units": -2.0,
        "absolute_units": 2.0,
        "percent_of_reported": 6.7,
        "matches_rounding": False,
    }
    assert result["summary"]["discrepancy_days"] == 1
    assert result["summary"]["discrepancies"][0]["date"] == "2026-01-10"


def test_internally_inconsistent_reported_total_is_excluded(pump_fixture):
    case = deepcopy(pump_fixture["cases"]["complete_and_matching"])
    case["treatments"] = [case["treatments"][-1]]
    case["treatments"][0]["notes"] = "Bolus: 4U | Basal: 24U | Total: 31U"
    result, day = _reconcile(case)
    candidate = day["pump_reported"]["candidates"][0]
    assert candidate["fields_complete"] is True
    assert candidate["components_match"] is False
    assert candidate["complete"] is False
    assert day["pump_reported"]["selected"] is None
    assert day["completeness"] == "incomplete_reported_total"
    assert result["summary"]["pump_reported_days"] == 0


def test_partial_delivered_basal_never_becomes_tdd(pump_fixture):
    case = pump_fixture["cases"]["partial_delivery"]
    _, day = _reconcile(case)
    expected = case["expected"]
    assert day["completeness"] == expected["completeness"]
    assert day["calculated"]["total_units"] is expected["calculated_total"]
    assert day["calculated"]["delivered_basal_units"] == expected["delivered_basal"]
    assert day["calculated"]["delivered_basal_coverage_pct"] == expected["coverage_pct"]


def test_glooko_schedule_is_not_claimed_as_delivered(pump_fixture):
    case = pump_fixture["cases"]["scheduled_not_delivered"]
    result, day = _reconcile(case)
    expected = case["expected"]
    assert day["completeness"] == expected["completeness"]
    assert day["calculated"]["total_units"] is expected["calculated_total"]
    assert day["scheduled_basal"]["units"] == expected["scheduled_basal"]
    assert day["scheduled_basal"]["coverage_pct"] == expected["scheduled_coverage_pct"]
    assert "not confirmed delivery" in " ".join(result["summary"]["limitations"])


def test_conflicting_authoritative_totals_are_not_silently_selected(pump_fixture):
    case = pump_fixture["cases"]["conflicting_reported_totals"]
    _, day = _reconcile(case)
    expected = case["expected"]
    assert day["completeness"] == expected["completeness"]
    assert day["pump_reported"]["selected"] is expected["selected"]
    assert day["pump_reported"]["conflict"] is expected["conflict"]
    assert [candidate["total_units"] for candidate in day["pump_reported"]["candidates"]] == expected[
        "candidate_totals"
    ]


def test_provider_and_cross_source_duplicates_are_suppressed(pump_fixture):
    case = pump_fixture["cases"]["duplicates"]
    result, day = _reconcile(case)
    expected = case["expected"]
    assert day["pump_reported"]["selected"]["total_units"] == expected["pump_reported_total"]
    assert day["calculated"]["total_units"] == expected["calculated_total"]
    assert day["calculated"]["bolus_units"] == expected["bolus_units"]
    assert result["summary"]["duplicates_suppressed"] == expected["duplicates_suppressed"]


def test_bolus_without_delivered_basal_stays_incomplete(pump_fixture):
    case = pump_fixture["cases"]["missing_basal"]
    result, day = _reconcile(case)
    expected = case["expected"]
    assert day["completeness"] == expected["completeness"]
    assert day["calculated"]["total_units"] is expected["calculated_total"]
    assert day["calculated"]["bolus_units"] == expected["bolus_units"]
    assert result["summary"]["pump_reported_avg_tdd"] is None
    assert result["summary"]["calculated_avg_tdd"] is None


def test_overlapping_conflicting_delivery_rates_are_incomplete():
    treatments = [
        {"id": "synthetic-overlap-a", "source": "tandem", "type": "tempbasal", "timestamp": "2026-01-16T00:00:00Z", "duration": 1440, "absolute": 1},
        {"id": "synthetic-overlap-b", "source": "tandem", "type": "tempbasal", "timestamp": "2026-01-16T12:00:00Z", "duration": 720, "absolute": 2},
    ]
    result = reconcile_treatments(treatments, ZoneInfo("UTC"))
    day = result["days"][0]
    assert day["completeness"] == "partial_delivered_basal"
    assert day["calculated"]["delivered_basal_conflict"] is True
    assert day["calculated"]["total_units"] is None


def test_visit_report_never_substitutes_scheduled_basal_for_tdd(pump_fixture, monkeypatch):
    case = pump_fixture["cases"]["scheduled_not_delivered"]
    monkeypatch.setattr(report, "_paged", lambda entity_type, since: case["treatments"])
    output = report._insulin(ZoneInfo("UTC"), "2026-01-12T00:00:00Z", 1)
    assert output["pump_reported_avg_tdd"] is None
    assert output["calculated_avg_tdd"] is None
    assert output["avg_tdd_est"] is None
    assert output["scheduled_avg_daily_basal"] == 19.2
    assert output["incomplete_days"] == 1


def test_visit_report_keeps_matching_sources_separate(pump_fixture, monkeypatch):
    case = pump_fixture["cases"]["complete_and_matching"]
    monkeypatch.setattr(report, "_paged", lambda entity_type, since: case["treatments"])
    output = report._insulin(ZoneInfo("UTC"), "2026-01-10T00:00:00Z", 1)
    assert output["pump_reported_avg_tdd"] == 28
    assert output["pump_reported_sources"] == {"tandem": 1}
    assert output["calculated_avg_tdd"] == 28
    assert output["calculated_source"] == "tandem_delivered"
    assert output["calculated_avg_daily_basal"] == 24


def test_dst_day_uses_local_day_length_for_complete_coverage():
    treatments = [
        {
            "id": "synthetic-dst-basal",
            "source": "tandem",
            "type": "tempbasal",
            "timestamp": "2026-03-08T05:00:00Z",
            "duration": 23 * 60,
            "absolute": 1,
        }
    ]
    result = reconcile_treatments(treatments, ZoneInfo("America/New_York"))
    day = result["days"][0]
    assert day["date"] == "2026-03-08"
    assert day["calculated"]["delivered_basal_coverage_pct"] == 100
    assert day["calculated"]["delivered_basal_units"] == 23
    assert day["calculated"]["total_units"] == 23
