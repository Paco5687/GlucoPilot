"""Evidence items for the analyses that answer recurring questions.

Each builder turns one deterministic analysis of her own records into the
data, confidence and source references of a single Evidence Bundle
``derived_metric``. The ``findings`` are plain sentences with the numbers
already worked out, so a Companion reply can restate and cite them instead of
reasoning from a handful of raw readings. Builders are pure; loading rows and
linking sources stays in ``evidence_bundle``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import bedtime_sleep, correction_response, walking_response
from .analytics_confidence import comparison_confidence, mean_confidence

WALKING = "WalkingResponseSummary"
CORRECTION = "CorrectionResponseSummary"
BEDTIME = "BedtimeSleepSummary"
ENTITY_TYPES = (BEDTIME, CORRECTION, WALKING)
MAX_SOURCE_REFS = 40
WAKING_HOURS = (7, 22)  # local; overnight rest is not "sitting down"
ASSOCIATION_ONLY = (
    "These are medians of what her own records show; they are associations, "
    "not causes, and not treatment guidance."
)

_BAND_LABELS = {
    "under_120": "below 120 mg/dL",
    "120_to_170": "between 120 and 170 mg/dL",
    "over_170": "above 170 mg/dL",
    "under_140": "below 140 mg/dL",
    "140_to_180": "between 140 and 180 mg/dL",
    "over_180": "above 180 mg/dL",
}


def _change(delta: float | None) -> str:
    if delta is None:
        return "was not measurable"
    if delta > 0:
        return f"rose a median of {delta:g} mg/dL"
    if delta < 0:
        return f"fell a median of {-delta:g} mg/dL"
    return "was unchanged at the median"


def _window(start: date, end: date) -> dict[str, Any]:
    return {"start": start.isoformat(), "end": end.isoformat(), "days": (end - start).days + 1}


def _confidence(envelope: dict[str, Any], limitations: list[str]) -> dict[str, Any]:
    return {
        "label": envelope["confidence_label"],
        "score": envelope["confidence_score"],
        "discovery_status": envelope["discovery_status"],
        "sample_count": envelope["sample_count"],
        "limitations": [*limitations, ASSOCIATION_ONLY],
    }


def _recent(refs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The newest references, bounded: a summary links the observations it is
    built from without carrying thousands of links into every stored reply."""
    unique = list(dict.fromkeys(ref for ref in refs if ref[1]))
    return unique[-MAX_SOURCE_REFS:]


def _waking(row: dict[str, Any], tz: ZoneInfo) -> bool:
    try:
        hour = datetime.fromisoformat(str(row["start"]).replace("Z", "+00:00")).astimezone(tz).hour
    except (KeyError, TypeError, ValueError):
        return False
    return WAKING_HOURS[0] <= hour < WAKING_HOURS[1]


def walking_item(
    measurement: dict[str, Any],
    *,
    start: date,
    end: date,
    timezone_name: str = "UTC",
) -> dict[str, Any]:
    """Sitting (no walking) against walking during waking hours, from
    walking_response.measure(). The Activity page's sitting controls also run
    overnight; her question is about sitting down during the day."""
    tz = ZoneInfo(timezone_name)
    measured = [row for row in measurement["measured"] if _waking(row, tz)]
    controls = [row for row in measurement["controls"] if _waking(row, tz)]
    analysis = walking_response.summarize({**measurement, "measured": measured, "controls": controls})
    window = _window(start, end)
    groups = [(band, analysis["bands"][band]) for band, _low, _high in walking_response.BANDS]
    sitting = [(band, group["sitting"]) for band, group in groups if group["sitting"]]
    walking = [(band, group["walking"]) for band, group in groups if group["walking"]]
    findings = []
    # The direct answer first, one direction per starting band, so a reply
    # cannot mix up which way glucose went while she sat.
    directions = []
    for verb, test in (("rose", lambda d: d > 0), ("fell", lambda d: d < 0), ("held steady", lambda d: d == 0)):
        bands = [_BAND_LABELS[band] for band, group in sitting if group["delta_120"] is not None and test(group["delta_120"])]
        if bands:
            directions.append(f"{verb} over 2 hours when it started {' or '.join(bands)}")
    if directions:
        findings.append(
            "Sitting or resting during the day, with no walking for the next 2 hours, glucose "
            + "; it ".join(directions) + "."
        )
    for band, group in sitting:
        findings.append(
            f"Sitting or resting, starting {_BAND_LABELS[band]}: glucose {_change(group['delta_60'])} after "
            f"1 hour and {_change(group['delta_120'])} after 2 hours ({group['n']} daytime checks)."
        )
    for band, group in walking:
        findings.append(
            f"After walking {walking_response.MIN_WALK_MINUTES} minutes or more, starting {_BAND_LABELS[band]}: "
            f"glucose {_change(group['delta_30'])} by 30 minutes and {_change(group['delta_60'])} by 1 hour; "
            f"its lowest point within 2 hours was a median of {abs(group['nadir_delta'] or 0):g} mg/dL below "
            f"the start ({group['n']} walks)."
        )
    for band, group in groups:
        if group["walking"] and group["sitting"]:
            walked, sat = group["walking"]["went_low_pct"], group["sitting"]["went_low_pct"]
            lead = (
                "Lows were more common after walking than after sitting" if walked > sat
                else "Lows were less common after walking than after sitting" if walked < sat
                else "Lows were as common after walking as after sitting"
            )
            findings.append(
                f"{lead} when glucose started {_BAND_LABELS[band]}: it went below 70 within 2 hours after "
                f"{walked}% of walks and after {sat}% of sitting periods."
            )
    if not findings:
        findings.append(
            f"Too few measured walks ({analysis['walks_measured']}) or walk-free periods to compare "
            "sitting and walking in this window."
        )
    walking_days = len({row["start"][:10] for row in measured})
    envelope = comparison_confidence(
        [row["delta_60"] for row in measured if row.get("delta_60") is not None],
        [row["delta_60"] for row in controls if row.get("delta_60") is not None],
        valid_days=walking_days,
        expected_days=window["days"],
        temporal_direction="activity-then-glucose",
        unit="mg/dL",
    )
    data = {
        "title": "Glucose while sitting compared with walking",
        "summary": (
            f"Glucose response with and without walking from {window['start']} to {window['end']}: "
            f"{analysis['walks_measured']} walks and {analysis['controls']} walk-free half-hour checks "
            "measured during waking hours."
        ),
        "findings": findings,
        "window": window,
        "sample_count": analysis["walks_measured"] + analysis["controls"],
        "algorithm_version": analysis["algorithm_version"],
        "supporting_data": {
            "bands": analysis["bands"],
            "median_walk_minutes": analysis["median_walk_minutes"],
            "walks_total": analysis["walks_total"],
        },
        "semantics": {
            **analysis["semantics"],
            "waking_hours": (
                f"Walks and sitting checks starting {WAKING_HOURS[0]:02d}:00 to {WAKING_HOURS[1]:02d}:00 "
                "local time only."
            ),
        },
    }
    refs = _recent([
        ("ActivityPositionInterval", row["interval_ids"][0])
        for row in sorted(measured, key=lambda row: row["start"])
        if row.get("interval_ids")
    ])
    return {
        "data": data,
        "confidence": _confidence(envelope, [
            "Walking is inferred from wearable step bouts; 'sitting' means no walking was recorded, not a measured posture.",
            "Meals, insulin and stress in the same hours are not separated out.",
        ]),
        "source_refs": refs,
    }


def correction_item(
    episodes: list[dict[str, Any]],
    readings: list[dict[str, Any]],
    *,
    start: date,
    end: date,
) -> dict[str, Any]:
    """How soon glucose falls after a temp basal correction starts."""
    course = correction_response.timecourse(episodes, readings, method="temp_basal")
    every = correction_response.timecourse(episodes, readings, method="temp_basal", min_start_glucose=0)
    overall = correction_response.summarize(episodes)
    window = _window(start, end)
    threshold = correction_response.HIGH_START_MG_DL
    findings = []
    if course["enough"]:
        findings.append(
            f"After {course['n']} temp basal corrections started at or above {threshold:g} mg/dL, glucose was "
            f"{correction_response.FALL_MG_DL} mg/dL below where it started within 1 hour in "
            f"{course['fell_within_60_pct']}% and within 2 hours in {course['fell_within_120_pct']}%; half got "
            f"there by {course['median_minutes_to_fall']} minutes, and in {course['did_not_fall_pct']}% it had "
            "not within 3 hours."
        )
        findings.append(
            "Median change from the starting glucose: "
            + ", ".join(
                f"{_label_minutes(minutes)} {_signed(course[f'delta_{minutes}'])}"
                for minutes in correction_response.TIMECOURSE_OFFSETS_MINUTES
            )
            + " mg/dL."
        )
        findings.append(
            f"These corrections started at a median of {course['median_start_glucose']} mg/dL, ran a median of "
            f"{course['median_minutes']} minutes, and had no carbs logged in the 3 hours after."
        )
        rising = course["rising_at_start"]
        if rising["enough"]:
            findings.append(
                f"When glucose was still climbing as the temp basal started ({rising['n']} of {course['n']}), "
                f"it rose a median of {rising['median_further_rise']} mg/dL further, peaked about "
                f"{rising['median_minutes_to_peak']} minutes after the start, and was "
                f"{correction_response.FALL_MG_DL} mg/dL below its starting value a median of "
                f"{rising['median_minutes_to_fall']} minutes after the start ({rising['fell_within_120_pct']}% "
                "within 2 hours)."
            )
        elif course["rose_first_pct"]:
            findings.append(
                f"In {course['rose_first_pct']}% glucose first rose at least {correction_response.RISE_MG_DL} mg/dL "
                f"further, peaking a median of {course['median_peak_rise']} mg/dL higher about "
                f"{course['median_minutes_to_peak']} minutes after the start."
            )
        findings.append(
            f"The lowest point came a median of {course['median_minutes_to_nadir']} minutes after the start."
        )
        if every["enough"] and every["n"] > course["n"]:
            findings.append(
                f"Including corrections started below {threshold:g} mg/dL ({every['n']} in all, median start "
                f"{every['median_start_glucose']} mg/dL), glucose was {correction_response.FALL_MG_DL} mg/dL below "
                f"its starting value a median of {every['median_minutes_to_fall']} minutes after the start: within "
                f"1 hour in {every['fell_within_60_pct']}% and within 2 hours in {every['fell_within_120_pct']}%."
            )
        if overall.get("drop_per_unit_temp_basal") is not None:
            findings.append(
                f"Across all {overall['n_temp_basal']} clean temp basal corrections, glucose fell a median of "
                f"{overall['drop_per_unit_temp_basal']:g} mg/dL per extra unit over 3 hours."
            )
    else:
        findings.append(
            f"Only {course['n']} clean temp basal corrections started at or above {threshold:g} mg/dL in this "
            "window, too few to describe a typical time course."
        )
    used = [
        episode for episode in episodes
        if episode["method"] == "temp_basal" and not episode["confounded"]
        and episode["start_glucose"] >= threshold
    ]
    envelope = mean_confidence(
        [
            episode["start_glucose"] - episode["nadir_glucose"]
            for episode in used
        ],
        valid_days=len({episode["start"][:10] for episode in used}),
        expected_days=window["days"],
        temporal_direction="correction-then-glucose",
        unit="mg/dL",
    )
    data = {
        "title": "Temp basal corrections: how soon glucose falls",
        "summary": (
            f"Glucose time course after temp basal corrections from {window['start']} to {window['end']}: "
            f"{course['n']} corrections started at or above {threshold:g} mg/dL."
        ),
        "findings": findings,
        "window": window,
        "sample_count": course["n"],
        "algorithm_version": course["algorithm_version"],
        "supporting_data": {
            "timecourse": course,
            "timecourse_all_starts": {
                key: every[key]
                for key in (
                    "n", "median_start_glucose", "median_minutes_to_fall",
                    "fell_within_60_pct", "fell_within_120_pct", "delta_60", "delta_120",
                )
            },
            "all_corrections": {
                key: overall[key]
                for key in (
                    "n_total", "n_clean", "n_confounded", "n_temp_basal", "n_bolus",
                    "drop_per_unit_temp_basal", "drop_per_unit_from_high", "drop_per_unit_from_low",
                )
            },
        },
    }
    refs = _recent([
        ("Treatment", source_id)
        for episode in sorted(used, key=lambda episode: episode["start"])
        for source_id in episode.get("source_ids", [])[:1]
    ])
    return {
        "data": data,
        "confidence": _confidence(envelope, [
            "Meals are rarely logged, so some corrections may overlap food that was not entered.",
            "A correction started near range cannot fall far, so the starting glucose travels with every number.",
        ]),
        "source_refs": refs,
    }


def _label_minutes(minutes: int) -> str:
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour{'s' if hours > 1 else ''}"
    return f"{minutes} minutes"


def _signed(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"+{value:g}" if value > 0 else f"{value:g}"


def _bedtime_measure(basis_nights: dict[str, int]) -> str:
    oura = basis_nights[bedtime_sleep.OURA_BEDTIME]
    fixed = basis_nights[bedtime_sleep.FIXED_WINDOW]
    if oura and fixed:
        return (
            f"mean CGM from 30 minutes before to 90 minutes after her Oura bedtime on "
            f"{bedtime_sleep.nights_text(oura)}, 22:30 to 00:30 on {bedtime_sleep.nights_text(fixed)}"
        )
    if oura:
        return "mean CGM from 30 minutes before to 90 minutes after her Oura bedtime"
    return "mean CGM 22:30 to 00:30"


def _bedtime_limitation(basis_nights: dict[str, int]) -> str:
    oura = basis_nights[bedtime_sleep.OURA_BEDTIME]
    fixed = basis_nights[bedtime_sleep.FIXED_WINDOW]
    if oura and fixed:
        return (
            f"Bedtime is the start of her Oura main sleep period on {bedtime_sleep.nights_text(oura)}; "
            f"the other {bedtime_sleep.nights_text(fixed)} had no Oura bedtime between 18:00 and 06:00 "
            "and use a fixed 22:30 to 00:30 window."
        )
    if oura:
        return (
            f"Bedtime is the start of her Oura main sleep period on {bedtime_sleep.all_nights_text(oura)}."
        )
    if fixed:
        return (
            f"Bedtime is a fixed 22:30 to 00:30 window on {bedtime_sleep.all_nights_text(fixed)}, not "
            "her recorded bedtime: none had an Oura bedtime between 18:00 and 06:00."
        )
    return (
        "Bedtime is the start of her Oura main sleep period, or a fixed 22:30 to 00:30 window "
        "on nights without one."
    )


def bedtime_item(
    analysis: dict[str, Any],
    *,
    start: date,
    end: date,
) -> dict[str, Any]:
    """Bedtime glucose against that night's sleep, from bedtime_sleep.analyze()."""
    window = _window(start, end)
    nights = analysis["nights"]
    findings = []
    measured = analysis["nights_measured"]
    if measured:
        over = analysis["nights_over_180"]
        findings.append(
            f"Bedtime glucose ({_bedtime_measure(analysis['bedtime_basis_nights'])}) was measured "
            f"on {measured} nights from "
            f"{analysis['first_night']} to {analysis['last_night']}; it was above "
            f"{bedtime_sleep.HIGH_BEDTIME_MG_DL} mg/dL on "
            + (f"{over} of them" if over else "none of them")
            + f" (highest {analysis['highest_bedtime_glucose']} mg/dL)."
        )
    for band, _low, _high in bedtime_sleep.BANDS:
        group = analysis["bands"][band]
        count = analysis["band_nights"][band]
        label = _BAND_LABELS[band]
        if group:
            findings.append(
                f"Nights going to bed {label} ({group['nights']} nights): median sleep "
                f"{group['median_sleep_hours']} hours, sleep score {group['median_sleep_score']}; "
                f"next-day mean glucose {group['median_next_day_glucose']} mg/dL."
            )
        elif count:
            findings.append(f"Only {count} nights went to bed {label}, too few to summarize.")
    dinner = analysis["dinner"]
    if dinner["late"] and dinner["earlier"]:
        late, earlier = dinner["late"], dinner["earlier"]
        findings.append(
            f"Dinners logged at or after 20:00 ({late['nights']} nights): median bedtime glucose "
            f"{late['median_bedtime_glucose']} mg/dL and sleep {late['median_sleep_hours']} hours; earlier "
            f"dinners ({earlier['nights']} nights): {earlier['median_bedtime_glucose']} mg/dL and "
            f"{earlier['median_sleep_hours']} hours."
        )
    elif dinner["nights_logged"]:
        findings.append(
            f"A dinner time was logged on {dinner['nights_logged']} of these nights, too few to compare "
            "late and earlier dinners."
        )
    else:
        findings.append(
            "No dinner times were logged on these nights, so late and earlier dinners cannot be compared."
        )
    if not measured:
        findings = ["No night in this window had enough CGM readings at bedtime to measure."]
    slept = [row for row in nights if row["sleep_hours"] is not None]
    envelope = comparison_confidence(
        [row["sleep_hours"] for row in slept if row["bedtime_glucose"] > 140],
        [row["sleep_hours"] for row in slept if row["bedtime_glucose"] <= 140],
        valid_days=len(slept),
        expected_days=window["days"],
        temporal_direction="bedtime-glucose-then-sleep",
        unit="hours",
    )
    data = {
        "title": "Bedtime glucose and sleep",
        "summary": (
            f"Bedtime glucose and that night's Oura sleep from {window['start']} to {window['end']}: "
            f"{measured} nights measured, {analysis['nights_with_sleep']} with sleep data."
        ),
        "findings": findings,
        "window": window,
        "sample_count": measured,
        "algorithm_version": analysis["algorithm_version"],
        "supporting_data": {
            "bands": analysis["bands"],
            "band_nights": analysis["band_nights"],
            "bedtime_basis_nights": analysis["bedtime_basis_nights"],
            "dinner": dinner,
            "highest_bedtime_glucose": analysis["highest_bedtime_glucose"],
            "nights_over_180": analysis["nights_over_180"],
        },
        "semantics": analysis["semantics"],
    }
    refs = _recent([("OuraDaily", row["sleep_entity_id"]) for row in slept if row.get("sleep_entity_id")])
    return {
        "data": data,
        "confidence": _confidence(envelope, [
            _bedtime_limitation(analysis["bedtime_basis_nights"]),
            "Sleep figures are Oura's estimates.",
            "Dinner timing comes only from logged carbohydrate entries.",
        ]),
        "source_refs": refs,
    }
