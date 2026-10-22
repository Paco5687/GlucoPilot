"""Companion sentences that restate a derived analysis are held to its numbers.

Synthetic analyses only. The findings follow analysis_evidence's templates;
the numbers are made up.
"""

from __future__ import annotations

import pytest

from server import companion_evidence

pytestmark = pytest.mark.risk_critical

CORRECTION = {
    "summary": (
        "Glucose time course after temp basal corrections from 2026-05-01 to 2026-08-28: "
        "32 corrections started at or above 150 mg/dL."
    ),
    "findings": [
        "After 32 temp basal corrections started at or above 150 mg/dL, glucose was 10 mg/dL below where it "
        "started within 1 hour in 70% and within 2 hours in 85%; half got there by 25 minutes, and in 5% it "
        "had not within 3 hours.",
        "Median change from the starting glucose: 30 minutes -6, 1 hour -14, 90 minutes -27, 2 hours -45, "
        "3 hours -61 mg/dL.",
        "These corrections started at a median of 172 mg/dL, ran a median of 75 minutes, and had no carbs "
        "logged in the 3 hours after.",
        "When glucose was still climbing as the temp basal started (20 of 32), it rose a median of 12 mg/dL "
        "further, peaked about 15 minutes after the start, and was 10 mg/dL below its starting value a median "
        "of 40 minutes after the start (80% within 2 hours).",
        "The lowest point came a median of 150 minutes after the start.",
        "Including corrections started below 150 mg/dL (201 in all, median start 128 mg/dL), glucose was "
        "10 mg/dL below its starting value a median of 62 minutes after the start: within 1 hour in 41% and "
        "within 2 hours in 77%.",
        "Across all 201 clean temp basal corrections, glucose fell a median of 18 mg/dL per extra unit over "
        "3 hours.",
    ],
    "supporting_data": {
        "timecourse": {
            "n": 32, "median_start_glucose": 172, "median_minutes": 75, "median_minutes_to_fall": 25,
            "median_minutes_to_nadir": 150, "median_minutes_to_peak": 35, "median_peak_rise": 19,
            "fell_within_60_pct": 70, "fell_within_120_pct": 85, "did_not_fall_pct": 5, "rose_first_pct": 45,
            "delta_30": -6, "delta_60": -14, "delta_90": -27, "delta_120": -45, "delta_180": -61,
            "min_start_glucose": 150,
            "rising_at_start": {
                "n": 20, "median_further_rise": 12, "median_minutes_to_peak": 15,
                "median_minutes_to_fall": 40, "fell_within_120_pct": 80,
            },
        },
        "timecourse_all_starts": {
            "n": 201, "median_start_glucose": 128, "median_minutes_to_fall": 62,
            "fell_within_60_pct": 41, "fell_within_120_pct": 77, "delta_60": 1, "delta_120": -24,
        },
        "all_corrections": {"n_temp_basal": 201, "drop_per_unit_temp_basal": 18.0},
    },
}


def _walking(under_120_walk_low: int = 50) -> dict:
    return {
        "summary": (
            "Glucose response with and without walking from 2026-05-01 to 2026-08-28: 900 walks and 320 "
            "walk-free half-hour checks measured during waking hours."
        ),
        "findings": [
            "Sitting or resting during the day, with no walking for the next 2 hours, glucose rose over 2 hours "
            "when it started below 120 mg/dL; it fell over 2 hours when it started between 120 and 170 mg/dL.",
            "Sitting or resting, starting below 120 mg/dL: glucose rose a median of 6 mg/dL after 1 hour and "
            "rose a median of 18 mg/dL after 2 hours (260 daytime checks).",
            "Sitting or resting, starting between 120 and 170 mg/dL: glucose fell a median of 12 mg/dL after "
            "1 hour and fell a median of 25 mg/dL after 2 hours (60 daytime checks).",
            "After walking 8 minutes or more, starting below 120 mg/dL: glucose fell a median of 2 mg/dL by "
            "30 minutes and was unchanged at the median by 1 hour; its lowest point within 2 hours was a median "
            "of 14 mg/dL below the start (650 walks).",
            "After walking 8 minutes or more, starting between 120 and 170 mg/dL: glucose fell a median of "
            "9 mg/dL by 30 minutes and fell a median of 20 mg/dL by 1 hour; its lowest point within 2 hours was "
            "a median of 50 mg/dL below the start (250 walks).",
            f"Lows were {'more' if under_120_walk_low > 30 else 'less'} common after walking than after sitting "
            f"when glucose started below 120 mg/dL: it went below 70 within 2 hours after {under_120_walk_low}% "
            "of walks and after 30% of sitting periods.",
            "Lows were more common after walking than after sitting when glucose started between 120 and "
            "170 mg/dL: it went below 70 within 2 hours after 25% of walks and after 10% of sitting periods.",
        ],
        "supporting_data": {
            "bands": {
                "under_120": {
                    "sitting": {"n": 260, "delta_60": 6, "delta_120": 18, "nadir_delta": -5, "went_low_pct": 30},
                    "walking": {
                        "n": 650, "minutes": 19, "delta_15": -1, "delta_30": -2, "delta_60": 0, "delta_120": 2,
                        "nadir_delta": -14, "went_low_pct": under_120_walk_low,
                    },
                },
                "120_to_170": {
                    "sitting": {"n": 60, "delta_60": -12, "delta_120": -25, "nadir_delta": -31, "went_low_pct": 10},
                    "walking": {
                        "n": 250, "minutes": 24, "delta_30": -9, "delta_60": -20, "delta_120": -41,
                        "nadir_delta": -50, "went_low_pct": 25,
                    },
                },
                "over_170": None,
            },
            "median_walk_minutes": 21,
        },
    }


BEDTIME = {
    "summary": (
        "Bedtime glucose and that night's Oura sleep from 2026-05-01 to 2026-08-28: 100 nights measured, "
        "80 with sleep data."
    ),
    "findings": [
        "Bedtime glucose (mean CGM 22:30 to 00:30) was measured on 100 nights from 2026-05-01 to 2026-08-27; "
        "it was above 180 mg/dL on none of them (highest 175 mg/dL).",
        "Nights going to bed below 140 mg/dL (88 nights): median sleep 8.4 hours, sleep score 88; next-day "
        "mean glucose 101 mg/dL.",
        "Nights going to bed between 140 and 180 mg/dL (12 nights): median sleep 7.9 hours, sleep score 80; "
        "next-day mean glucose 110 mg/dL.",
        "No dinner times were logged on these nights, so late and earlier dinners cannot be compared.",
    ],
    "supporting_data": {
        "bands": {
            "under_140": {"median_sleep_hours": 8.4, "median_sleep_score": 88, "median_next_day_glucose": 101},
            "140_to_180": {"median_sleep_hours": 7.9, "median_sleep_score": 80, "median_next_day_glucose": 110},
            "over_180": None,
        },
        "highest_bedtime_glucose": 175,
        "nights_over_180": 0,
    },
}


def _item(alias: str, entity_type: str, analysis: dict | None = None, domain: str = "glucose") -> dict:
    item = {
        "alias": alias,
        "id": f"synthetic:{alias}",
        "domain": domain,
        "entity_type": entity_type,
        "entity_id": alias.lower(),
        "title": entity_type,
        "confidence": {},
        "source_ids": [f"entity:{entity_type}:{alias.lower()}"],
        "source_links": [],
        "claim": None,
        "flag": None,
        "nonstandard_flag": None,
    }
    if analysis is not None:
        item["analysis"] = analysis
    return item


def _context(walking: dict | None = None) -> dict:
    return {
        "scopes": [{"scope": "analyses"}],
        "evidence_items": [
            _item("E1", "CorrectionResponseSummary", CORRECTION),
            _item("E2", "WalkingResponseSummary", walking or _walking()),
            _item("E3", "BedtimeSleepSummary", BEDTIME),
            _item("E4", "GlucoseReading"),
        ],
    }


def _finalize(sentence: str, context: dict | None = None) -> tuple[str, int]:
    reply, evidence = companion_evidence.finalize_reply(sentence, context or _context(), [], [])
    return reply, evidence["omissions"]["count"]


@pytest.mark.parametrize(
    "sentence",
    [
        # Restated, rounded and hedged.
        "Half of your corrections got there by 25 minutes [E1].",
        "A temp basal usually starts lowering your glucose within 30 minutes [E1].",
        "It took about 30 minutes for glucose to start dropping [E1].",
        "Your temp basals ran a median of 75 minutes [E1].",
        "In 85% of corrections glucose was 10 mg/dL lower within 2 hours [E1].",
        "Glucose fell 14 mg/dL by 1 hour and 45 mg/dL by 2 hours [E1].",
        "Median glucose fell 27 mg/dL by 90 minutes [E1].",
        "When it was still rising, it peaked about 15 minutes later and fell 10 mg/dL below start by a median "
        "of 40 minutes [E1].",
        "The median time to peak was 35 minutes, and the lowest point came at 150 minutes [E1].",
        "The lowest point came around 2 hours and 30 minutes after the start [E1].",
        "The lowest point came about 2.5 hours after the start [E1].",
        "Counting starts below 150 mg/dL, the median time to fall 10 mg/dL was 62 minutes [E1].",
        "Glucose fell a median of 18 mg/dL per extra unit over 3 hours [E1].",
        "Your temp basal doesn't need 2 hours to start working [E1].",
        "These corrections started at a median of 172 mg/dL, across 32 corrections [E1].",
        "That is 20 of 32 corrections [E1].",
        # Walking and sitting.
        "Sitting from below 120 mg/dL, glucose rose 18 mg/dL after 2 hours [E2].",
        "Sitting from below 120, glucose rose a median of +6 mg/dL at 1 hour [E2].",
        "Walking from 120 to 170 mg/dL, glucose fell about 20 mg/dL in an hour [E2].",
        "After a walk from below 120, glucose dipped as much as 14 mg/dL below the start within 2 hours [E2].",
        "Lows were more common after walking: 50% of walks against 30% of sitting periods [E2].",
        "From 120 to 170 mg/dL, lows followed 25% of walks; that is a 15% higher risk than sitting [E2].",
        "Within 1 hour, 70% of corrections had come down 10 mg/dL, and 40% counting low starts [E1].",
        "Walking lowers glucose, which raises the risk of lows [E2].",
        "Walking doesn't prevent lows in your data [E2].",
        "If you are worried about lows, stop walking to avoid lows and check your glucose [E2].",
        "If it drops below 70 mg/dL, treat it [E2].",
        "If you walk, check your glucose every 10 minutes and at 15 minutes [E2].",
        "Walking can help with low energy, though glucose fell after it [E2].",
        # Bedtime and sleep, including a difference between two groups.
        "Going to bed below 140 mg/dL, you slept 8.4 hours with a sleep score of 88 [E3].",
        "Going to bed between 140–180 mg/dL, you slept about 8 hours [E3].",
        "That is a difference of 0.5 hours [E3].",
        "Higher bedtimes came with about half an hour less sleep [E3].",
        "Your highest bedtime glucose was 175 mg/dL, below 180 mg/dL [E3].",
        # Another item is cited for the number, so this check does not apply.
        "Your current glucose is 115 mg/dL [E4], and sitting may raise it [E2].",
    ],
)
def test_restated_analysis_numbers_are_kept(sentence):
    reply, omitted = _finalize(sentence)
    assert omitted == 0, reply
    assert sentence.split("[")[0].strip() in reply


@pytest.mark.parametrize(
    "sentence",
    [
        # A time from another event: 75 is how long the temp basal ran, 90 a
        # time-course point, 150 the lowest point, 40 the rising group's time
        # to fall; glucose fell after 25 and peaked at 35 or 15.
        "The median time to reach that drop was 75 minutes [E1].",
        "The median time to reach that drop was 90 minutes [E1].",
        "It usually takes about 2 hours before your glucose starts to drop [E1].",
        "Glucose fell 10 mg/dL below where it started a median of 150 minutes after the start [E1].",
        "It peaked 40 minutes after the start [E1].",
        # A change given at the wrong time point, or a number the analysis lacks.
        "Glucose fell a median of 27 mg/dL in the first hour [E1].",
        "Glucose fell 14 mg/dL by 1 hour and 61 mg/dL by 2 hours [E1].",
        "Glucose fell a median of 33 mg/dL in the first hour [E1].",
        "Your current glucose is 115 mg/dL [E1].",
        "Sitting from below 120 mg/dL, glucose rose 18 mg/dL in an hour [E2].",
        "After walking, glucose was -14 mg/dL at 2 hours [E2].",
        "Going to bed between 140 and 180 mg/dL, you slept 6.5 hours [E3].",
        "Lows followed 40% of walks [E2].",
        # Walking said to prevent lows when lows were more common after walking.
        "Walking lowers the chance of going under 70 [E2].",
        "Walking can help prevent lows [E2].",
        "A short walk keeps you from going low [E2].",
        "Lows were less common after walking than after sitting [E2].",
        "You had fewer lows after walking [E2].",
    ],
)
def test_misstated_analysis_numbers_are_omitted(sentence):
    reply, omitted = _finalize(f"Here is what your records show. {sentence}")
    assert omitted == 1, reply
    assert sentence.split("[")[0].strip() not in reply
    assert "Here is what your records show." in reply


def test_walking_reducing_lows_is_left_alone_when_a_band_found_that():
    context = _context(walking=_walking(under_120_walk_low=20))
    reply, omitted = _finalize("Walking lowers the chance of going under 70 [E2].", context)
    assert omitted == 0, reply


def test_a_misstated_sentence_does_not_cost_the_rest_of_its_line():
    reply, omitted = _finalize(
        "Half got there by 25 minutes [E1]. The median time to reach that drop was 75 minutes [E1]. "
        "They ran a median of 75 minutes [E1]."
    )
    assert omitted == 1
    assert "Half got there by 25 minutes [E1]." in reply
    assert "They ran a median of 75 minutes [E1]." in reply
    assert "time to reach that drop" not in reply
