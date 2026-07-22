"""Cycle-phase inference from Oura nightly temperature deviation.

Nightly body temperature is biphasic across the menstrual cycle: it rises
~0.2-0.5 °C after ovulation (progesterone) and falls back at menses. This is
the same signal Lively's Oura integration consumes; we compute phases directly
from the feed we already have.

Method (fertility-awareness "three over six" style, on a smoothed series):
  1. 3-day moving average over readiness_temperature_deviation, interpolating
     single-day gaps.
  2. Ovulation shift: first day where this day and the next two all exceed the
     mean of the previous 6 days by >= SHIFT_THRESHOLD.
  3. Menses onset: after a shift, the day the smoothed temp falls back below
     that cycle's pre-shift baseline (or a max-luteal timeout).
  4. Phases: menstrual = onset + MENSTRUAL_DAYS; ovulation = shift day ± 1;
     follicular between; luteal after shift until next onset.

Output: PeriodLog rows with source="oura_inferred", phase + notes. Rows from
any other source (manual logging, Lively import) always win — inference only
fills empty dates, and reruns wipe/regenerate only its own rows. Ambiguous
stretches (missing temp, no confirmed pattern) are left unassigned rather
than guessed.
"""

import logging
from datetime import date, timedelta
from typing import Any

from . import db
from .config import OWNER_EMAIL
from .repositories import get_repositories

log = logging.getLogger("glucopilot.cycle")

SHIFT_THRESHOLD = 0.15  # °C above the prior-6-day mean
MENSTRUAL_DAYS = 5
MIN_LUTEAL_DAYS = 8
MAX_LUTEAL_DAYS = 18  # timeout: assume menses even without a clean temp drop
MIN_CYCLE_GAP = 15  # ignore shift candidates closer than this to the last one


def _load_temps() -> list[tuple[date, float]]:
    rows = get_repositories().oura_daily.query({"owner_email": OWNER_EMAIL}, "date", 2000)
    series = []
    for r in rows:
        t = r.get("readiness_temperature_deviation")
        d = r.get("date")
        if t is None or not d:
            continue
        try:
            series.append((date.fromisoformat(d), float(t)))
        except ValueError:
            continue
    return series


def _smooth(series: list[tuple[date, float]]) -> dict[date, float]:
    """Interpolate 1-day gaps, then 3-day centered moving average."""
    if not series:
        return {}
    filled: dict[date, float] = {}
    for i, (d, t) in enumerate(series):
        filled[d] = t
        if i + 1 < len(series):
            nd, nt = series[i + 1]
            if (nd - d).days == 2:  # single missing day
                filled[d + timedelta(days=1)] = (t + nt) / 2
    days = sorted(filled)
    smoothed = {}
    for d in days:
        window = [filled[d + timedelta(days=o)] for o in (-1, 0, 1) if d + timedelta(days=o) in filled]
        smoothed[d] = sum(window) / len(window)
    return smoothed


def _detect(smoothed: dict[date, float]) -> tuple[list[date], list[date]]:
    """Return (ovulation shift days, menses onset days)."""
    days = sorted(smoothed)
    shifts: list[date] = []
    onsets: list[date] = []
    last_shift: date | None = None
    i = 6
    while i < len(days) - 2:
        d = days[i]
        prior = [smoothed[days[j]] for j in range(i - 6, i)]
        if len(prior) < 6:
            i += 1
            continue
        baseline = sum(prior) / len(prior)
        window = [smoothed[days[i + k]] for k in range(3) if i + k < len(days)]
        if len(window) == 3 and all(v >= baseline + SHIFT_THRESHOLD for v in window):
            if last_shift is None or (d - last_shift).days >= MIN_CYCLE_GAP:
                shifts.append(d)
                last_shift = d
                # find the menses onset after this shift
                onset = None
                for j in range(i + MIN_LUTEAL_DAYS, min(i + MAX_LUTEAL_DAYS + 1, len(days))):
                    if smoothed[days[j]] <= baseline + 0.05:
                        onset = days[j]
                        break
                if onset is None and i + MAX_LUTEAL_DAYS < len(days):
                    onset = days[i + MAX_LUTEAL_DAYS]
                if onset:
                    onsets.append(onset)
                i += MIN_LUTEAL_DAYS
                continue
        i += 1
    return shifts, onsets


def _assign_phases(smoothed: dict[date, float], shifts: list[date], onsets: list[date]) -> dict[date, str]:
    phases: dict[date, str] = {}
    events: list[tuple[date, str]] = sorted(
        [(d, "ovulation") for d in shifts] + [(d, "menses") for d in onsets]
    )
    if not events:
        return phases

    for idx, (d, kind) in enumerate(events):
        next_event = events[idx + 1][0] if idx + 1 < len(events) else None
        if kind == "ovulation":
            for offset in (-1, 0, 1):
                phases[d + timedelta(days=offset)] = "ovulation"
            # luteal from after ovulation until next event
            end = next_event or (d + timedelta(days=MAX_LUTEAL_DAYS))
            cursor = d + timedelta(days=2)
            while cursor < end:
                phases.setdefault(cursor, "luteal")
                cursor += timedelta(days=1)
        else:  # menses onset
            for offset in range(MENSTRUAL_DAYS):
                phases.setdefault(d + timedelta(days=offset), "menstrual")
            # follicular from after menstrual until next event
            end = next_event or (d + timedelta(days=MENSTRUAL_DAYS + 14))
            cursor = d + timedelta(days=MENSTRUAL_DAYS)
            while cursor < end:
                phases.setdefault(cursor, "follicular")
                cursor += timedelta(days=1)

    # only keep dates we actually have temp coverage for (no future guessing)
    have = set(smoothed)
    return {d: p for d, p in phases.items() if d in have}


async def infer() -> dict[str, Any]:
    series = _load_temps()
    if len(series) < 30:
        return {"ok": False, "message": f"Not enough temperature data ({len(series)} days; need 30+)."}

    smoothed = _smooth(series)
    shifts, onsets = _detect(smoothed)
    phases = _assign_phases(smoothed, shifts, onsets)

    # cycle stats from consecutive onsets
    onset_sorted = sorted(onsets)
    lengths = [(b - a).days for a, b in zip(onset_sorted, onset_sorted[1:]) if 15 <= (b - a).days <= 60]
    avg_len = round(sum(lengths) / len(lengths), 1) if lengths else None

    # replace only our own rows; every other source wins
    for old in db.query_entities("PeriodLog", {"owner_email": OWNER_EMAIL, "source": "oura_inferred"}):
        db.delete_entity("PeriodLog", old["id"])
    taken = {
        r.get("date")
        for r in db.query_entities("PeriodLog", {"owner_email": OWNER_EMAIL}, "date", 5000)
    }

    to_create = [
        {
            "date": d.isoformat(),
            "phase": phase,
            "source": "oura_inferred",
            "notes": "Inferred from Oura nightly temperature",
            "owner_email": OWNER_EMAIL,
        }
        for d, phase in sorted(phases.items())
        if d.isoformat() not in taken
    ]
    if to_create:
        db.bulk_create_entities("PeriodLog", to_create)

    result = {
        "ok": True,
        "days_analyzed": len(series),
        "ovulations_detected": len(shifts),
        "menses_onsets_detected": len(onsets),
        "phase_days_written": len(to_create),
        "estimated_cycle_starts": [d.isoformat() for d in onset_sorted],
        "avg_cycle_length_days": avg_len,
    }
    log.info("cycle inference: %s", result)
    return result
