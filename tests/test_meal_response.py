"""Meal response: rise onset and peak after logged meals, readable only without insulin action."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from server import db, food
from server import meal_response as mr
from server.activity_position import IntervalBody, SqliteActivityPositionRepository
from server.migrations import run_migrations
from server.repositories import get_repositories

pytestmark = pytest.mark.risk_critical

T0 = datetime(2026, 9, 10, 18, 0, tzinfo=timezone.utc)
RANGE = {"start": T0 - timedelta(days=1), "end": T0 + timedelta(days=10)}


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _cgm(shape, t0=T0, before=60, after=240):
    """A 5-minute CGM trace; shape(minute relative to the meal) -> mg/dL."""
    return [{"timestamp": _iso(t0 + timedelta(minutes=m)), "value": shape(m)} for m in range(-before, after + 1, 5)]


def _meal(t=T0, **extra):
    return {"id": f"meal-{t.isoformat()}", "type": "carb", "source": "manual", "timestamp": _iso(t),
            "amount": 40, "food_name": "Rice bowl", **extra}


def _steady_then_rise(m):
    # flat 110 until +30, then +5 per 5 min up to 180 at +100, then falling back
    if m <= 30:
        return 110
    if m <= 100:
        return 110 + (m - 30)
    return 180 - (m - 100)


def _only(result):
    assert len(result["meals"]) == 1
    return result["meals"][0]


def test_steady_meal_measures_rise_start_peak_and_is_readable():
    meal = _only(mr.analyze([_meal()], _cgm(_steady_then_rise), [], **RANGE))
    assert meal["start_glucose"] == 110 and meal["trend_30"] == 0
    assert meal["nadir"] == 110 and meal["nadir_minutes"] == 0
    assert meal["rise_start_minutes"] == 40 and meal["rise_start_glucose"] == 120
    assert (meal["peak"], meal["peak_minutes"], meal["rise"]) == (180, 100, 70)
    assert meal["readable"] and meal["reasons"] == []
    assert not meal["temp_basal"] and not meal["bolus"] and not meal["low_before"] and meal["walking_minutes"] == 0


def test_rise_start_needs_the_next_reading_to_hold():
    def blip(m):
        return 125 if m == 15 else _steady_then_rise(m)   # a single spike is not the rise
    assert _only(mr.analyze([_meal()], _cgm(blip), [], **RANGE))["rise_start_minutes"] == 40


def test_rise_is_measured_from_the_low_point_in_the_first_90_minutes():
    def dip_then_rise(m):
        if m <= 0:
            return 120
        if m <= 40:
            return 120 - m // 2         # drifts down to 100 at +40
        return 100 + (m - 40)           # then climbs
    meal = _only(mr.analyze([_meal()], _cgm(dip_then_rise, before=60), [], **RANGE))
    assert meal["nadir"] == 100 and meal["nadir_minutes"] == 40
    assert meal["rise_start_minutes"] == 50 and meal["rise"] == meal["peak"] - 100


def test_falling_start_under_a_temp_basal_is_not_readable():
    treatments = [_meal(), {"type": "tempbasal_correction", "timestamp": _iso(T0 - timedelta(minutes=45)),
                            "duration": 120, "multiplier": 1.5}]
    meal = _only(mr.analyze(treatments, _cgm(lambda m: 200 - m if m <= 60 else 140 + (m - 60)), [], **RANGE))
    assert meal["trend_30"] == 30 * -1 and meal["temp_basal"]
    assert not meal["readable"] and set(meal["reasons"]) == {"falling", "temp_basal"}


def test_temp_basal_still_running_into_the_window_counts_but_an_earlier_finished_one_does_not():
    running = {"type": "tempbasal_correction", "timestamp": _iso(T0 - timedelta(minutes=90)), "duration": 45}
    finished = {"type": "tempbasal_correction", "timestamp": _iso(T0 - timedelta(hours=3)), "duration": 60}
    assert _only(mr.analyze([_meal(), running], _cgm(_steady_then_rise), [], **RANGE))["temp_basal"]
    assert not _only(mr.analyze([_meal(), finished], _cgm(_steady_then_rise), [], **RANGE))["temp_basal"]


def test_bolus_window_is_one_hour_before_to_two_hours_after():
    def bolus(minutes, **extra):
        return {"type": "insulin", "event_type": "Bolus", "insulin_type": "rapid", "amount": 2,
                "timestamp": _iso(T0 + timedelta(minutes=minutes)), **extra}
    cgm = _cgm(_steady_then_rise)
    assert _only(mr.analyze([_meal(), bolus(100)], cgm, [], **RANGE))["reasons"] == ["bolus"]
    assert _only(mr.analyze([_meal(), bolus(-55)], cgm, [], **RANGE))["bolus"]
    assert not _only(mr.analyze([_meal(), bolus(130)], cgm, [], **RANGE))["bolus"]
    assert not _only(mr.analyze([_meal(), bolus(-90)], cgm, [], **RANGE))["bolus"]
    # Long-acting insulin and pump daily totals are not boluses.
    assert not _only(mr.analyze([_meal(), bolus(10, insulin_type="long")], cgm, [], **RANGE))["bolus"]
    assert not _only(mr.analyze([_meal(), bolus(10, event_type="Daily Total")], cgm, [], **RANGE))["bolus"]


def test_low_treatment_starting_low_and_rising_is_still_readable():
    def treated_low(m):
        return 60 if m < -10 else 60 + (m + 10) * 1.8   # 60 until -10, then climbing
    meal = _only(mr.analyze([_meal()], _cgm(treated_low), [], **RANGE))
    assert meal["low_before"] and meal["start_glucose"] == 78 and meal["trend_30"] > mr.STEADY_MG_DL
    assert meal["readable"]


def test_steep_rise_from_a_normal_number_is_not_readable():
    meal = _only(mr.analyze([_meal()], _cgm(lambda m: 120 + m), [], **RANGE))
    assert meal["reasons"] == ["rising"]


def test_walking_minutes_count_only_walking_inside_two_hours_after():
    intervals = [
        {"activity": "walking", "start_time": _iso(T0 + timedelta(minutes=10)), "end_time": _iso(T0 + timedelta(minutes=30))},
        {"activity": "walking", "start_time": _iso(T0 + timedelta(minutes=110)), "end_time": _iso(T0 + timedelta(minutes=140))},
        {"activity": "resting", "start_time": _iso(T0 + timedelta(minutes=40)), "end_time": _iso(T0 + timedelta(minutes=90))},
        {"activity": "walking", "start_time": _iso(T0 - timedelta(minutes=30)), "end_time": _iso(T0 - timedelta(minutes=5))},
    ]
    meal = _only(mr.analyze([_meal()], _cgm(_steady_then_rise), intervals, **RANGE))
    assert meal["walking_minutes"] == 30
    assert meal["readable"]          # walking is context, not a reason to set a meal aside


def test_only_food_logger_meals_in_range_are_measured():
    treatments = [
        _meal(),
        {**_meal(), "id": "pump", "source": "glooko", "food_name": None},
        {**_meal(), "id": "bare", "food_name": None},
        {**_meal(), "id": "spoken", "food_name": None, "items": [{"name": "Rice", "carbs": 40}]},
        _meal(T0 + timedelta(days=30)),
    ]
    result = mr.analyze(treatments, _cgm(_steady_then_rise), [], **RANGE)
    assert {m["id"] for m in result["meals"]} == {_meal()["id"], "spoken"}


def test_second_cgm_source_copies_do_not_break_the_hold():
    primary = _cgm(_steady_then_rise)
    # A gap-filler copy one minute later reading lower would otherwise sit
    # between every pair of readings.
    copies = [{"timestamp": _iso(datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")) + timedelta(minutes=1)),
               "value": r["value"] - 12} for r in primary]
    assert _only(mr.analyze([_meal()], primary + copies, [], **RANGE))["rise_start_minutes"] == 40


def test_missing_or_sparse_glucose_is_not_readable():
    meal = _only(mr.analyze([_meal()], [], [], **RANGE))
    assert meal["reasons"] == ["no_glucose"] and meal["peak"] is None
    sparse = [r for i, r in enumerate(_cgm(_steady_then_rise)) if i < 14 or i % 4 == 0]
    meal = _only(mr.analyze([_meal()], sparse, [], **RANGE))
    assert meal["reasons"] == ["sparse_glucose"] and meal["rise_start_minutes"] is None


def _days(n, shape, extra=None):
    treatments, readings = [], []
    for day in range(n):
        t = T0 + timedelta(days=day)
        treatments.append(_meal(t))
        if extra:
            treatments.append(extra(t))
        readings += _cgm(shape, t0=t)
    return treatments, readings


def test_summary_needs_five_readable_meals_and_uses_medians():
    treatments, readings = _days(4, _steady_then_rise)
    result = mr.analyze(treatments, readings, [], **RANGE)
    assert result["meals_readable"] == 4 and result["summary"] is None

    treatments, readings = _days(5, _steady_then_rise)
    later = T0 + timedelta(days=5)          # one slower meal shifts nothing in the median
    treatments.append(_meal(later))
    readings += _cgm(lambda m: 110 if m <= 60 else min(110 + (m - 60), 200), t0=later)
    result = mr.analyze(treatments, readings, [], **RANGE)
    assert result["summary"] == {"n": 6, "n_with_rise": 6, "rise_start_minutes": 40, "peak_minutes": 100,
                                 "rise": 70, "start_glucose": 110}
    assert result["meals"][0]["timestamp"] == _iso(later)      # newest first


def test_unreadable_meals_stay_out_of_the_summary_and_are_counted_by_reason():
    def bolus(t):
        return {"type": "insulin", "event_type": "Bolus", "amount": 1, "timestamp": _iso(t + timedelta(minutes=5))}
    treatments, readings = _days(6, _steady_then_rise, extra=bolus)
    result = mr.analyze(treatments, readings, [], **RANGE)
    assert result["meals_total"] == 6 and result["meals_readable"] == 0
    assert result["summary"] is None and result["not_readable_reasons"] == {"bolus": 6}
    assert result["algorithm_version"] == mr.ALGORITHM_VERSION


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "data" / "app.sqlite3"
    path.parent.mkdir()
    run_migrations(path)
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


def test_food_action_reads_typed_glucose_treatments_and_walking(database, monkeypatch):
    meal_time = (datetime.now(timezone.utc) - timedelta(days=2)).replace(second=0, microsecond=0)
    get_repositories().treatments.create({**_meal(meal_time), "owner_email": food.OWNER_EMAIL})
    get_repositories().treatments.create({"type": "insulin", "event_type": "Bolus", "amount": 1, "source": "glooko",
                                          "timestamp": _iso(meal_time + timedelta(minutes=200)),
                                          "owner_email": food.OWNER_EMAIL})
    monkeypatch.setenv("TYPED_GLUCOSE_WRITES_ENABLED", "true")
    db.bulk_create_entities("GlucoseReading", [{**r, "source": "dexcom", "owner_email": food.OWNER_EMAIL}
                                               for r in _cgm(_steady_then_rise, t0=meal_time)])
    monkeypatch.setenv("TYPED_GLUCOSE_WRITES_ENABLED", "false")
    # A reading only in the legacy store is not read: CGM comes from the typed table.
    db.create_entity("GlucoseReading", {"timestamp": _iso(meal_time + timedelta(minutes=37)), "value": 400,
                                        "source": "dexcom", "owner_email": food.OWNER_EMAIL})
    SqliteActivityPositionRepository().create_manual(
        IntervalBody(start_time=meal_time + timedelta(minutes=20), end_time=meal_time + timedelta(minutes=45),
                     activity="walking"),
        actor_label="test",
    )
    result = asyncio.run(food.handle({"action": "meal_response"}))
    meal = _only(result)
    assert meal["food_name"] == "Rice bowl" and meal["carbs"] == 40
    assert meal["rise_start_minutes"] == 40 and meal["peak"] == 180
    assert meal["walking_minutes"] == 25 and not meal["bolus"] and meal["readable"]
