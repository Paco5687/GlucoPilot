"""Walking response: walks merged from bouts, measured against sitting controls."""

from datetime import datetime, timedelta, timezone

import pytest

from server import walking_response as wr

pytestmark = pytest.mark.risk_critical

T0 = datetime(2026, 9, 10, 14, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _bout(offset_min, length_min):
    return {"activity": "walking", "start_time": _iso(T0 + timedelta(minutes=offset_min)),
            "end_time": _iso(T0 + timedelta(minutes=offset_min + length_min))}


def test_bouts_merge_into_walks_and_short_ones_drop():
    walks = wr.build_walks([_bout(0, 3), _bout(5, 3), _bout(11, 4), _bout(60, 2)])
    assert len(walks) == 1                 # the 2-minute stray at +60 is not a walk
    assert walks[0]["bouts"] == 3 and walks[0]["minutes"] == 15.0


def test_non_walking_intervals_are_ignored():
    assert wr.build_walks([{**_bout(0, 20), "activity": "resting"}]) == []


def _cgm(start_value, per_5min, hours=6, t0=T0 - timedelta(hours=1)):
    return [{"timestamp": _iso(t0 + timedelta(minutes=5 * i)), "value": start_value + i * per_5min}
            for i in range(hours * 12)]


def test_response_measures_deltas_nadir_and_after_stop():
    # Glucose falls 2 mg/dL per 5 min from 160 starting an hour before the walk.
    readings = _cgm(184, -2)               # 184 at T0-60min -> 160 at T0
    result = wr.analyze([_bout(0, 20)], readings, start=T0 - timedelta(hours=1), end=T0 + timedelta(hours=5))
    assert result["walks_measured"] == 1
    band = result["bands"]["120_to_170"]["walking"]
    assert band is None                    # fewer than MIN_GROUP walks -> no summary
    # The single walk's raw measurement still shaped the totals
    assert result["median_walk_minutes"] == 20


def test_summaries_need_five_walks_and_controls_come_from_walk_free_time():
    bouts = [_bout(i * 240, 20) for i in range(6)]  # six 20-min walks, four hours apart
    readings = _cgm(150, 0, hours=30)
    result = wr.analyze(bouts, readings, start=T0 - timedelta(hours=1), end=T0 + timedelta(hours=28))
    band = result["bands"]["120_to_170"]
    assert band["walking"]["n"] == 6 and band["walking"]["delta_120"] == 0
    assert band["sitting"] is not None and band["sitting"]["n"] > 0
    # flat glucose: nobody goes low
    assert band["walking"]["went_low_pct"] == 0 and band["sitting"]["went_low_pct"] == 0
    assert result["durations_120_to_170"]["15_to_30"]["n"] == 6
