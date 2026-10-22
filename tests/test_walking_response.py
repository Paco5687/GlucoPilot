"""Walking response: walks merged from bouts, measured against sitting controls."""

from datetime import datetime, timedelta, timezone

import pytest

from server import walking_response as wr

pytestmark = pytest.mark.risk_critical

T0 = datetime(2026, 9, 10, 14, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _bout(offset_min, length_min):
    return {"activity": "walking", "id": f"bout-{offset_min}",
            "start_time": _iso(T0 + timedelta(minutes=offset_min)),
            "end_time": _iso(T0 + timedelta(minutes=offset_min + length_min))}


def test_bouts_merge_into_walks_and_short_ones_drop():
    walks = wr.build_walks([_bout(0, 3), _bout(5, 3), _bout(11, 4), _bout(60, 2)])
    assert len(walks) == 1                 # the 2-minute stray at +60 is not a walk
    assert walks[0]["bouts"] == 3 and walks[0]["minutes"] == 15.0
    assert walks[0]["interval_ids"] == ["bout-0", "bout-5", "bout-11"]


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


def test_walking_item_compares_sitting_and_walking_in_waking_hours_only():
    from datetime import date

    from server import analysis_evidence

    # T0 is 14:00 UTC; walks every four hours land at 14:00, 18:00, 22:00, 02:00...
    bouts = [_bout(i * 240, 20) for i in range(12)]
    readings = _cgm(150, 0, hours=50)
    measurement = wr.measure(bouts, readings, start=T0 - timedelta(hours=1), end=T0 + timedelta(hours=48))
    assert all(row["start"] for row in measurement["controls"])
    item = analysis_evidence.walking_item(
        measurement, start=date(2026, 9, 10), end=date(2026, 9, 12), timezone_name="UTC"
    )
    bands = item["data"]["supporting_data"]["bands"]["120_to_170"]
    def waking(row):
        return 7 <= int(row["start"][11:13]) < 22

    # The overnight walks and sitting checks are measured but left out of the item.
    assert any(not waking(row) for row in measurement["measured"])
    assert any(not waking(row) for row in measurement["controls"])
    assert bands["walking"]["n"] == sum(1 for row in measurement["measured"] if waking(row))
    assert bands["sitting"]["n"] == sum(1 for row in measurement["controls"] if waking(row))
    findings = item["data"]["findings"]
    assert findings[0] == (
        "Sitting or resting during the day, with no walking for the next 2 hours, glucose held steady over "
        "2 hours when it started between 120 and 170 mg/dL."
    )
    assert any(f.startswith("Sitting or resting, starting between 120 and 170 mg/dL: glucose was unchanged "
                            "at the median after 1 hour") for f in findings)
    assert any(f.startswith("After walking 8 minutes or more, starting between 120 and 170 mg/dL") for f in findings)
    assert item["confidence"]["limitations"]
    assert item["source_refs"][0] == ("ActivityPositionInterval", "bout-0")
    assert len(item["source_refs"]) <= analysis_evidence.MAX_SOURCE_REFS


def test_walking_item_without_enough_walks_says_so():
    from datetime import date

    from server import analysis_evidence

    measurement = wr.measure([], [], start=T0, end=T0 + timedelta(hours=4))
    item = analysis_evidence.walking_item(measurement, start=date(2026, 9, 10), end=date(2026, 9, 10))
    assert item["data"]["findings"][0].startswith("Too few measured walks (0)")


def _check(start_glucose, delta_120, hour=15):
    return {"start": f"2026-09-10T{hour:02d}:00:00Z", "start_glucose": start_glucose, "nadir_delta": min(0, delta_120),
            "went_low": False, "delta_15": 0, "delta_30": 0, "delta_60": delta_120 // 2,
            "delta_90": delta_120, "delta_120": delta_120}


def test_walking_item_leads_with_the_direction_glucose_took_while_sitting():
    from datetime import date

    from server import analysis_evidence

    controls = [_check(95, 20) for _ in range(6)] + [_check(150, -24) for _ in range(6)]
    controls += [_check(150, 30, hour=3) for _ in range(6)]  # overnight: not "sitting down"
    item = analysis_evidence.walking_item(
        {"walks": [], "measured": [], "controls": controls},
        start=date(2026, 9, 10), end=date(2026, 9, 10), timezone_name="UTC",
    )
    assert item["data"]["findings"][:3] == [
        "Sitting or resting during the day, with no walking for the next 2 hours, glucose rose over 2 hours "
        "when it started below 120 mg/dL; it fell over 2 hours when it started between 120 and 170 mg/dL.",
        "Sitting or resting, starting below 120 mg/dL: glucose rose a median of 10 mg/dL after 1 hour and "
        "rose a median of 20 mg/dL after 2 hours (6 daytime checks).",
        "Sitting or resting, starting between 120 and 170 mg/dL: glucose fell a median of 12 mg/dL after 1 hour "
        "and fell a median of 24 mg/dL after 2 hours (6 daytime checks).",
    ]


def test_walking_item_states_which_activity_had_more_lows():
    from datetime import date

    from server import analysis_evidence

    def walk(went_low):
        return {**_check(95, 0), "went_low": went_low, "minutes": 20, "hour": 15}

    measured = [walk(True) for _ in range(3)] + [walk(False) for _ in range(2)]
    controls = [{**_check(95, 0), "went_low": index == 0} for index in range(5)]
    item = analysis_evidence.walking_item(
        {"walks": [], "measured": measured, "controls": controls},
        start=date(2026, 9, 10), end=date(2026, 9, 10), timezone_name="UTC",
    )
    assert item["data"]["findings"][-1] == (
        "Lows were more common after walking than after sitting when glucose started below 120 mg/dL: it went "
        "below 70 within 2 hours after 60% of walks and after 20% of sitting periods."
    )
