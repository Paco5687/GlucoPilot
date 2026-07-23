from __future__ import annotations

import pytest

from server.evidence_bundle import _confidence as evidence_confidence
from server.glucose_reconciliation import (
    ReconciliationInputError,
    capture_context,
    pair_fields,
    summarize,
)

pytestmark = pytest.mark.risk_critical


def _pair(index: int, meter: float, cgm: float, **context):
    timestamp = f"2026-07-{index + 1:02d}T12:00:00Z"
    return {
        "id": f"fingerstick-{index}",
        "timestamp": timestamp,
        "value": meter,
        **pair_fields(
            meter,
            timestamp,
            {
                "id": f"cgm-{index}",
                "timestamp": f"2026-07-{index + 1:02d}T12:02:00Z",
                "value": cgm,
                "source": "synthetic-cgm",
                "trend": "Flat",
            },
        ),
        **capture_context(context),
    }


def test_pair_preserves_both_values_and_derives_fixed_comparison_fields():
    pair = _pair(
        0,
        65,
        92,
        timing_context="symptoms",
        sensor_day=2,
        sensor_site="arm",
        activity="resting",
        position="lying",
        hydration="usual",
        compression_possible=True,
    )

    assert pair["value"] == 65
    assert pair["cgm_value"] == 92
    assert pair["delta"] == 27
    assert pair["absolute_difference_mg_dl"] == 27
    assert pair["relative_difference_percent"] == 41.5
    assert pair["pair_offset_seconds"] == 120
    assert pair["directional_difference"] == "cgm_high"
    assert pair["low_classification"] == "meter_only_low"
    assert pair["severe_low_classification"] == "neither_low"
    assert pair["compression_possible"] is True


def test_low_reconciliation_separates_confirmed_and_cgm_only_lows():
    summary = summarize(
        [
            _pair(0, 62, 60),
            _pair(1, 102, 58),
            _pair(2, 52, 51),
            _pair(3, 49, 90),
        ]
    )

    lows = summary["low_reconciliation"]
    assert lows["paired_checks"] == 4
    assert lows["confirmed_low"] == 2
    assert lows["cgm_only_low"] == 1
    assert lows["meter_only_low"] == 1
    assert lows["confirmed_severe_low"] == 1
    assert lows["meter_only_severe_low"] == 1
    assert "do not correct" in lows["caveat"]


def test_persistent_bias_requires_sample_size_and_interval_excluding_zero():
    too_few = summarize([_pair(index, 100, 125) for index in range(4)])
    assert too_few["persistent_bias"]["classification"] == "insufficient_sample"
    assert too_few["persistent_bias"]["minimum_sample_count"] == 5

    enough = summarize([_pair(index, 100 + index, 125 + index) for index in range(5)])
    assert enough["persistent_bias"]["classification"] == "cgm_high"
    assert enough["persistent_bias"]["sample_count"] == 5
    assert enough["strata"]["trend"][0]["sample_count"] == 5


def test_context_validation_is_bounded():
    with pytest.raises(ReconciliationInputError, match="sensor_day"):
        capture_context({"sensor_day": 31})
    with pytest.raises(ReconciliationInputError, match="compression_possible"):
        capture_context({"compression_possible": "maybe"})
    with pytest.raises(ReconciliationInputError, match="context_note"):
        capture_context({"context_note": "x" * 501})


def test_evidence_bundle_confidence_preserves_pairing_semantics():
    pair = _pair(0, 100, 125)
    confidence = evidence_confidence("FingerstickReading", pair)

    assert confidence["method"] == "glucose-reconciliation/1.0.0"
    assert confidence["label"] == "not_assessed"
    assert any("separate observations" in item for item in confidence["limitations"])
    assert any("does not establish" in item for item in confidence["limitations"])
