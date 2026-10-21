"""Overview wearable averages ignore the 0/0.0 placeholders devices write on
days with no reading. All rows are synthetic."""

from types import SimpleNamespace

from server import health_summary
from server.wellness_ranges import plausible_avg


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def query(self, *_args, **_kwargs):
        return list(self.rows)


def _day(hrv, rhr, sleep, breathing, spo2):
    return {"hrv": hrv, "resting_heart_rate": rhr, "sleep_minutes": sleep,
            "breathing_rate": breathing, "spo2_avg": spo2}


def _trends(monkeypatch, fitbit, oura=()):
    repos = SimpleNamespace(fitbit_daily=_Rows(fitbit), oura_daily=_Rows(oura))
    monkeypatch.setattr(health_summary, "get_repositories", lambda: repos)
    monkeypatch.setattr(health_summary, "config_value", lambda _key, default: default)
    monkeypatch.setattr(health_summary, "assess_daily", lambda *_a, **_k: {})
    return health_summary._wearable_trends()["metrics"]


def test_zero_placeholder_days_are_excluded_from_recent_and_prior(monkeypatch):
    recent = [_day(40, 60, 420, 15, 96)] * 7 + [_day(0, 0, 0, 0.0, 0.0)] * 7
    prior = [_day(50, 58, 450, 14, 97)] * 14 + [_day(0, 0, 0, 0.0, 0.0)] * 14
    metrics = _trends(monkeypatch, recent + prior)

    assert metrics["hrv"] == {"recent": 40.0, "prior": 50.0}
    assert metrics["resting_heart_rate"] == {"recent": 60.0, "prior": 58.0}
    assert metrics["sleep_minutes"] == {"recent": 420.0, "prior": 450.0}
    assert metrics["breathing_rate"] == {"recent": 15.0, "prior": 14.0}
    assert metrics["spo2_avg"] == {"recent": 96.0, "prior": 97.0}


def test_metric_with_only_placeholders_is_omitted(monkeypatch):
    rows = [_day(40, 60, 420, 15, 0.0)] * 14
    metrics = _trends(monkeypatch, rows)

    assert "spo2_avg" not in metrics
    assert metrics["hrv"]["recent"] == 40.0
    assert metrics["hrv"]["prior"] is None


def test_oura_hrv_excludes_zero_nights(monkeypatch):
    oura = [{"hrv": 30}] * 10 + [{"hrv": 0}] * 4
    metrics = _trends(monkeypatch, [], oura)

    assert metrics["hrv_oura"]["recent"] == 30.0


def test_plausible_avg_bounds_and_bad_values():
    rows = [{"spo2_avg": v} for v in (95, 69.9, 100.1, None, "n/a", float("nan"), 97)]
    assert plausible_avg(rows, "spo2_avg") == 96.0
    assert plausible_avg([{"hrv": 301}, {"hrv": 300}], "hrv") == 300.0
    assert plausible_avg([{"breathing_rate": 3}, {"breathing_rate": 61}], "breathing_rate") is None
    # Fields without a range keep every finite value, zero included.
    assert plausible_avg([{"other": 0}, {"other": 4}], "other") == 2.0
