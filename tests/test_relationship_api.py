from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from server import db
from server.data_contracts import ConfidenceLabel, EvidenceLevel
from server.migrations import run_migrations
from server.relationships import RelationshipWrite, SqliteRelationshipRepository


pytestmark = pytest.mark.risk_critical
OWNER = "owner@glucopilot.local"


def _session(role: str) -> str:
    payload = base64.b64encode(
        json.dumps({"logged_in": True, "role": role, "provider_name": "Synthetic Provider"}).encode()
    )
    return TimestampSigner("test-secret-key").sign(payload).decode()


@pytest.fixture
def graph_api(tmp_path, monkeypatch):
    database = tmp_path / "data" / "app.sqlite3"
    database.parent.mkdir()
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setenv("RELATIONSHIP_READS_ENABLED", "true")

    record = db.create_entity("MedicalRecord", {"owner_email": OWNER})
    labs = [db.create_entity("LabResult", {"owner_email": OWNER}) for _ in range(3)]
    foreign = db.create_entity("LabResult", {"owner_email": "other@glucopilot.local"})
    repository = SqliteRelationshipRepository(database=database)
    raw_secrets = []
    for index, lab in enumerate(labs):
        source = f"https://synthetic.invalid/source?access_token=secret-source-{index}"
        evidence = f"urn:synthetic:secret-evidence-{index}"
        input_version = f"secret-input-version-{index}"
        raw_secrets.extend((source, evidence, input_version))
        repository.add(
            RelationshipWrite(
                owner_email=OWNER,
                subject_type="LabResult",
                subject_id=lab["id"],
                predicate="extracted_from",
                object_type="MedicalRecord",
                object_id=record["id"],
                source_id=source,
                evidence_level=EvidenceLevel.SOURCE_RECORD.value,
                evidence_ids=(evidence,),
                input_data_version=input_version,
                input_hash="sha256:" + str(index + 1) * 64,
                projection_key=f"synthetic-api-edge-{index}",
                confidence_label=ConfidenceLabel.HIGH.value,
                confidence_score=0.95,
                confidence_method=f"secret-confidence-method-{index}",
            )
        )

    from server.main import app

    with TestClient(app) as client:
        client.cookies.set("session", _session("admin"))
        yield {
            "client": client,
            "record": record,
            "labs": labs,
            "foreign": foreign,
            "raw_secrets": raw_secrets,
        }


def test_graph_gate_and_authentication_precede_data_access(graph_api, monkeypatch):
    client = graph_api["client"]
    lab = graph_api["labs"][0]
    path = f"/api/relationships/LabResult/{lab['id']}/neighbors"

    monkeypatch.setenv("RELATIONSHIP_READS_ENABLED", "false")
    assert client.get(path).status_code == 503
    client.cookies.clear()
    assert client.get(path).status_code == 401


def test_neighbors_reverse_neighbors_metadata_bounds_and_redaction(graph_api):
    client = graph_api["client"]
    lab = graph_api["labs"][0]
    record = graph_api["record"]

    outgoing = client.get(f"/api/relationships/LabResult/{lab['id']}/neighbors")
    assert outgoing.status_code == 200
    body = outgoing.json()
    assert body["direction"] == "outgoing"
    assert body["budget"] == {"item_limit": 50, "returned": 1, "truncated": False}
    edge = body["relationships"][0]
    assert edge["assertion"] == {
        "kind": "source_fact",
        "status": "confirmed",
        "evidence_level": "source_record",
        "evidence_count": 1,
        "evidence_refs": [edge["assertion"]["evidence_refs"][0]],
    }
    assert edge["confidence"]["label"] == "high"
    assert edge["confidence"]["score"] == 0.95
    assert edge["source"]["class"] == "system"
    assert edge["source"]["ref"].startswith("sha256:")
    assert edge["version"]["generator_version"] == "1.0.0"
    assert edge["version"]["input_data_version_ref"].startswith("sha256:")
    encoded = json.dumps(body, sort_keys=True)
    assert all(secret not in encoded for secret in graph_api["raw_secrets"])
    assert "input_hash" not in encoded
    assert "projection_key" not in encoded

    incoming = client.get(
        f"/api/relationships/MedicalRecord/{record['id']}/reverse-neighbors?limit=2"
    )
    assert incoming.status_code == 200
    assert incoming.json()["budget"] == {"item_limit": 2, "returned": 2, "truncated": True}
    assert client.get(
        f"/api/relationships/MedicalRecord/{record['id']}/reverse-neighbors?limit=251"
    ).status_code == 422


def test_traversal_is_deterministic_breadth_first_and_strictly_bounded(graph_api):
    client = graph_api["client"]
    lab = graph_api["labs"][0]
    path = f"/api/relationships/LabResult/{lab['id']}/traverse?depth=2&limit=2"
    first = client.get(path)
    second = client.get(path)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    body = first.json()
    assert body["budget"]["depth_limit"] == 2
    assert body["budget"]["item_limit"] == 2
    assert body["budget"]["returned_relationships"] <= 2
    assert body["budget"]["returned_nodes"] <= 3
    assert body["budget"]["expanded"] <= body["budget"]["expansion_limit"] == 1000
    assert body["ordering"][0] == "breadth_first"
    assert client.get(
        f"/api/relationships/LabResult/{lab['id']}/traverse?depth=5"
    ).status_code == 422


def test_evidence_paths_are_bounded_redacted_and_owner_scoped(graph_api):
    client = graph_api["client"]
    start, target = graph_api["labs"][:2]
    path = (
        f"/api/relationships/LabResult/{start['id']}/evidence-paths"
        f"?target_type=LabResult&target_id={target['id']}&depth=2&max_paths=2"
    )
    response = client.get(path)
    assert response.status_code == 200
    body = response.json()
    assert body["paths"]
    assert all(len(item["relationships"]) <= 2 for item in body["paths"])
    assert body["budget"]["returned_paths"] <= 2
    assert body["budget"]["expanded"] <= body["budget"]["expansion_limit"]
    assert body["ordering"][0] == "shortest_path_first"
    encoded = json.dumps(body, sort_keys=True)
    assert all(secret not in encoded for secret in graph_api["raw_secrets"])

    missing = client.get(
        f"/api/relationships/LabResult/{start['id']}/evidence-paths"
        "?target_type=LabResult&target_id=missing"
    )
    foreign = client.get(
        f"/api/relationships/LabResult/{start['id']}/evidence-paths"
        f"?target_type=LabResult&target_id={graph_api['foreign']['id']}"
    )
    assert missing.status_code == foreign.status_code == 404
    assert missing.json() == foreign.json() == {"detail": "Not found"}


def test_provider_session_can_read_but_graph_has_no_mutation_route(graph_api):
    client = graph_api["client"]
    client.cookies.set("session", _session("provider"))
    lab = graph_api["labs"][0]
    path = f"/api/relationships/LabResult/{lab['id']}/neighbors"
    assert client.get(path).status_code == 200
    assert client.post(path, json={"predicate": "extracted_from"}).status_code == 405


def test_openapi_contract_exposes_only_bounded_get_operations(graph_api):
    schema = graph_api["client"].get("/openapi.json").json()
    paths = {
        path: value
        for path, value in schema["paths"].items()
        if path.startswith("/api/relationships/")
    }
    assert set(paths) == {
        "/api/relationships/{entity_type}/{entity_id}/neighbors",
        "/api/relationships/{entity_type}/{entity_id}/reverse-neighbors",
        "/api/relationships/{entity_type}/{entity_id}/traverse",
        "/api/relationships/{entity_type}/{entity_id}/evidence-paths",
    }
    assert all(set(operations) == {"get"} for operations in paths.values())
    traverse_parameters = {
        item["name"]: item["schema"]
        for item in paths[
            "/api/relationships/{entity_type}/{entity_id}/traverse"
        ]["get"]["parameters"]
    }
    assert traverse_parameters["depth"]["maximum"] == 4
    assert traverse_parameters["limit"]["maximum"] == 250
