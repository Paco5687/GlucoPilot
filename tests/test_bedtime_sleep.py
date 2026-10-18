"""Bedtime glucose against that night's sleep, with a late-dinner comparison."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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


def test_nights_without_an_oura_bedtime_use_the_fixed_window():
    analysis = _analysis()
    assert {row["bedtime_basis"] for row in analysis["nights"]} == {"fixed_window"}
    assert {row["bedtime"] for row in analysis["nights"]} == {None}
    assert analysis["bedtime_basis_nights"] == {"oura_bedtime": 0, "fixed_window": NIGHTS - 1}
    assert "from 22:30 to 00:30 local time on all 11 nights" in analysis["semantics"]["bedtime"]
    item = analysis_evidence.bedtime_item(analysis, start=START, end=START + timedelta(days=NIGHTS - 1))
    assert item["data"]["findings"][0].startswith("Bedtime glucose (mean CGM 22:30 to 00:30) was measured")
    assert item["confidence"]["limitations"][0] == (
        "Bedtime is a fixed 22:30 to 00:30 window on all 11 nights, not her recorded bedtime: "
        "none had an Oura bedtime between 18:00 and 06:00."
    )


# Oura bedtimes in a non-UTC zone. Glucose is 200 from 23:10 to 01:10 local and
# 110 otherwise, so a window anchored on a 23:40 bedtime reads 200 and the
# fixed 22:30-00:30 window reads (8 x 110 + 16 x 200) / 24 = 170.
NY = ZoneInfo("America/New_York")
OURA_START = date(2026, 9, 1)


def _high_late_cgm(days=4):
    readings = []
    t = datetime(OURA_START.year, OURA_START.month, OURA_START.day, 12, tzinfo=NY).astimezone(timezone.utc)
    end = t + timedelta(days=days)
    while t < end:
        local = t.astimezone(NY)
        minute = local.hour * 60 + local.minute
        value = 200 if minute >= 23 * 60 + 10 or minute < 70 else 110
        readings.append({"timestamp": _iso(t), "value": value})
        t += timedelta(minutes=5)
    return readings


def _oura_day(night, **extra):
    return {
        "id": f"oura-ny-{night}",
        "date": (OURA_START + timedelta(days=night + 1)).isoformat(),
        "sleep_total_seconds": 8 * 3600,
        "sleep_score": 85,
        **extra,
    }


def _mixed_nights():
    sleep = [
        _oura_day(0, bedtime_start="2026-09-01T23:40:00-04:00", bedtime_end="2026-09-02T07:40:00-04:00"),
        _oura_day(1),  # synced before bedtime was stored
        # A nap-only day: its main period starts in the afternoon, not at bedtime.
        _oura_day(2, bedtime_start="2026-09-04T14:05:00-04:00", bedtime_end="2026-09-04T14:40:00-04:00"),
    ]
    return bs.analyze(
        _high_late_cgm(), sleep, [],
        start=OURA_START, end=OURA_START + timedelta(days=2), timezone_name="America/New_York",
    )


def test_oura_bedtime_anchors_the_bedtime_window():
    nights = _mixed_nights()["nights"]
    oura, missing, nap = nights
    assert oura["bedtime_basis"] == "oura_bedtime" and oura["bedtime"] == "23:40"
    assert oura["bedtime_glucose"] == 200 and oura["band"] == "over_180"
    assert oura["sleep_entity_id"] == "oura-ny-0"
    for night in (missing, nap):
        assert night["bedtime_basis"] == "fixed_window" and night["bedtime"] is None
        assert night["bedtime_glucose"] == 170
    # The fallback nights still join to their Oura sleep.
    assert nap["sleep_entity_id"] == "oura-ny-2" and nap["sleep_hours"] == 8.0


def test_semantics_and_limitations_count_each_bedtime_basis():
    analysis = _mixed_nights()
    assert analysis["bedtime_basis_nights"] == {"oura_bedtime": 1, "fixed_window": 2}
    assert analysis["semantics"]["bedtime"] == (
        "Bedtime glucose is the mean CGM reading from 30 minutes before to 90 minutes after "
        "Oura's recorded bedtime (bedtime_start of the main sleep period) on 1 night, and the "
        "mean CGM reading from 22:30 to 00:30 local time on the other 2 nights, which had no "
        "Oura bedtime between 18:00 and 06:00 local time. Nights with under an hour of readings "
        "in the bedtime window are not counted."
    )
    item = analysis_evidence.bedtime_item(
        analysis, start=OURA_START, end=OURA_START + timedelta(days=2)
    )
    assert item["data"]["findings"][0].startswith(
        "Bedtime glucose (mean CGM from 30 minutes before to 90 minutes after her Oura bedtime "
        "on 1 night, 22:30 to 00:30 on 2 nights) was measured on 3 nights"
    )
    assert item["data"]["supporting_data"]["bedtime_basis_nights"] == {
        "oura_bedtime": 1, "fixed_window": 2,
    }
    assert item["confidence"]["limitations"][0] == (
        "Bedtime is the start of her Oura main sleep period on 1 night; the other 2 nights had "
        "no Oura bedtime between 18:00 and 06:00 and use a fixed 22:30 to 00:30 window."
    )


def test_every_night_on_oura_bedtime_says_so():
    sleep = [
        _oura_day(night, bedtime_start=f"2026-09-0{night + 1}T23:40:00-04:00")
        for night in range(3)
    ]
    analysis = bs.analyze(
        _high_late_cgm(), sleep, [],
        start=OURA_START, end=OURA_START + timedelta(days=2), timezone_name="America/New_York",
    )
    assert analysis["bedtime_basis_nights"] == {"oura_bedtime": 3, "fixed_window": 0}
    assert {row["bedtime_glucose"] for row in analysis["nights"]} == {200}
    assert "(bedtime_start of the main sleep period) on all 3 nights." in analysis["semantics"]["bedtime"]
    item = analysis_evidence.bedtime_item(analysis, start=OURA_START, end=OURA_START + timedelta(days=2))
    assert item["confidence"]["limitations"][0] == (
        "Bedtime is the start of her Oura main sleep period on all 3 nights."
    )


def test_dinner_after_midnight_before_a_late_oura_bedtime_is_late():
    sleep = [_oura_day(0, bedtime_start="2026-09-02T01:30:00-04:00")]
    snack = datetime(2026, 9, 2, 0, 20, tzinfo=NY).astimezone(timezone.utc)
    night = bs.build_nights(
        _high_late_cgm(), sleep, [{"type": "carb", "amount": 30, "timestamp": _iso(snack)}],
        start=OURA_START, end=OURA_START, timezone_name="America/New_York",
    )[0]
    assert night["bedtime_basis"] == "oura_bedtime" and night["bedtime"] == "01:30"
    assert night["dinner_time"] == "00:20" and night["late_dinner"] is True
