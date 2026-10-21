"""Plausible ranges for daily wearable metrics.

Devices write 0 or 0.0 on days with no reading (no SpO2 captured, ring not
worn), and averaging those in drags a summary toward a value that was never
measured. Values outside these ranges are treated as missing.
"""

import math

WELLNESS_PLAUSIBLE: dict[str, tuple[float, float]] = {
    "sleep_score": (1, 100),
    "readiness_score": (1, 100),
    "lowest_heart_rate": (25, 200),
    "resting_heart_rate": (25, 200),
    "spo2_average": (70, 100),
    "spo2_avg": (70, 100),
    "steps": (1, 100_000),
    "sleep_minutes": (1, 24 * 60),
    "hrv": (1, 300),
    "breathing_rate": (4, 60),
}


def plausible_avg(rows: list[dict], field: str) -> float | None:
    """Mean of the finite values of `field` inside its plausible range
    (unbounded for fields without one); None when nothing qualifies."""
    low, high = WELLNESS_PLAUSIBLE.get(field, (-math.inf, math.inf))
    vals = []
    for r in rows:
        try:
            value = float(r.get(field))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and low <= value <= high:
            vals.append(value)
    return sum(vals) / len(vals) if vals else None
