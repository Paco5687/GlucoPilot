"""Glooko daily insulin totals — the only complete insulin figure available.

On an Omnipod 5 the v2 event streams carry the programmed schedule and manual
boluses only, missing over half the day. These totals come from the v3 graph
API and are what makes a real TDD possible, so the mapping is pinned here.
"""

from __future__ import annotations

import pytest

from server import glooko
from server.typed_treatments import parse_pump_daily_total

pytestmark = pytest.mark.risk_critical


def test_note_format_round_trips_through_the_reconciler_parser():
    mapped = glooko._map_daily_total({"date": "2026-08-03", "total": 36.1, "basal": 35.6, "bolus": 0.5})

    assert mapped["event_type"] == "Daily Total"
    assert mapped["type"] == "insulin"
    assert mapped["source"] == "glooko"
    # The reconciler reads the day label off the ISO prefix, so it must equal
    # the local date rather than drifting to a neighbouring day.
    assert mapped["timestamp"][:10] == "2026-08-03"

    parsed = parse_pump_daily_total(mapped["notes"])
    assert parsed["completeness"] == "complete"
    assert parsed["total_units"] == 36.1
    assert parsed["bolus_units"] == 0.5


def test_components_are_made_to_sum_exactly():
    """Glooko rounds each field independently; the note must still reconcile.

    Left alone, basal+bolus can miss the total by 0.1 U and trip the
    reconciler's 0.05 component check — which silently drops the day.
    """
    # Glooko's own numbers here sum to 36.7, not the stated 36.6.
    mapped = glooko._map_daily_total({"date": "2026-08-01", "total": 36.6, "basal": 36.2, "bolus": 0.5})
    parsed = parse_pump_daily_total(mapped["notes"])

    assert parsed["completeness"] == "complete"
    assert parsed["total_units"] == 36.6
    assert parsed["bolus_units"] == 0.5
    # basal is derived from the authoritative total, so the components agree.
    assert parsed["basal_units"] == 36.1
    assert abs(parsed["basal_units"] + parsed["bolus_units"] - parsed["total_units"]) <= 0.05


def test_total_without_a_bolus_component_still_maps():
    mapped = glooko._map_daily_total({"date": "2026-08-04", "total": 35.7, "basal": None, "bolus": None})
    parsed = parse_pump_daily_total(mapped["notes"])

    assert parsed["total_units"] == 35.7
    assert parsed["completeness"] == "partial"


def test_missing_total_maps_to_nothing():
    assert glooko._map_daily_total({"date": "2026-08-04", "total": None, "basal": 1, "bolus": 2}) is None


def test_ns_id_is_stable_per_day_so_resync_does_not_duplicate():
    row = {"date": "2026-08-05", "total": 42.3, "basal": 41.8, "bolus": 0.6}
    assert glooko._map_daily_total(row)["ns_id"] == glooko._map_daily_total(row)["ns_id"]
    assert glooko._map_daily_total(row)["ns_id"] == "glooko-dailytotal-2026-08-05"


def test_us_graph_host_is_region_prefixed(monkeypatch):
    # The v3 graph API answers on the region-prefixed host the web app uses.
    monkeypatch.setattr(glooko, "_region", lambda: "us")
    assert glooko._graph_base_url() == "https://us.api.glooko.com"
    monkeypatch.setattr(glooko, "_region", lambda: "eu")
    assert glooko._graph_base_url() == "https://eu.api.glooko.com"
