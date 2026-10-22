"""Visit Report history and timing sections. Synthetic data only."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from server import report_trends

UTC = ZoneInfo("UTC")


def _points(start: datetime, values: list[float], minutes: int = 5) -> list[tuple[datetime, float]]:
    return [(start + timedelta(minutes=minutes * i), value) for i, value in enumerate(values)]


def test_history_starts_on_the_first_of_the_month_eleven_months_back():
    assert report_trends.history_start(date(2026, 10, 22)) == date(2025, 11, 1)
    assert report_trends.history_start(date(2026, 1, 5)) == date(2025, 2, 1)


def test_glucose_by_month_reports_each_month_with_its_own_span():
    # Two full days at the end of one month, one in-range and one low-heavy day in the next.
    feb = _points(datetime(2026, 2, 27, tzinfo=timezone.utc), [120] * 576)
    march = _points(datetime(2026, 3, 1, tzinfo=timezone.utc), [60] * 6 + [150] * 282)

    rows = report_trends.glucose_by_month(feb + march, UTC)

    assert [row["month"] for row in rows] == ["2026-02", "2026-03"]
    feb_row, march_row = rows
    assert (feb_row["first_day"], feb_row["last_day"], feb_row["days"]) == ("2026-02-27", "2026-02-28", 2)
    assert (feb_row["tir"], feb_row["tbr70"], feb_row["coverage_pct"]) == (100.0, 0.0, 100.0)
    assert march_row["tbr70"] == round(6 / 288 * 100, 1)
    assert march_row["low_episodes"] == 1
    assert march_row["gmi"] == round(3.31 + 0.02392 * ((60 * 6 + 150 * 282) / 288), 1)


def test_lows_by_hour_finds_the_shortest_span_and_counts_low_nights():
    points = []
    for day in range(3):
        midnight = datetime(2026, 5, 1 + day, tzinfo=timezone.utc)
        # In range every 5 minutes all day...
        values = [120.0] * 288
        # ...low from 14:00 to 15:00 each day, and on the first night 02:00–02:30.
        for i in range(14 * 12, 15 * 12):
            values[i] = 62.0
        if day == 0:
            for i in range(2 * 12, 2 * 12 + 6):
                values[i] = 65.0
        points += _points(midnight, values)

    lows = report_trends.lows_by_hour(points, UTC)

    assert lows["low_readings"] == 3 * 12 + 6
    assert lows["span"] == {"start_hour": 14, "end_hour": 15, "share_pct": 86}
    assert lows["pct_below_70_by_hour"][14] == 100.0
    assert lows["overnight"] == {"nights": 3, "nights_with_low": 1, "hours": "00:00–06:00"}


def _mode(start: datetime, mode: str, minutes: float) -> dict:
    return {"type": "pump_mode", "mode": mode, "timestamp": start.isoformat().replace("+00:00", "Z"), "duration": minutes}


def test_pump_use_splits_modes_by_hour_and_matches_the_temp_basal_tile_count():
    rows = []
    for day in range(4):
        midnight = datetime(2026, 6, 1 + day, tzinfo=timezone.utc)
        rows += [
            _mode(midnight, "automatic", 8 * 60),
            _mode(midnight + timedelta(hours=8), "manual", 11 * 60),
            _mode(midnight + timedelta(hours=19), "automatic", 5 * 60),
        ]
    rows.append(rows[1])  # a repeated sync row counts once
    correction = {
        "type": "tempbasal_correction", "timestamp": "2026-06-01T09:00:00.000Z",
        "absolute": 3.8, "multiplier": 1.95, "duration": 120,
    }
    rows += [correction, dict(correction), {**correction, "timestamp": "2026-06-02T09:00:00.000Z", "duration": 60},
             {**correction, "timestamp": "2026-06-03T09:00:00.000Z", "absolute": None}]

    use = report_trends.pump_use(rows, UTC, days=4)

    modes = use["modes"]
    assert modes["days_with_mode_records"] == 4
    assert (modes["automated_pct"], modes["manual_pct"]) == (54, 46)
    assert modes["manual_span"] == {"start_hour": 8, "end_hour": 19, "hours": 11}
    assert modes["manual_share_by_hour"][7] == 0 and modes["manual_share_by_hour"][8] == 100
    assert use["temp_basals"] == {"count": 2, "per_day": 0.5, "median_increase_pct": 95, "median_minutes": 90}


def test_pump_use_is_absent_without_mode_or_temp_basal_rows():
    assert report_trends.pump_use([{"type": "insulin"}], UTC, days=10) is None


def _day(day: str, total: float, basal: float, bolus: float) -> dict:
    return {"date": day, "pump_reported": {"selected": {"total_units": total, "basal_units": basal, "bolus_units": bolus}}}


def test_insulin_by_month_averages_reported_totals_and_uses_that_months_weight():
    days = [
        _day("2026-03-01", 40, 30, 10), _day("2026-03-02", 36, 26, 10),
        _day("2026-04-01", 38, 38, 0),
        _day("2026-06-01", 30, 30, 0),
        {"date": "2026-06-02", "pump_reported": {"selected": None}},
    ]
    weights = [{"date": "2026-03-15", "weight_kg": 95.0}]

    march, april, june = report_trends.insulin_by_month(days, weights)

    assert (march["days"], march["tdd"], march["basal"], march["bolus"]) == (2, 38.0, 28.0, 10.0)
    assert (march["weight_kg"], march["weight_carried"], march["units_per_kg"]) == (95.0, False, 0.4)
    # April carries March's weight forward; June is beyond the carry window.
    assert (april["weight_carried"], april["units_per_kg"]) == (True, 0.4)
    assert (june["days"], june["weight_kg"], june["units_per_kg"]) == (1, None, None)


def test_a1c_vs_gmi_needs_two_weeks_of_cgm_before_the_draw():
    cgm = _points(datetime(2026, 5, 1, tzinfo=timezone.utc), [154.0] * (288 * 30))
    draws = [{"date": "2026-05-10", "value": 6.2}, {"date": "2026-06-15", "value": 6.4}]

    rows = report_trends.a1c_vs_gmi(draws, cgm, UTC)

    assert rows == [{
        "date": "2026-06-15", "a1c": 6.4, "gmi": round(3.31 + 0.02392 * 154, 1),
        "difference": round(6.4 - round(3.31 + 0.02392 * 154, 1), 1), "cgm_days": 30,
    }]


def test_imaging_studies_carry_their_measurements_newest_first():
    records = [
        {"id": "ct", "doc_type": "imaging_report", "title": "Synthetic CT", "record_date": "2026-06-30", "summary": " Spleen enlarged. "},
        {"id": "mri", "doc_type": "imaging_report", "title": "Synthetic MRI", "record_date": "2026-10-18", "summary": ""},
        {"id": "lab", "doc_type": "lab_report", "title": "Synthetic panel", "record_date": "2026-08-24"},
    ]
    labs = [
        {"record_id": "ct", "category": "Imaging - CT Abdomen/Pelvis", "test_name": "Spleen  length", "value": 12.5,
         "unit": "cm", "reference_high": 12, "flag": "high"},
        {"record_id": "lab", "category": "Chemistry", "test_name": "Sodium", "value": 140},
    ]

    studies = report_trends.imaging_studies(records, labs)

    assert [study["title"] for study in studies] == ["Synthetic MRI", "Synthetic CT"]
    assert studies[0]["summary"] is None and studies[0]["measurements"] == []
    assert studies[1]["summary"] == "Spleen enlarged."
    assert studies[1]["measurements"] == [{
        "name": "Spleen length", "value": 12.5, "unit": "cm",
        "reference_low": None, "reference_high": 12, "flag": "high",
    }]


@pytest.mark.parametrize(("category", "expected"), [
    ("Imaging - CT Abdomen/Pelvis", True), ("imaging", True), ("Metabolic Panel", False), (None, False),
])
def test_imaging_categories(category, expected):
    assert report_trends.is_imaging_category(category) is expected
