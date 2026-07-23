"""Oura nightly HRV: which sleep period counts, and what the scores are named.

Oura's `daily_sleep` endpoint returns 0-100 contributor sub-scores; the measured
HRV and real stage durations only exist on the per-period `sleep` endpoint. These
tests pin both halves of that split, plus the nap-vs-night choice that decides
which HRV number represents a day.
"""

import json
import sqlite3

from server.migrations import MIGRATIONS
from server.oura import _main_sleep_by_day, _process_daily


def _night(day, hrv, duration=28800, **extra):
    return {
        "day": day,
        "type": "long_sleep",
        "average_hrv": hrv,
        "total_sleep_duration": duration,
        **extra,
    }


def _nap(day, hrv, duration=1800):
    return {"day": day, "type": "sleep", "average_hrv": hrv, "total_sleep_duration": duration}


def test_nap_never_supplants_the_night():
    """A short afternoon nap carries its own HRV — the night still wins."""
    periods = [_nap("2026-07-18", 21), _night("2026-07-18", 34)]

    assert _main_sleep_by_day(periods)["2026-07-18"]["average_hrv"] == 34
    # ...and the order it arrives in must not change the answer.
    assert _main_sleep_by_day(list(reversed(periods)))["2026-07-18"]["average_hrv"] == 34


def test_nap_is_used_only_when_it_is_the_whole_day():
    """If the ring recorded no long_sleep, the nap is all there is."""
    assert _main_sleep_by_day([_nap("2026-07-21", 17)])["2026-07-21"]["average_hrv"] == 17


def test_split_night_keeps_the_longer_stretch():
    periods = [_night("2026-07-19", 22, duration=3600), _night("2026-07-19", 31, duration=25200)]
    assert _main_sleep_by_day(periods)["2026-07-19"]["average_hrv"] == 31


def test_period_without_a_day_is_skipped():
    assert _main_sleep_by_day([{"type": "long_sleep", "average_hrv": 40}]) == {}


def test_contributor_scores_and_measured_durations_stay_apart():
    """The 0-100 sub-scores and the real seconds must not land on one name."""
    daily_sleep = [
        {
            "day": "2026-07-22",
            "score": 84,
            "contributors": {
                "total_sleep": 99,
                "efficiency": 98,
                "rem_sleep": 96,
                "deep_sleep": 81,
                "latency": 90,
            },
        }
    ]
    periods = [
        _night(
            "2026-07-22",
            26,
            duration=32160,
            deep_sleep_duration=4500,
            rem_sleep_duration=6990,
            light_sleep_duration=20670,
            awake_time=2397,
            latency=870,
            time_in_bed=34557,
            efficiency=93,
            average_breath=13.875,
        )
    ]

    day = _process_daily(daily_sleep, periods, [], [], [], [])["2026-07-22"]

    assert day["hrv"] == 26
    assert day["breathing_rate"] == 13.875
    # Scores keep their 0-100 identity under a `_score` name...
    assert day["sleep_total_score"] == 99
    assert day["sleep_efficiency_score"] == 98
    assert day["sleep_deep_score"] == 81
    # ...while `_seconds` names carry seconds, which is what they claim.
    assert day["sleep_total_seconds"] == 32160
    assert day["sleep_deep_seconds"] == 4500
    assert day["sleep_latency_seconds"] == 870
    assert day["time_in_bed_seconds"] == 34557
    assert day["sleep_efficiency"] == 93
    assert day["sleep_score"] == 84


def test_day_with_only_scores_has_no_measured_hrv():
    """No sleep period (ring not worn) means no HRV — never a fabricated one."""
    daily_sleep = [{"day": "2026-07-20", "score": 70, "contributors": {"total_sleep": 88}}]
    day = _process_daily(daily_sleep, [], [], [], [], [])["2026-07-20"]

    assert day.get("hrv") is None
    assert day["sleep_total_score"] == 88
    assert "sleep_total_seconds" not in day


def _relabel_migration():
    return next(m for m in MIGRATIONS if m.name == "oura_sleep_score_relabel")


def _oura_row(connection, entity_id, payload):
    connection.execute(
        "INSERT INTO entities (id, type, data, created_date, updated_date) VALUES (?,?,?,?,?)",
        (entity_id, "OuraDaily", json.dumps(payload), "2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z"),
    )


def _fetch(connection, entity_id):
    row = connection.execute("SELECT data FROM entities WHERE id=?", (entity_id,)).fetchone()
    return json.loads(row[0])


def test_migration_relabels_scores_but_spares_real_durations(tmp_path):
    """The <=100 guard is what separates a stored score from stored seconds."""
    path = tmp_path / "relabel.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE entities (
                id TEXT PRIMARY KEY, type TEXT NOT NULL, data TEXT NOT NULL,
                created_date TEXT NOT NULL, updated_date TEXT NOT NULL
            )
            """
        )
        # A legacy row: contributor scores sitting under `_seconds` names.
        _oura_row(connection, "legacy", {"date": "2026-07-01", "sleep_total_seconds": 100,
                                         "sleep_deep_seconds": 97, "sleep_efficiency": 95})
        # A row already holding genuine seconds must be left completely alone.
        _oura_row(connection, "measured", {"date": "2026-07-02", "sleep_total_seconds": 32160,
                                           "sleep_deep_seconds": 4500})

        for statement in _relabel_migration().statements:
            connection.execute(statement.sql, statement.parameters)

        legacy = _fetch(connection, "legacy")
        assert legacy["sleep_total_score"] == 100
        assert legacy["sleep_deep_score"] == 97
        assert legacy["sleep_efficiency_score"] == 95
        assert "sleep_total_seconds" not in legacy
        assert "sleep_efficiency" not in legacy

        measured = _fetch(connection, "measured")
        assert measured["sleep_total_seconds"] == 32160
        assert measured["sleep_deep_seconds"] == 4500
        assert "sleep_total_score" not in measured


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "twice.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE entities (
                id TEXT PRIMARY KEY, type TEXT NOT NULL, data TEXT NOT NULL,
                created_date TEXT NOT NULL, updated_date TEXT NOT NULL
            )
            """
        )
        _oura_row(connection, "row", {"date": "2026-07-01", "sleep_total_seconds": 68})
        statements = _relabel_migration().statements
        for _ in range(2):
            for statement in statements:
                connection.execute(statement.sql, statement.parameters)

        row = _fetch(connection, "row")
        assert row["sleep_total_score"] == 68
        assert "sleep_total_seconds" not in row
