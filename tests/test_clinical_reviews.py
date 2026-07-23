"""P7 provider review authorization, attribution, and append-only history."""

import base64
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from server import clinical_reviews, db
from server.hypotheses import CreateHypothesisBody, SqliteHypothesisRepository
from server.migrations import run_migrations


pytestmark = pytest.mark.risk_critical


def _session(role, name="Synthetic Reviewer"):
    payload = base64.b64encode(json.dumps({
        "logged_in": True,
        "role": role,
        "provider_name": name if role == "provider" else "",
    }).encode())
    return TimestampSigner("test-secret-key").sign(payload).decode()


@pytest.fixture
def review_api(tmp_path, monkeypatch):
    database = tmp_path / "data" / "app.sqlite3"
    database.parent.mkdir()
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    hypothesis = SqliteHypothesisRepository().create(
        CreateHypothesisBody(title="Synthetic tentative finding"),
        {"kind": "patient", "role": "admin", "label": "Synthetic Owner"},
    )

    from server.main import app

    with TestClient(app) as client:
        yield client, database, hypothesis["id"]


def _provider_review(client, hypothesis_id):
    client.cookies.set("session", _session("provider"))
    response = client.post(
        "/api/clinical-reviews/actions",
        json={
            "target_kind": "hypothesis",
            "target_type": "HealthHypothesis",
            "target_id": hypothesis_id,
            "target_label": "Synthetic tentative finding",
            "action": "hypothesis_confirm",
            "text": "Reviewed against the linked synthetic evidence.",
            "evidence_bundle_id": "urn:glucopilot:evidence-bundle:" + "a" * 64,
        },
    )
    client.cookies.clear()
    assert response.status_code == 200
    return response.json()


def test_provider_action_records_identity_target_time_and_prior_new_state(review_api):
    client, _database, hypothesis_id = review_api
    first = _provider_review(client, hypothesis_id)
    event = first["events"][0]

    assert event["actor_role"] == "provider"
    assert event["actor_id"].startswith("provider:")
    assert event["actor_label"] == "Synthetic Reviewer"
    assert event["created_at"].endswith("Z")
    assert event["prior_state"] == {}
    assert event["new_state"]["provider_status"] == "hypothesis_confirmed"
    assert first["target_id"] == hypothesis_id

    client.cookies.set("session", _session("provider"))
    revised = client.post(
        "/api/clinical-reviews/actions",
        json={
            "target_kind": "hypothesis",
            "target_type": "HealthHypothesis",
            "target_id": hypothesis_id,
            "action": "question",
            "text": "Could the opposing evidence change this review?",
        },
    ).json()
    client.cookies.clear()
    assert len(revised["events"]) == 2
    assert revised["events"][1]["prior_state"]["provider_status"] == "hypothesis_confirmed"
    assert revised["events"][1]["new_state"]["provider_status"] == "question_open"


def test_owner_dispute_preserves_provider_history_and_can_be_followed_by_accept(review_api):
    client, database, hypothesis_id = review_api
    review = _provider_review(client, hypothesis_id)
    client.cookies.set("session", _session("admin"))
    disputed = client.post(
        f"/api/clinical-reviews/{review['id']}/owner-decision",
        json={"decision": "dispute", "reason": "The source identity needs clarification."},
    )
    accepted = client.post(
        f"/api/clinical-reviews/{review['id']}/owner-decision",
        json={"decision": "accept", "reason": "Clarification was received and reviewed."},
    )
    client.cookies.clear()

    assert disputed.status_code == accepted.status_code == 200
    result = accepted.json()
    assert result["owner_status"] == "accepted"
    assert [event["action"] for event in result["events"]] == [
        "hypothesis_confirm", "owner_dispute", "owner_accept"
    ]
    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE clinical_review_events SET reason='changed' WHERE thread_id=?",
                (review["id"],),
            )


def test_roles_cannot_cross_source_and_review_mutation_boundaries(review_api):
    client, _database, _hypothesis_id = review_api
    client.cookies.set("session", _session("admin"))
    assert client.post(
        "/api/clinical-reviews/actions",
        json={
            "target_kind": "brief",
            "target_type": "ClinicianBrief",
            "target_id": "synthetic",
            "action": "mark_reviewed",
        },
    ).status_code == 403
    client.cookies.set("session", _session("provider"))
    assert client.post(
        "/api/entities/SymptomLog",
        json={"symptom": "synthetic source mutation attempt"},
    ).status_code == 403
    assert client.post(
        "/api/clinical-reviews/missing/owner-decision",
        json={"decision": "accept", "reason": "Provider cannot accept."},
    ).status_code == 403
    client.cookies.clear()


def test_companion_context_separates_confirmation_annotation_and_dispute(review_api):
    client, _database, hypothesis_id = review_api
    review = _provider_review(client, hypothesis_id)
    context = clinical_reviews.companion_context()
    assert context["clinician_confirmed_facts"][0]["target_id"] == hypothesis_id
    assert context["clinician_confirmed_facts"][0]["definitive_allowed"] is True

    client.cookies.set("session", _session("admin"))
    client.post(
        f"/api/clinical-reviews/{review['id']}/owner-decision",
        json={"decision": "dispute", "reason": "Synthetic owner dispute."},
    )
    client.cookies.clear()
    disputed = clinical_reviews.companion_context()
    assert disputed["clinician_confirmed_facts"] == []
    assert disputed["owner_disputed_reviews"][0]["target_id"] == hypothesis_id
