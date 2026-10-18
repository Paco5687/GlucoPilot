"""Bedtime glucose against that night's sleep, with a late-dinner comparison."""

from datetime import date, datetime, timedelta, timezone

import pytest

from server import analysis_evidence, bedtime_sleep as bs

pytestmark = pytest.mark.risk_critical

START = date(2026, 9, 1)
NIGHTS = 12
MISSING_NIGHT = 6     # no CGM at bedtime: not counted
HIGHER_FROM = 7       # nights 7-11 go to bed between 140 and 180


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _bedtime(night):
    day = START + timedelta(days=night)
    return datetime(day.year, day.month, day.day, 22, 30, tzinfo=timezone.utc)


def _cgm():
    readings = []
    t = datetime(START.year, START.month, START.day, tzinfo=timezone.utc)
    end = t + timedelta(days=NIGHTS + 1)
    while t < end:
        value = 110
        for night in range(NIGHTS):
            if _bedtime(night) <= t < _bedtime(night) + timedelta(hours=2):
                if night == MISSING_NIGHT:
                    value = None
                elif night >= HIGHER_FROM:
                    value = 160
                else:
                    value = 120
        if value is not None:
            readings.append({"timestamp": _iso(t), "value": value})
        t += timedelta(minutes=5)
    return readings


def _oura():
    # A night belongs to the morning it ends on, as Oura records it.
    return [
        {
            "id": f"oura-{night}",
            "date": (START + timedelta(days=night + 1)).isoformat(),
            "sleep_total_seconds": (7 if night >= HIGHER_FROM else 9) * 3600,
            "sleep_score": 75 if night >= HIGHER_FROM else 90,
            "sleep_efficiency": 88,
        }
        for night in range(NIGHTS)
    ]


def _dinners():
    rows = []
    for night in range(NIGHTS):
        day = START + timedelta(days=night)
        hour = 21 if night >= HIGHER_FROM else 18
        rows.append({
            "type": "carb",
            "amount": 40,
            "timestamp": _iso(datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)),
        })
    return rows


def _analysis():
    return bs.analyze(
        _cgm(), _oura(), _dinners(),
        start=START, end=START + timedelta(days=NIGHTS - 1), timezone_name="UTC",
    )


def test_nights_join_bedtime_glucose_to_the_sleep_that_follows():
    nights = bs.build_nights(
        _cgm(), _oura(), _dinners(),
        start=START, end=START + timedelta(days=NIGHTS - 1), timezone_name="UTC",
    )
    assert len(nights) == NIGHTS - 1
    assert MISSING_NIGHT not in {date.fromisoformat(row["night"]).day - 1 for row in nights}
    first = nights[0]
    assert first["bedtime_glucose"] == 120 and first["band"] == "under_140"
    assert first["sleep_day"] == "2026-09-02" and first["sleep_entity_id"] == "oura-0"
    assert first["sleep_hours"] == 9.0
    assert first["next_day_glucose"] == 110
    assert first["dinner_time"] == "18:00" and first["late_dinner"] is False
    assert nights[-1]["band"] == "140_to_180" and nights[-1]["late_dinner"] is True


def test_bands_and_dinner_groups_summarize_sleep():
    analysis = _analysis()
    assert analysis["nights_measured"] == NIGHTS - 1
    assert analysis["nights_over_180"] == 0
    assert analysis["highest_bedtime_glucose"] == 160
    under, middle = analysis["bands"]["under_140"], analysis["bands"]["140_to_180"]
    assert under["nights"] == 6 and under["median_sleep_hours"] == 9.0 and under["median_sleep_score"] == 90
    assert middle["nights"] == 5 and middle["median_sleep_hours"] == 7.0
    assert analysis["bands"]["over_180"] is None and analysis["band_nights"]["over_180"] == 0
    assert analysis["dinner"]["late"]["nights"] == 5
    assert analysis["dinner"]["late"]["median_bedtime_glucose"] == 160
    assert analysis["dinner"]["earlier"]["nights"] == 6


def test_groups_under_five_nights_are_not_summarized():
    analysis = bs.analyze(
        _cgm(), _oura(), [],
        start=START, end=START + timedelta(days=3), timezone_name="UTC",
    )
    assert analysis["bands"]["under_140"] is None
    assert analysis["band_nights"]["under_140"] == 4
    assert analysis["dinner"]["nights_logged"] == 0


def test_bedtime_item_states_findings_with_their_numbers():
    item = analysis_evidence.bedtime_item(
        _analysis(), start=START, end=START + timedelta(days=NIGHTS - 1)
    )
    findings = item["data"]["findings"]
    assert "above 180 mg/dL on none of them (highest 160 mg/dL)" in findings[0]
    assert any("between 140 and 180 mg/dL (5 nights): median sleep 7.0 hours" in f for f in findings)
    assert any(f.startswith("Dinners logged at or after 20:00 (5 nights)") for f in findings)
    assert item["confidence"]["limitations"]
    assert item["confidence"]["label"] in {"low", "medium", "high"}
    assert ("OuraDaily", "oura-0") in item["source_refs"]


def test_bedtime_item_without_dinner_logs_says_so():
    analysis = bs.analyze(
        _cgm(), _oura(), [],
        start=START, end=START + timedelta(days=NIGHTS - 1), timezone_name="UTC",
    )
    item = analysis_evidence.bedtime_item(analysis, start=START, end=START + timedelta(days=NIGHTS - 1))
    assert item["data"]["findings"][-1] == (
        "No dinner times were logged on these nights, so late and earlier dinners cannot be compared."
    )
