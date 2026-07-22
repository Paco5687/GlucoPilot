from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from server import db, patterns
from server.backup import create_verified_backup, verify_backup
from server.evidence_sets import (
    EvidenceSetError,
    SqliteEvidenceSetRepository,
    StaleEvidenceError,
)
from server.migrations import run_migrations


pytestmark = pytest.mark.risk_critical
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "golden" / "evidence_windows.json"


@pytest.fixture(scope="module")
def cases():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def evidence_database(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database = data_dir / "app.sqlite3"
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    return database


def _readings(cases):
    return [
        db.create_entity("GlucoseReading", {**item, "owner_email": cases["owner_email"]})
        for item in cases["observations"]
    ]


def _capture(repository, cases, readings):
    return repository.capture_window(
        owner_email=cases["owner_email"],
        entity_type="GlucoseReading",
        time_field="timestamp",
        window_start=cases["window_start"],
        window_end=cases["window_end"],
        observations=readings,
        filters={"source": "synthetic"},
        summary={"minimum": 101, "maximum": 123},
    )


def test_fixture_is_public_safe(cases):
    encoded = json.dumps(cases).lower()
    assert cases["synthetic"] is True
    assert cases["owner_email"] == "owner@glucopilot.local"
    assert "token" not in encoded
    assert "password" not in encoded


def test_migration_adds_bounded_indexed_evidence_storage(evidence_database):
    with sqlite3.connect(evidence_database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"observation_windows", "evidence_sets", "evidence_set_windows"} <= tables
        strict = {row[1]: row[5] for row in connection.execute("PRAGMA table_list")}
        assert strict["observation_windows"] == 1
        indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {
            "idx_observation_windows_owner_range",
            "idx_observation_windows_checksum",
            "idx_evidence_sets_claim",
            "idx_evidence_set_windows_window",
        } <= indexes


def test_window_is_deterministic_checksum_addressed_and_has_no_per_sample_edges(
    cases, evidence_database
):
    readings = _readings(cases)
    repository = SqliteEvidenceSetRepository(database=evidence_database)
    first = _capture(repository, cases, readings)
    second = _capture(repository, cases, list(reversed(readings)))
    assert first["id"] == second["id"]
    assert first["observation_checksum"].startswith("sha256:")
    assert first["query_checksum"].startswith("sha256:")
    assert first["observation_count"] == 3
    assert first["member_ids"] == [item["id"] for item in readings]
    with sqlite3.connect(evidence_database) as connection:
        assert connection.execute("SELECT count(*) FROM observation_windows").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM entity_relationships").fetchone() == (0,)


def test_drill_down_reproduces_exact_included_observations(cases, evidence_database):
    readings = _readings(cases)
    repository = SqliteEvidenceSetRepository(database=evidence_database)
    window = _capture(repository, cases, readings)
    drilled = repository.drill_down(window["id"])
    assert [item["id"] for item in drilled] == window["member_ids"]
    assert [item["value"] for item in drilled] == [101, 112, 123]


def test_source_change_invalidates_window_and_blocks_stale_drill_down(cases, evidence_database):
    readings = _readings(cases)
    repository = SqliteEvidenceSetRepository(database=evidence_database)
    window = _capture(repository, cases, readings)
    pattern = db.create_entity("Pattern", {"owner_email": cases["owner_email"]})
    evidence = repository.create_set(
        owner_email=cases["owner_email"],
        claim_type="Pattern",
        claim_id=pattern["id"],
        window_ids=[window["id"]],
        summary={},
        input_data_version="synthetic",
    )
    db.update_entity("GlucoseReading", readings[1]["id"], {"value": 199})
    assert repository.validate_window(window["id"]) is False
    assert repository.get_window(window["id"])["status"] == "invalidated"
    with sqlite3.connect(evidence_database) as connection:
        assert connection.execute(
            "SELECT status FROM evidence_sets WHERE id=?", (evidence["id"],)
        ).fetchone() == ("invalidated",)
    db.update_entity("GlucoseReading", readings[1]["id"], {"value": 112})
    assert repository.validate_window(window["id"]) is False
    with pytest.raises(StaleEvidenceError, match="stale"):
        repository.drill_down(window["id"])


def test_pattern_claim_cites_one_window_not_each_observation(cases, evidence_database):
    readings = _readings(cases)
    pattern = db.create_entity(
        "Pattern", {"owner_email": cases["owner_email"], "title": "Synthetic pattern"}
    )
    repository = SqliteEvidenceSetRepository(database=evidence_database)
    window = _capture(repository, cases, readings)
    evidence = repository.create_set(
        owner_email=cases["owner_email"],
        claim_type="Pattern",
        claim_id=pattern["id"],
        window_ids=[window["id"]],
        summary={"finding": "synthetic"},
        input_data_version="sha256:" + "d" * 64,
    )
    references = repository.for_claim(cases["owner_email"], "Pattern", pattern["id"])
    assert evidence["set_checksum"].startswith("sha256:")
    assert len(references) == 1
    assert references[0].value["window_ids"] == [window["id"]]
    assert repository.for_claim("other@glucopilot.local", "Pattern", pattern["id"]) == []
    with sqlite3.connect(evidence_database) as connection:
        assert connection.execute("SELECT count(*) FROM evidence_set_windows").fetchone() == (1,)


def test_bounds_owner_and_claim_validation(cases, evidence_database, monkeypatch):
    readings = _readings(cases)
    repository = SqliteEvidenceSetRepository(database=evidence_database)
    monkeypatch.setattr("server.evidence_sets.MAX_OBSERVATIONS", 2)
    with pytest.raises(EvidenceSetError, match="bounded"):
        _capture(repository, cases, readings)
    monkeypatch.setattr("server.evidence_sets.MAX_OBSERVATIONS", 100_000)
    window = _capture(repository, cases, readings)
    with pytest.raises(EvidenceSetError, match="owner scope"):
        repository.create_set(
            owner_email="other@glucopilot.local",
            claim_type="Pattern",
            claim_id="missing",
            window_ids=[window["id"]],
            summary={},
            input_data_version="synthetic",
        )


def test_pattern_writer_cites_one_bounded_window_when_enabled(
    cases, evidence_database, monkeypatch
):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = now - timedelta(days=14)
    readings = []
    cursor = start
    while cursor <= now:
        readings.append(
            {
                "timestamp": cursor.isoformat().replace("+00:00", "Z"),
                "value": 220 if cursor.hour == 14 else 110,
                "source": "synthetic-cgm",
                "owner_email": cases["owner_email"],
            }
        )
        cursor += timedelta(minutes=5)
    db.bulk_create_entities("GlucoseReading", readings)

    async def no_enrichment(*args, **kwargs):
        return {"patterns": []}

    monkeypatch.setattr(patterns, "invoke_llm", no_enrichment)
    monkeypatch.setenv("EVIDENCE_SET_WRITES_ENABLED", "true")
    outcome = asyncio.run(patterns.analyze())
    assert outcome["patternsFound"] >= 1
    stored_patterns = db.query_entities("Pattern", {"owner_email": cases["owner_email"]})
    assert all(pattern["analytics_confidence"]["version"] == "analytics-confidence/1.0.0" for pattern in stored_patterns)
    assert all(pattern["analytics_confidence"]["language"]["definitive_allowed"] is False for pattern in stored_patterns)
    assert all("definitive" not in pattern["explanation"].lower() for pattern in stored_patterns)
    with sqlite3.connect(evidence_database) as connection:
        pattern_count = connection.execute("SELECT count(*) FROM entities WHERE type='Pattern'").fetchone()[0]
        assert connection.execute("SELECT count(*) FROM observation_windows").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM evidence_sets").fetchone() == (pattern_count,)
        assert connection.execute("SELECT count(*) FROM evidence_set_windows").fetchone() == (pattern_count,)
        summaries = [
            json.loads(row[0])
            for row in connection.execute("SELECT summary_json FROM evidence_sets")
        ]
        assert all(summary["analytics_confidence"]["sample_count"] > 0 for summary in summaries)


def test_verified_backup_preserves_evidence_projection_counts(cases, evidence_database, tmp_path):
    readings = _readings(cases)
    pattern = db.create_entity("Pattern", {"owner_email": cases["owner_email"]})
    repository = SqliteEvidenceSetRepository(database=evidence_database)
    window = _capture(repository, cases, readings)
    repository.create_set(
        owner_email=cases["owner_email"],
        claim_type="Pattern",
        claim_id=pattern["id"],
        window_ids=[window["id"]],
        summary={},
        input_data_version="synthetic",
    )
    (evidence_database.parent / "records").mkdir()
    backup, verification = create_verified_backup(
        evidence_database.parent, tmp_path / "backups", reason="synthetic-evidence-window"
    )
    assert verification["observation_window_count"] == 1
    assert verification["evidence_set_count"] == 1
    assert verification["evidence_set_window_count"] == 1
    assert verify_backup(backup) == verification
