"""Derived analyses as Evidence Bundle items the Companion can cite.

Synthetic data only: two weeks of a repeating CGM day with a daily walk, a
daily temp basal correction at a peak, and nightly Oura sleep.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from server import analysis_evidence, companion_evidence, db
from server.activity_position import SqliteActivityPositionRepository
from server.evidence_bundle import (
    EvidenceBundleQuery,
    EvidenceDomain,
    build_bundle,
    clear_bundle_cache,
)
from server.migrations import run_migrations

pytestmark = pytest.mark.risk_critical
OWNER = "owner@glucopilot.local"
AS_OF = date(2026, 9, 20)
DAYS = 14


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _glucose(t: datetime) -> float:
    """120 baseline; a daily climb to 170 at 18:00 UTC that falls back by 21:00."""
    minutes = t.hour * 60 + t.minute
    if 17 * 60 + 30 <= minutes < 18 * 60:
        return 120 + 50 * (minutes - (17 * 60 + 30)) / 30
    if 18 * 60 <= minutes < 21 * 60:
        return 170 - 60 * (minutes - 18 * 60) / 180
    return 120


@pytest.fixture
def analysis_database(tmp_path, monkeypatch):
    database = tmp_path / "data" / "app.sqlite3"
    database.parent.mkdir()
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(
        companion_evidence,
        "refresh_contradictions",
        lambda: {"enabled": True, "refreshed": False},
    )
    monkeypatch.setenv("RELATIONSHIP_READS_ENABLED", "false")
    monkeypatch.setenv("EVIDENCE_SET_READS_ENABLED", "false")
    clear_bundle_cache()

    first = datetime.combine(AS_OF - timedelta(days=DAYS), datetime.min.time(), tzinfo=timezone.utc)
    readings = []
    t = first
    while t < first + timedelta(days=DAYS + 1):
        readings.append({
            "owner_email": OWNER,
            "timestamp": _iso(t),
            "value": round(_glucose(t)),
            "source": "synthetic-cgm",
        })
        t += timedelta(minutes=5)
    db.bulk_create_entities("GlucoseReading", readings)

    intervals = SqliteActivityPositionRepository()
    treatments = []
    sleep = []
    for day in range(DAYS):
        midnight = first + timedelta(days=day)
        walk = midnight + timedelta(hours=15)  # 11:00 in America/New_York
        intervals.upsert_wearable(
            start_time=walk,
            end_time=walk + timedelta(minutes=20),
            activity="walking",
            source_label="synthetic wearable",
            source_entity_type="FitbitDaily",
            source_entity_id=f"synthetic-walk-{day}",
            source_payload={"day": day},
            confidence_score=0.9,
            limitations=["Synthetic interval."],
        )
        treatments.append({
            "owner_email": OWNER,
            "timestamp": _iso(midnight + timedelta(hours=18)),
            "type": "tempbasal_correction",
            "event_type": "Temp Basal",
            "absolute": 2.0,
            "multiplier": 2.0,
            "duration": 90,
            "source": "synthetic-pump",
        })
        sleep.append({
            "owner_email": OWNER,
            "date": (midnight + timedelta(days=1)).date().isoformat(),
            "sleep_total_seconds": 8.5 * 3600,
            "sleep_score": 85,
            "sleep_efficiency": 90,
        })
    db.bulk_create_entities("Treatment", treatments)
    db.bulk_create_entities("OuraDaily", sleep)
    return database


def _query(**kwargs) -> EvidenceBundleQuery:
    return EvidenceBundleQuery(
        start=datetime.combine(AS_OF - timedelta(days=DAYS), datetime.min.time(), tzinfo=timezone.utc),
        end=datetime.combine(AS_OF, datetime.max.time(), tzinfo=timezone.utc),
        question_intent="glucose sitting temp basal bedtime sleep",
        item_budget=20,
        **kwargs,
    )


def test_bundle_returns_each_analysis_only_when_named(analysis_database):
    bundle = build_bundle(_query(
        domains=[EvidenceDomain.GLUCOSE],
        normalized_entity_types=analysis_evidence.ENTITY_TYPES,
    ))
    items = {item["entity_type"]: item for item in bundle["evidence"]["derived_metrics"]}
    assert set(items) == set(analysis_evidence.ENTITY_TYPES)
    for item in items.values():
        assert item["domain"] == "glucose"
        assert item["data"]["findings"]
        # Local (America/New_York) dates the UTC range touches: the evening before
        # the first UTC midnight through AS_OF.
        assert item["data"]["window"] == {
            "start": (AS_OF - timedelta(days=DAYS + 1)).isoformat(),
            "end": AS_OF.isoformat(),
            "days": DAYS + 2,
        }
        assert item["confidence"]["limitations"]
        assert item["confidence"]["label"] in {"low", "medium", "high"}
        assert 0 < len(item["source_links"]) <= analysis_evidence.MAX_SOURCE_REFS
    assert {link["kind"] for link in items["WalkingResponseSummary"]["source_links"]} == {"canonical_interval"}
    assert {link["entity_type"] for link in items["CorrectionResponseSummary"]["source_links"]} == {"Treatment"}
    assert {link["entity_type"] for link in items["BedtimeSleepSummary"]["source_links"]} == {"OuraDaily"}

    broad = build_bundle(_query(domains=[EvidenceDomain.GLUCOSE, EvidenceDomain.INSULIN]))
    assert not [
        item for item in broad["evidence"]["derived_metrics"]
        if item["entity_type"] in analysis_evidence.ENTITY_TYPES
    ]


def test_each_analysis_reports_her_numbers(analysis_database):
    bundle = build_bundle(_query(
        domains=[EvidenceDomain.GLUCOSE],
        normalized_entity_types=analysis_evidence.ENTITY_TYPES,
    ))
    items = {item["entity_type"]: item for item in bundle["evidence"]["derived_metrics"]}
    bedtime = items["BedtimeSleepSummary"]["data"]
    assert bedtime["supporting_data"]["nights_over_180"] == 0
    assert bedtime["supporting_data"]["bands"]["under_140"]["median_sleep_hours"] == 8.5
    correction = items["CorrectionResponseSummary"]["data"]
    assert correction["supporting_data"]["timecourse"]["n"] == DAYS
    assert correction["supporting_data"]["timecourse"]["median_start_glucose"] == 170
    walking = items["WalkingResponseSummary"]["data"]
    assert walking["supporting_data"]["bands"]["under_120"]["walking"]["n"] == DAYS


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Why does my blood sugar keep going up when I sit down?", "WalkingResponseSummary"),
        ("Why does the temp basal need two hours before it starts working?", "CorrectionResponseSummary"),
        ("If I eat dinner late I'll be high at bedtime and won't sleep", "BedtimeSleepSummary"),
    ],
)
def test_companion_leads_with_the_matching_analysis(analysis_database, question, expected):
    public, reasoning = companion_evidence.build_context(question, as_of=AS_OF, refresh=False)
    first = reasoning["items"][0]
    assert first["alias"] == "E1" and first["entity_type"] == expected
    assert public["evidence_items"][0]["entity_type"] == expected
    assert "analyses" in {scope["scope"] for scope in public["scopes"]}
    prompt = companion_evidence.prompt_context(reasoning)
    assert first["data"]["findings"][0][:60] in prompt

    reply, evidence = companion_evidence.finalize_reply(
        f"Your glucose data answers this directly. {first['data']['findings'][0]} [E1]",
        public,
        [],
        [],
    )
    assert evidence["omissions"]["count"] == 0
    assert "[E1]" in reply
    statement = evidence["statements"][0]
    assert statement["evidence_aliases"] == ["E1"]
    assert statement["source_links"]


def test_every_finding_restated_with_its_alias_is_kept(analysis_database):
    """The misstated-analysis check reads numbers from the findings the model was
    shown; each finding, cited as itself, must pass it."""
    public, reasoning = companion_evidence.build_context(
        "Does sitting raise my glucose, does a temp basal need two hours, and does a high bedtime cost sleep?",
        as_of=AS_OF,
        refresh=False,
    )
    analyses = [item for item in reasoning["items"] if item["entity_type"] in analysis_evidence.ENTITY_TYPES]
    assert {item["entity_type"] for item in analyses} == set(analysis_evidence.ENTITY_TYPES)
    public_by_alias = {item["alias"]: item for item in public["evidence_items"]}
    prompt = companion_evidence.prompt_context(reasoning)
    for item in analyses:
        assert public_by_alias[item["alias"]]["analysis"]["findings"] == item["data"]["findings"]
        assert "analysis" not in item
        reply, evidence = companion_evidence.finalize_reply(
            "\n".join(f"{finding} [{item['alias']}]" for finding in item["data"]["findings"]),
            public,
            [],
            [],
        )
        assert evidence["omissions"]["count"] == 0, reply
        assert len(evidence["statements"]) == len(item["data"]["findings"])
    assert '"analysis":' not in prompt


def test_unrelated_questions_do_not_compute_analyses(analysis_database):
    public, reasoning = companion_evidence.build_context(
        "How is my thyroid doing?", as_of=AS_OF, refresh=False
    )
    assert "analyses" not in {scope["scope"] for scope in public["scopes"]}
    assert not [
        item for item in reasoning["items"]
        if item["entity_type"] in analysis_evidence.ENTITY_TYPES
    ]
