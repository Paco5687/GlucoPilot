from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from server import db, insights, patterns
from server.backup import create_verified_backup, verify_backup
from server.evidence_bundle import (
    EvidenceBundleQuery,
    EvidenceDomain,
    build_bundle,
    claim_detail,
    clear_bundle_cache,
    window_detail,
)
from server.migrations import run_migrations


pytestmark = pytest.mark.risk_critical
OWNER = "owner@glucopilot.local"
GOLDEN = json.loads(
    (Path(__file__).parent / "fixtures" / "golden" / "evidence_backed_claims.json").read_text()
)


def test_claim_fixture_is_public_safe_and_matches_contract():
    encoded = json.dumps(GOLDEN).lower()
    assert GOLDEN["synthetic"] is True
    assert GOLDEN["owner_email"] == OWNER
    assert GOLDEN["contract_version"] == "evidence-backed-claim/1.0.0"
    assert GOLDEN["evidence_roles"] == ["supporting", "opposing", "limiting"]
    assert "token" not in encoded
    assert "password" not in encoded


def _quality(*, expected_days=14):
    return {
        "version": "data-quality/1.0.0",
        "ai_eligible": True,
        "coverage_status": "complete",
        "freshness_status": "current",
        "limitations": [],
        "expected_days": expected_days,
        "input_data_version": "sha256:" + "a" * 64,
    }


@pytest.fixture
def claim_database(tmp_path, monkeypatch):
    database = tmp_path / "data" / "app.sqlite3"
    database.parent.mkdir()
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setenv("EVIDENCE_SET_WRITES_ENABLED", "true")
    return database


def _session() -> str:
    payload = base64.b64encode(json.dumps({"logged_in": True, "role": "admin"}).encode())
    return TimestampSigner("test-secret-key").sign(payload).decode()


def _seed_pattern_readings():
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    cursor = now - timedelta(days=14)
    records = []
    while cursor <= now:
        records.append(
            {
                "timestamp": cursor.isoformat().replace("+00:00", "Z"),
                "value": 220 if cursor.hour == 14 else 110,
                "source": "synthetic-cgm",
                "owner_email": OWNER,
            }
        )
        cursor += timedelta(minutes=5)
    db.bulk_create_entities("GlucoseReading", records)


def test_pattern_generations_preserve_lineage_and_open_exact_sources(
    claim_database, monkeypatch, tmp_path
):
    _seed_pattern_readings()

    async def no_enrichment(*args, **kwargs):
        return {"patterns": []}

    monkeypatch.setattr(patterns, "invoke_llm", no_enrichment)
    first = asyncio.run(patterns.analyze())
    first_rows = db.query_entities("Pattern", {"owner_email": OWNER})
    assert first["patternsFound"] == len(first_rows) >= 1
    assert all(row["is_active"] is True for row in first_rows)
    assert all(row["claim_contract_version"] == "evidence-backed-claim/1.0.0" for row in first_rows)
    assert all(row["evidence_set_id"].startswith("urn:glucopilot:evidence-set:") for row in first_rows)

    second = asyncio.run(patterns.analyze())
    all_rows = db.query_entities("Pattern", {"owner_email": OWNER})
    current = [row for row in all_rows if row.get("is_active")]
    prior = [row for row in all_rows if not row.get("is_active")]
    assert second["patternsFound"] == len(current) == len(first_rows)
    assert len(all_rows) == len(first_rows) * 2
    assert all(row.get("assertion_status") == "superseded" for row in prior)
    assert all(row.get("superseded_by_claim_id") for row in prior)

    detail = claim_detail("Pattern", current[0]["id"])
    assert detail["claim_contract_version"] == "evidence-backed-claim/1.0.0"
    assert detail["claim"]["version_number"] == 2
    assert len(detail["lineage"]) == 2
    assert {version["assertion_status"] for version in detail["lineage"]} == {
        "provisional",
        "superseded",
    }
    assert detail["evidence"]["supporting"]
    assert detail["evidence"]["opposing"] == []
    assert detail["evidence"]["limiting"]
    clear_bundle_cache()
    bundle = build_bundle(EvidenceBundleQuery(
        start=datetime.now(timezone.utc) - timedelta(days=30),
        end=datetime.now(timezone.utc) + timedelta(days=1),
        domains=(EvidenceDomain.ANALYTICS,),
        question_intent="current glucose pattern claim evidence",
        item_budget=50,
    ))
    bundle_claim = next(
        item for item in bundle["evidence"]["derived_metrics"]
        if item["entity_id"] == current[0]["id"]
    )["claim"]
    assert bundle_claim["href"] == f"/api/evidence/claims/Pattern/{current[0]['id']}"
    assert bundle_claim["assertion_status"] == "provisional"
    source_window = detail["evidence"]["supporting"][0]
    drilled = window_detail(source_window["window_id"], limit=3)
    assert drilled["returned"] == 3
    assert all(item["href"].startswith("/api/evidence/sources/") for item in drilled["observations"])

    with sqlite3.connect(claim_database) as connection:
        assert connection.execute("SELECT count(*) FROM claim_versions").fetchone() == (
            len(all_rows),
        )
        assert connection.execute(
            "SELECT count(*) FROM claim_versions WHERE evidence_set_id IS NULL"
        ).fetchone() == (0,)

    (claim_database.parent / "records").mkdir(exist_ok=True)
    backup, verification = create_verified_backup(
        claim_database.parent,
        tmp_path / "backups",
        reason="synthetic-evidence-backed-claims",
    )
    assert verification["claim_algorithm_registry_count"] == 2
    assert verification["claim_version_count"] == len(all_rows)
    assert verify_backup(backup) == verification


def test_insight_refresh_supersedes_instead_of_delete_and_links_domain_evidence(
    claim_database, monkeypatch
):
    today = datetime.now(timezone.utc).date()
    glucose_metrics = {}
    glucose_rows = []
    for index in range(14):
        day = today - timedelta(days=13 - index)
        timestamp = f"{day.isoformat()}T12:00:00Z"
        glucose_rows.append(db.create_entity(
            "GlucoseReading",
            {
                "owner_email": OWNER,
                "timestamp": timestamp,
                "value": 110 + index,
                "source": "synthetic-cgm",
            },
        ))
        glucose_metrics[day.isoformat()] = {
            "tir": 70 + index * 0.5,
            "avg": 120,
            "cv": 20,
            "lows": 0,
            "highs": 0,
        }
        db.create_entity(
            "OuraDaily",
            {
                "owner_email": OWNER,
                "date": day.isoformat(),
                "sleep_score": 60 + index,
                "source": "synthetic-oura",
            },
        )

    monkeypatch.setattr(
        insights,
        "_daily_glucose_metrics",
        lambda *args: (glucose_metrics, _quality(expected_days=90), glucose_rows),
    )
    monkeypatch.setattr(insights, "assess_daily", lambda *args, **kwargs: _quality(expected_days=90))
    monkeypatch.setattr(insights, "assess_pump_tdd", lambda *args, **kwargs: _quality(expected_days=90))

    async def no_enrichment(*args, **kwargs):
        return {"insights": []}

    monkeypatch.setattr(insights, "invoke_llm", no_enrichment)
    first = asyncio.run(insights.analyze())
    first_rows = db.query_entities("Insight", {"owner_email": OWNER})
    assert first["insightsFound"] == len(first_rows) == 1
    assert first_rows[0]["is_active"] is True

    second = asyncio.run(insights.analyze())
    rows = db.query_entities("Insight", {"owner_email": OWNER})
    current = [row for row in rows if row.get("is_active")]
    prior = [row for row in rows if not row.get("is_active")]
    assert second["insightsFound"] == len(current) == 1
    assert len(rows) == 2
    assert prior[0]["assertion_status"] == "superseded"
    assert prior[0]["superseded_by_claim_id"] == current[0]["id"]

    detail = claim_detail("Insight", current[0]["id"])
    assert detail["claim"]["algorithm"] == {
        "id": "cross-domain-insight-analysis",
        "version": "2.0.0",
    }
    assert detail["claim"]["analytics_confidence"]["version"] == "analytics-confidence/1.0.0"
    assert {window["entity_type"] for window in detail["evidence"]["supporting"]} == {
        "GlucoseReading",
        "OuraDaily",
    }
    assert len(detail["lineage"]) == 2


def test_claim_and_window_routes_are_authenticated(claim_database, monkeypatch):
    _seed_pattern_readings()

    async def no_enrichment(*args, **kwargs):
        return {"patterns": []}

    monkeypatch.setattr(patterns, "invoke_llm", no_enrichment)
    asyncio.run(patterns.analyze())
    pattern = db.query_entities("Pattern", {"is_active": True, "owner_email": OWNER})[0]

    from server.main import app

    with TestClient(app) as client:
        path = f"/api/evidence/claims/Pattern/{pattern['id']}"
        assert client.get(path).status_code == 401
        client.cookies.set("session", _session())
        response = client.get(path)
        assert response.status_code == 200
        window_path = response.json()["evidence"]["supporting"][0]["href"]
        window_response = client.get(window_path)
        assert window_response.status_code == 200
        assert window_response.json()["observations"]
        assert client.get("/api/evidence/claims/Pattern/missing").status_code == 404
