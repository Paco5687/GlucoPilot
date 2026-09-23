"""Correction response: temp-basal and bolus corrections measured the same way."""

from datetime import datetime, timedelta, timezone

import pytest

from server import correction_response as cr
from server import glooko

pytestmark = pytest.mark.risk_critical

BASE = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _cgm(start_value=150, drop_per_point=1.0, hours=4):
    return [{"timestamp": _iso(BASE + timedelta(minutes=5 * i)), "value": start_value - i * drop_per_point}
            for i in range(hours * 12)]


def _temp(minutes_offset=0, rate=4.58, multiplier=1.95, duration=90):
    return {"event_type": "Temp Basal", "type": "tempbasal", "timestamp": _iso(BASE + timedelta(minutes=minutes_offset)),
            "absolute": rate, "multiplier": multiplier, "duration": duration}


def test_temp_basal_extra_units_and_response():
    # 4.58 U/hr at 1.95x for 90 min -> extra = 4.58*(1-1/1.95)*1.5 = 3.35 U
    episodes = cr.build_correction_episodes([_temp()], _cgm(150, 1.0))
    assert len(episodes) == 1
    e = episodes[0]
    assert e["method"] == "temp_basal"
    assert e["units"] == pytest.approx(3.35, abs=0.01)
    assert e["start_glucose"] == 150
    # 3 h window at -1 mg/dL per 5 min -> nadir 150-35=115 -> 35 mg/dL over 3.35 U
    assert e["nadir_glucose"] == 115
    assert e["drop_per_unit"] == pytest.approx(35 / 3.35, abs=0.1)
    assert e["confounded"] is False


def test_contiguous_temp_segments_merge_into_one_episode():
    segs = [_temp(0, duration=30), _temp(35, duration=30), _temp(70, duration=30)]
    episodes = cr.build_correction_episodes(segs, _cgm())
    assert len(episodes) == 1
    assert episodes[0]["units"] == pytest.approx(3 * 4.58 * (1 - 1 / 1.95) * 0.5, abs=0.01)


def test_scheduled_segments_without_multiplier_are_not_corrections():
    scheduled = {**_temp(), "multiplier": None}
    assert cr.build_correction_episodes([scheduled], _cgm()) == []


def test_standalone_bolus_is_a_correction_but_meal_bolus_is_not():
    bolus = {"event_type": "Bolus", "type": "insulin", "timestamp": _iso(BASE), "amount": 2.0}
    carbs = {"type": "carb", "event_type": "Carbs", "timestamp": _iso(BASE + timedelta(minutes=10)), "amount": 40.0}
    assert [e["method"] for e in cr.build_correction_episodes([bolus], _cgm())] == ["bolus"]
    assert cr.build_correction_episodes([bolus, carbs], _cgm()) == []


def test_carbs_inside_the_response_window_confound_but_keep_the_episode():
    carbs = {"type": "carb", "event_type": "Carbs", "timestamp": _iso(BASE + timedelta(minutes=100)), "amount": 20.0}
    episodes = cr.build_correction_episodes([_temp(), carbs], _cgm())
    assert len(episodes) == 1 and episodes[0]["confounded"] is True
    assert cr.summarize(episodes)["n_clean"] == 0


def test_weekly_series_splits_by_method_and_starting_glucose():
    treatments = [_temp(0), _temp(24 * 60)]  # two temp basals a day apart
    treatments.append({"event_type": "Bolus", "type": "insulin", "timestamp": _iso(BASE + timedelta(hours=6)), "amount": 1.5})
    cgm = []
    for day in range(2):
        for i in range(12 * 24):
            cgm.append({"timestamp": _iso(BASE + timedelta(days=day, minutes=5 * i)), "value": 160 - (i % 36)})
    episodes = cr.build_correction_episodes(treatments, cgm)
    series = cr.weekly_correction_series(episodes, end=BASE.date() + timedelta(days=3), weeks=2)
    assert len(series) == 1
    week = series[0]
    assert week["n_temp_basal"] == 2 and week["n_bolus"] == 1
    assert week["units_temp_basal"] > week["units_bolus"]
    assert week["drop_per_unit_from_high"] is not None  # all starts >= 150 here
    assert week["confident"] is False  # 3 episodes < 5


def test_glooko_temporary_basal_maps_with_multiplier_and_minutes():
    mapped = glooko._map_temporary_basal({
        "pumpTimestamp": "2026-09-19T07:50:43.000Z", "duration": 5400, "percentage": 1.95, "rate": 4.58, "guid": "abc",
    })
    assert mapped["event_type"] == "Temp Basal"
    assert mapped["multiplier"] == 1.95 and mapped["absolute"] == 4.58
    assert mapped["duration"] == 90.0
    assert mapped["ns_id"] == "glooko-tempbasal-abc"
