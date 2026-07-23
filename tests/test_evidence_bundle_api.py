from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from server import db, evidence_bundle
from server.contradictions import SqliteContradictionRepository
from server.data_contracts import EvidenceLevel
from server.evidence_bundle import clear_bundle_cache
from server.migrations import run_migrations
from server.relationships import RelationshipWrite, SqliteRelationshipRepository


pytestmark = pytest.mark.risk_critical
OWNER = "owner@glucopilot.local"


def _session(role: str) -> str:
    payload = base64.b64encode(
        json.dumps({"logged_in": True, "role": role, "provider_name": "Synthetic Provider"}).encode()
    )
    return TimestampSigner("test-secret-key").sign(payload).decode()


def _query(*, budget: int = 20) -> dict:
    return {
        "start": "2026-05-01T00:00:00Z",
        "end": "2026-05-31T23:59:59Z",
        "domains": ["glucose", "labs", "analytics"],
        "question_intent": "synthetic glucose lab trend",
        "item_budget": budget,
    }


@pytest.fixture
def evidence_api(tmp_path, monkeypatch):
    database = tmp_path / "data" / "app.sqlite3"
    database.parent.mkdir()
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setenv("RELATIONSHIP_READS_ENABLED", "true")
    monkeypatch.setenv("EVIDENCE_SET_READS_ENABLED", "false")
    clear_bundle_cache()

    glucose = db.create_entity(
        "GlucoseReading",
        {
            "owner_email": OWNER,
            "timestamp": "2026-05-10T12:00:00Z",
            "value": 112,
            "source": "synthetic-cgm",
        },
    )
    out_of_range = db.create_entity(
        "GlucoseReading",
        {
            "owner_email": OWNER,
            "timestamp": "2026-04-10T12:00:00Z",
            "value": 999,
            "source": "synthetic-cgm",
        },
    )
    foreign = db.create_entity(
        "GlucoseReading",
        {
            "owner_email": "other@glucopilot.local",
            "timestamp": "2026-05-10T12:00:00Z",
            "value": 777,
            "api_token": "foreign-secret-token",
        },
    )
    record = db.create_entity(
        "MedicalRecord",
        {
            "owner_email": OWNER,
            "record_date": "2026-05-12",
            "title": "Synthetic lab report",
            "filename": "synthetic.pdf",
            "stored_as": "private-internal-name.pdf",
        },
    )
    normal_lab = db.create_entity(
        "LabResult",
        {
            "owner_email": OWNER,
            "record_id": record["id"],
            "collected_date": "2026-05-12",
            "test_name": "Synthetic glucose",
            "value": 90,
            "unit": "mg/dL",
            "flag": "normal",
            "source_page": 2,
            "parser_confidence": 0.91,
        },
    )
    pattern = db.create_entity(
        "Pattern",
        {
            "owner_email": OWNER,
            "last_detected": "2026-05-20T00:00:00Z",
            "title": "Synthetic glucose trend",
            "supporting_evidence": [{"observation": glucose["id"]}],
            "api_token": "must-not-leak",
        },
    )

    SqliteRelationshipRepository(database=database).add(
        RelationshipWrite(
            owner_email=OWNER,
            subject_type="LabResult",
            subject_id=normal_lab["id"],
            predicate="extracted_from",
            object_type="MedicalRecord",
            object_id=record["id"],
            source_id="urn:synthetic:evidence-bundle",
            evidence_level=EvidenceLevel.SOURCE_RECORD.value,
            evidence_ids=("urn:synthetic:document-page-2",),
            input_data_version="synthetic-evidence-bundle-v1",
            input_hash="sha256:" + "a" * 64,
            projection_key="synthetic-evidence-bundle-edge",
        )
    )
    SqliteContradictionRepository().reconcile(
        [
            {
                "detection_key": "sha256:" + "b" * 64,
                "rule_id": "labs.synthetic_conflict",
                "rule_version": "clinical-contradictions/1.0.0",
                "domain": "labs",
                "subject_type": "LabResult",
                "subject_key": normal_lab["id"],
                "severity": "blocking",
                "explanation": "Synthetic sources disagree and both sides must remain visible.",
                "left": {"label": "Source A", "value": 90, "entity_id": normal_lab["id"]},
                "right": {"label": "Source B", "value": 120, "entity_id": normal_lab["id"]},
                "context": {"synthetic": True},
            }
        ],
        "sha256:" + "c" * 64,
    )

    from server.main import app

    with TestClient(app) as client:
        client.cookies.set("session", _session("admin"))
        yield {
            "client": client,
            "glucose": glucose,
            "out_of_range": out_of_range,
            "foreign": foreign,
            "record": record,
            "lab": normal_lab,
            "pattern": pattern,
        }
    clear_bundle_cache()


def test_bundle_is_deterministic_complete_bounded_and_source_linked(evidence_api, monkeypatch):
    client = evidence_api["client"]
    original_loader = evidence_bundle._load_entity_candidates
    load_count = 0

    def counted_loader(*args, **kwargs):
        nonlocal load_count
        load_count += 1
        return original_loader(*args, **kwargs)

    monkeypatch.setattr(evidence_bundle, "_load_entity_candidates", counted_loader)
    first = client.post("/api/evidence/bundles/query", json=_query())
    second = client.post("/api/evidence/bundles/query", json=_query())
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert load_count == 1

    body = first.json()
    assert body["bundle_id"].startswith("urn:glucopilot:evidence-bundle:")
    assert body["bundle_version"] == "2.3.0"
    assert body["data_version"]["input_hash"].startswith("sha256:")
    assert body["budget"]["returned_items"] <= body["budget"]["item_limit"] == 20
    assert body["evidence"]["direct_observations"]
    assert body["evidence"]["derived_metrics"]
    assert body["evidence"]["relationships"]
    assert body["evidence"]["documents"]
    assert body["evidence"]["reassuring_evidence"]
    assert body["evidence"]["opposing_evidence"]
    assert body["contradictions"][0]["severity"] == "blocking"
    assert body["missing_data_caveats"]
    assert {item["domain"] for item in body["missing_data_caveats"]} >= {
        "all", "glucose", "labs", "analytics"
    }
    assert body["source_links"]
    lab_item = next(
        item for item in body["evidence"]["direct_observations"]
        if item["entity_id"] == evidence_api["lab"]["id"]
    )
    assert lab_item["confidence"]["label"] == "unverified"
    assert lab_item["confidence"]["clinically_verified"] is False
    for link in body["source_links"]:
        if link["kind"] == "normalized_entity":
            assert client.get(link["href"]).status_code == 200

    encoded = json.dumps(body, sort_keys=True)
    assert "must-not-leak" not in encoded
    assert "foreign-secret-token" not in encoded
    assert "private-internal-name.pdf" not in encoded
    assert evidence_api["out_of_range"]["id"] not in encoded
    assert evidence_api["foreign"]["id"] not in encoded


def test_blocking_contradictions_are_protected_from_ranking_budget(evidence_api):
    response = evidence_api["client"].post(
        "/api/evidence/bundles/query",
        json=_query(budget=1),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["budget"]["returned_items"] == 1
    assert body["budget"]["protected_blocking_contradictions"] == 1
    assert body["budget"]["blocking_contradictions_count_against_item_limit"] is False
    assert len(body["contradictions"]) == 1
    assert body["contradictions"][0]["severity"] == "blocking"


def test_data_change_invalidates_cache_and_changes_bundle_identity(evidence_api):
    client = evidence_api["client"]
    before = client.post("/api/evidence/bundles/query", json=_query()).json()
    db.update_entity("GlucoseReading", evidence_api["glucose"]["id"], {"value": 118})
    after = client.post("/api/evidence/bundles/query", json=_query()).json()
    assert after["data_version"]["input_hash"] != before["data_version"]["input_hash"]
    assert after["bundle_id"] != before["bundle_id"]
    values = [item["data"].get("value") for item in after["evidence"]["direct_observations"]]
    assert 118 in values


def test_bundle_auth_owner_scope_provider_access_and_read_only_contract(evidence_api):
    client = evidence_api["client"]
    client.cookies.clear()
    assert client.post("/api/evidence/bundles/query", json=_query()).status_code == 401

    client.cookies.set("session", _session("provider"))
    assert client.post("/api/evidence/bundles/query", json=_query()).status_code == 200
    assert client.get(
        f"/api/evidence/sources/GlucoseReading/{evidence_api['foreign']['id']}"
    ).status_code == 404
    assert client.post(
        "/api/evidence/bundles/query",
        json={**_query(), "owner_email": "other@glucopilot.local"},
    ).status_code == 422
    assert client.get("/api/evidence/bundles/query").status_code == 405


def test_bundle_validation_and_openapi_bounds(evidence_api):
    client = evidence_api["client"]
    assert client.post(
        "/api/evidence/bundles/query",
        json={**_query(), "item_budget": 251},
    ).status_code == 422
    assert client.post(
        "/api/evidence/bundles/query",
        json={**_query(), "domains": []},
    ).status_code == 422
    assert client.post(
        "/api/evidence/bundles/query",
        json={**_query(), "start": "2026-06-01T00:00:00Z"},
    ).status_code == 422

    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/api/evidence/bundles/query"]["post"]
    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    model = schema["components"]["schemas"][request_schema["$ref"].split("/")[-1]]
    assert model["properties"]["item_budget"]["maximum"] == 250
    assert set(schema["paths"]["/api/evidence/sources/{entity_type}/{entity_id}"]) == {"get"}
