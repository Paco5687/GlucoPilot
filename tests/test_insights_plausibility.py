"""Device placeholder values must not become wearable correlations."""

from server import insights


def test_zero_spo2_nights_are_missing_values_not_readings():
    glucose, wearables = {}, {}
    for day in range(30):
        key = f"2026-06-{day + 1:02d}"
        unmeasured = day % 3 == 0
        # Unmeasured nights happen to fall on high-glucose days: averaging the
        # 0.0 placeholders in would manufacture a strong SpO2/glucose correlation.
        glucose[key] = {"avg": 190.0 if unmeasured else 120.0, "tir": 60.0, "cv": 30.0}
        wearables[key] = {"spo2_average": 0.0 if unmeasured else 96.0}

    candidates = insights._correlation_candidates(glucose, wearables)

    assert not [candidate for candidate in candidates if "SpO2" in candidate["x"]]


def test_plausibility_bounds_only_apply_to_bounded_fields():
    assert insights._plausible("spo2_average", 0.0) is None
    assert insights._plausible("spo2_average", "97.5") == 97.5
    assert insights._plausible("spo2_average", None) is None
    assert insights._plausible("sleep_score", 0) == 0
