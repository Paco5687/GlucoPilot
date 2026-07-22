from __future__ import annotations

import json
from pathlib import Path

import pytest

from server import db, health_summary
from server.analytics_confidence import (
    ANALYTICS_CONFIDENCE_VERSION,
    DISCOVERY_STATUSES,
    comparison_confidence,
    correlation_confidence,
    phase_provenance,
    proportion_confidence,
    safe_analytics_text,
)
from server.evidence_bundle import _confidence as evidence_confidence
from server.insights import _correlation_candidates
from server.migrations import run_migrations


pytestmark = pytest.mark.risk_critical

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "golden" / "analytics_confidence.json"


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _pairs(case: dict) -> list[tuple[str, float, float]]:
    output = []
    for index in range(1, case["days"] + 1):
        if case["relationship"] == "positive_linear":
            x, y = index, index * 2
        elif case["relationship"] == "positive_then_negative":
            x, y = index, index if index <= 14 else -index
        else:
            x, y = 1, index
        output.append((f"2026-01-{index:02d}", x, y))
    return output


def _interval(envelope: dict) -> list[float] | None:
    interval = envelope["confidence_interval"]
    return [interval["lower"], interval["upper"]] if interval else None


def test_golden_correlation_status_effect_interval_and_replication(golden):
    assert golden["synthetic"] is True
    assert golden["version"] == ANALYTICS_CONFIDENCE_VERSION
    encoded = json.dumps(golden, sort_keys=True).lower()
    assert "@" not in encoded
    assert not any(secret in encoded for secret in (
        "access_token", "refresh_token", "api_key", "client_secret", "password"
    ))
    assert set(DISCOVERY_STATUSES) == {
        "exploratory", "emerging", "reproduced", "not-reproduced", "invalid"
    }

    for case in golden["correlations"]:
        effect, envelope = correlation_confidence(
            _pairs(case), expected_days=case["days"]
        )
        expected = case["expected"]
        actual_effect = round(effect, 4) if effect is not None else None
        assert actual_effect == expected["effect"]
        assert envelope["effect_size"]["value"] == expected["effect"]
        assert _interval(envelope) == expected["interval"]
        assert envelope["discovery_status"] == expected["status"]
        assert envelope["confidence_score"] == expected["confidence_score"]
        assert envelope["replication"]["status"] == expected["replication_status"]
        assert envelope["language"]["definitive_allowed"] is False
        assert envelope["language"]["causal_allowed"] is False


def test_seven_day_insight_candidate_is_explicitly_exploratory(golden):
    case = golden["correlations"][0]
    glucose = {
        day: {"tir": y, "avg": 120.0, "cv": 30.0}
        for day, _, y in _pairs(case)
    }
    wearables = {
        day: {"sleep_score": x}
        for day, x, _ in _pairs(case)
    }

    candidates = _correlation_candidates(glucose, wearables)

    candidate = next(item for item in candidates if item["x"] == "Sleep score (Oura)")
    assert candidate["n"] == 7
    assert candidate["analytics_confidence"]["discovery_status"] == "exploratory"


def test_small_comparison_and_recurrence_cannot_claim_definitive_strength(golden):
    comparison = golden["small_comparison"]
    envelope = comparison_confidence(
        comparison["group_a"],
        comparison["group_b"],
        valid_days=comparison["valid_days"],
        expected_days=comparison["expected_days"],
        unit="percentage_points",
    )
    expected = comparison["expected"]
    assert envelope["sample_count"] == expected["sample_count"]
    assert envelope["effect_size"]["value"] == expected["effect"]
    assert envelope["effect_size"]["raw_difference"] == expected["raw_difference"]
    assert _interval(envelope) == expected["interval"]
    assert envelope["missingness"]["missing_rate"] == expected["missing_rate"]
    assert envelope["discovery_status"] == expected["status"]
    assert envelope["confidence_score"] == expected["confidence_score"]

    recurrence = golden["small_recurrence"]
    envelope = proportion_confidence(
        recurrence["successes"],
        recurrence["trials"],
        valid_days=recurrence["valid_days"],
        expected_days=recurrence["expected_days"],
    )
    expected = recurrence["expected"]
    assert envelope["effect_size"]["value"] == expected["effect"]
    assert _interval(envelope) == expected["interval"]
    assert envelope["discovery_status"] == expected["status"]
    assert envelope["confidence_score"] == expected["confidence_score"]
    assert "definitive" not in safe_analytics_text(
        "This definitive result proves causality.", envelope, "Five recurrences were observed."
    ).lower()


def test_cycle_phase_provenance_and_evidence_confidence_are_preserved(golden):
    fixture = golden["cycle_phases"]
    provenance = phase_provenance(fixture["rows"])
    expected = fixture["expected"]
    assert {key: provenance[key] for key in (
        "classification", "confirmed_days", "inferred_days", "total_days"
    )} == {key: expected[key] for key in (
        "classification", "confirmed_days", "inferred_days", "total_days"
    )}
    assert provenance["by_phase"]["menstrual"] == expected["menstrual"]
    assert provenance["by_phase"]["follicular"] == expected["follicular"]

    analytics = proportion_confidence(5, 5, valid_days=5, expected_days=14)
    public = evidence_confidence({"analytics_confidence": analytics})
    assert public == {
        "label": "low",
        "score": 0.353,
        "method": ANALYTICS_CONFIDENCE_VERSION,
        "discovery_status": "exploratory",
    }


def test_health_summary_ai_context_requires_and_preserves_numerical_metadata(
    golden, tmp_path, monkeypatch
):
    database = tmp_path / "app.sqlite3"
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    _, analytics = correlation_confidence(
        _pairs(golden["correlations"][0]), expected_days=7
    )
    quality = {"cgm": {"ai_eligible": True}}
    common = {
        "owner_email": "owner@glucopilot.local",
        "title": "Synthetic correlation",
        "category": "general",
        "severity": "info",
        "data_quality": quality,
        "supporting_data": json.dumps({"r": 1.0, "n": 7}),
    }
    db.create_entity("Insight", common)
    db.create_entity("Insight", {**common, "analytics_confidence": analytics})

    snapshot = health_summary._insights_snapshot()

    assert len(snapshot) == 1
    assert snapshot[0]["statistics"] == {"r": 1.0, "n": 7}
    assert snapshot[0]["analytics_confidence"] == analytics
