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
    return {"event_type": "Temp Basal", "type": "tempbasal_correction", "timestamp": _iso(BASE + timedelta(minutes=minutes_offset)),
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



def _climb_then_fall(day):
    """Rising 2 mg/dL per 5 min into the start (154 -> 160), on to a 168 peak
    20 min in, then falling 2 per 5 min for the rest of the window."""
    t0 = BASE + timedelta(days=day)
    rows = []
    for i in range(-6, 48):
        value = 160 + 2 * i if i <= 4 else 168 - 2 * (i - 4)
        rows.append({"timestamp": _iso(t0 + timedelta(minutes=5 * i)), "value": value})
    return rows


def _temp_on(day, **kwargs):
    return {**_temp(**kwargs), "id": f"temp-{day}",
            "timestamp": _iso(BASE + timedelta(days=day))}


def test_episodes_carry_their_source_treatments_and_length():
    e = cr.build_correction_episodes([{**_temp(), "id": "temp-1"}], _cgm())[0]
    assert e["source_ids"] == ["temp-1"] and e["minutes"] == 90


def test_timecourse_measures_how_soon_glucose_falls_after_a_temp_basal():
    treatments = [_temp_on(day) for day in range(5)]
    readings = [row for day in range(5) for row in _climb_then_fall(day)]
    episodes = cr.build_correction_episodes(treatments, readings)
    course = cr.timecourse(episodes, readings, method="temp_basal")
    assert course["n"] == 5 and course["enough"] is True
    assert course["median_start_glucose"] == 160
    # 168 - 2*(i-4) <= 150 first at i=13: 65 minutes after the start.
    assert course["median_minutes_to_fall"] == 65
    assert course["fell_within_60_pct"] == 0 and course["fell_within_120_pct"] == 100
    assert course["delta_30"] == 4 and course["delta_60"] == -8 and course["delta_120"] == -32
    assert course["rose_first_pct"] == 0          # an 8 mg/dL climb is under the 10 mg/dL bar
    rising = course["rising_at_start"]
    assert rising["n"] == 5 and rising["enough"] is True
    assert rising["median_further_rise"] == 8 and rising["median_minutes_to_peak"] == 20
    assert rising["median_minutes_to_fall"] == 65


def test_timecourse_leaves_out_low_starts_confounded_and_other_methods():
    treatments = [_temp_on(0), _temp_on(1)]
    treatments.append({"type": "carb", "event_type": "Carbs", "amount": 30,
                       "timestamp": _iso(BASE + timedelta(days=1, minutes=60))})
    readings = [*_climb_then_fall(0), *_climb_then_fall(1)]
    episodes = cr.build_correction_episodes(treatments, readings)
    assert cr.timecourse(episodes, readings)["n"] == 1
    assert cr.timecourse(episodes, readings, min_start_glucose=170)["n"] == 0
    assert cr.timecourse(episodes, readings, method="bolus")["n"] == 0
    assert cr.timecourse(episodes, readings)["enough"] is False


def test_correction_item_reports_the_time_course_in_plain_findings():
    from datetime import date

    from server import analysis_evidence

    treatments = [_temp_on(day) for day in range(6)]
    readings = [row for day in range(6) for row in _climb_then_fall(day)]
    episodes = cr.build_correction_episodes(treatments, readings)
    item = analysis_evidence.correction_item(
        episodes, readings, start=date(2026, 9, 1), end=date(2026, 9, 6)
    )
    findings = item["data"]["findings"]
    assert findings[0] == (
        "After 6 temp basal corrections started at or above 150 mg/dL, glucose was 10 mg/dL below where it "
        "started within 1 hour in 0% and within 2 hours in 100%; half got there by 65 minutes, and in 0% it had "
        "not within 3 hours."
    )
    assert "1 hour -8, 90 minutes -20, 2 hours -32" in findings[1]
    assert findings[2].startswith("These corrections started at a median of 160 mg/dL, ran a median of 90 minutes")
    assert any(f.startswith("When glucose was still climbing as the temp basal started (6 of 6)") for f in findings)
    assert item["data"]["sample_count"] == 6
    assert item["confidence"]["limitations"]
    assert item["source_refs"] == [("Treatment", f"temp-{day}") for day in range(6)]


def test_correction_item_with_too_few_corrections_says_so():
    from datetime import date

    from server import analysis_evidence

    item = analysis_evidence.correction_item([], [], start=date(2026, 9, 1), end=date(2026, 9, 6))
    assert item["data"]["findings"] == [
        "Only 0 clean temp basal corrections started at or above 150 mg/dL in this window, "
        "too few to describe a typical time course."
    ]
    assert item["source_refs"] == []

def test_contiguous_temp_segments_merge_into_one_episode():
    segs = [_temp(0, duration=30), _temp(35, duration=30), _temp(70, duration=30)]
    episodes = cr.build_correction_episodes(segs, _cgm())
    assert len(episodes) == 1
    assert episodes[0]["units"] == pytest.approx(3 * 4.58 * (1 - 1 / 1.95) * 0.5, abs=0.01)


def test_scheduled_segments_are_not_corrections():
    # A scheduled segment at a raised rate looks like a temp basal but is not one.
    scheduled = {**_temp(), "type": "tempbasal", "multiplier": None}
    assert cr.build_correction_episodes([scheduled], _cgm()) == []
    still_scheduled = {**_temp(), "type": "tempbasal"}
    assert cr.build_correction_episodes([still_scheduled], _cgm()) == []


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
    assert mapped["event_type"] == "Temp Basal" and mapped["type"] == "tempbasal_correction"
    assert mapped["multiplier"] == 1.95 and mapped["absolute"] == 4.58
    assert mapped["duration"] == 90.0
    assert mapped["ns_id"] == "glooko-tempbasal-abc"


class TestCorrectionRowsStayOutOfBasalAccounting:
    @pytest.fixture
    def database(self, tmp_path, monkeypatch):
        from server import db
        from server.migrations import run_migrations

        path = tmp_path / "data" / "app.sqlite3"
        path.parent.mkdir()
        run_migrations(path)
        monkeypatch.setattr(db, "DB_PATH", path)
        return path

    def test_correction_is_not_deduped_against_a_scheduled_segment_at_the_same_instant(self, database):
        from server.config import OWNER_EMAIL

        at = "2026-09-19T07:50:43.000Z"
        scheduled = {"type": "tempbasal", "event_type": "Temp Basal", "timestamp": at, "absolute": 4.55,
                     "duration": 90.0, "source": "glooko", "ns_id": "glooko-seg-1", "owner_email": OWNER_EMAIL}
        correction = glooko._map_temporary_basal({"pumpTimestamp": at, "duration": 5400, "percentage": 1.95,
                                                  "rate": 4.58, "guid": "tb-1"})
        assert glooko._persist_treatments([scheduled]) == (1, 0, 0)
        # Before the type split this was (0, 1, 0): the correction vanished.
        assert glooko._persist_treatments([correction]) == (1, 0, 0)

    def test_reconciler_ignores_correction_rows(self):
        from zoneinfo import ZoneInfo

        from server.insulin_reconciliation import _basal_segments

        tz = ZoneInfo("America/New_York")
        rows = [
            {"type": "tempbasal", "timestamp": "2026-09-19T12:00:00.000Z", "duration": 60.0, "absolute": 2.35, "source": "glooko"},
            {"type": "tempbasal_correction", "timestamp": "2026-09-19T12:00:00.000Z", "duration": 60.0, "absolute": 4.58,
             "multiplier": 1.95, "source": "glooko"},
        ]
        segments, _, _ = _basal_segments(rows, tz)
        day = next(iter(segments))
        rates = [rate for cls in segments[day].values() for (_, _, rate) in cls]
        assert rates == [2.35]  # the correction never enters delivered/scheduled basal
