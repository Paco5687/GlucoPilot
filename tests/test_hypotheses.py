import base64
import json
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from starlette.middleware.sessions import SessionMiddleware

from server import conditions, db, hypotheses
from server.backup import create_verified_backup, verify_backup
from server.config import OWNER_EMAIL
from server.evidence_bundle import EvidenceBundleQuery, EvidenceDomain, build_bundle
from server.migrations import run_migrations


pytestmark = pytest.mark.risk_critical


@pytest.fixture
def hypothesis_database(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database = data_dir / "app.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", database)
    run_migrations(database)
    return database


def _actor(kind="patient", role="admin", label="Synthetic Owner"):
    return {"kind": kind, "role": role, "label": label}


def _evidence(version="v1", opposing_weight=0.5):
    return [
        hypotheses.EvidenceBody(
            role="supporting",
            source_kind="entity",
            source_type="LabResult",
            source_id="synthetic-lab",
            source_version=version,
            summary="Synthetic verified marker supports review.",
            weight=1,
            source_link={
                "kind": "normalized_entity",
                "href": "/api/evidence/sources/LabResult/synthetic-lab",
            },
        ),
        hypotheses.EvidenceBody(
            role="opposing",
            source_kind="clinical_reference",
            source_type="publication",
            source_id="synthetic-reference",
            source_version="2026",
            summary="Synthetic reference describes an alternative explanation.",
            weight=opposing_weight,
            source_link={"href": "https://doi.org/10.0000/synthetic"},
        ),
        hypotheses.EvidenceBody(
            role="missing",
            source_kind="missing",
            summary="A synthetic confirmatory test has not been recorded.",
            weight=0.5,
        ),
    ]


def _create(repository):
    return repository.create(
        hypotheses.CreateHypothesisBody(
            title="Synthetic thyroid hypothesis",
            description="For contract testing only.",
            origin_kind="algorithm",
            origin_label="synthetic-rule/1.0",
            suggested_verification="Discuss a synthetic confirmatory test.",
            review_at="2026-08-01",
            evidence=_evidence(),
        ),
        _actor(kind="algorithm", role="algorithm", label="synthetic-rule/1.0"),
    )


def test_hypothesis_origin_evidence_confidence_and_events_are_attributable(
    hypothesis_database,
):
    repository = hypotheses.SqliteHypothesisRepository()
    created = _create(repository)

    assert created["status"] == "proposed"
    assert created["origin_kind"] == "algorithm"
    assert created["confidence_score"] == 0.5
    assert created["confidence_method"] == "weighted-evidence-v1"
    assert len(created["evidence_by_role"]["supporting"]) == 1
    assert len(created["evidence_by_role"]["opposing"]) == 1
    assert len(created["evidence_by_role"]["missing"]) == 1
    assert created["events"][0]["action"] == "created"
    assert created["events"][0]["actor_kind"] == "algorithm"
    assert "diagnosis" in created["events"][0]["reason"]
    assert "owner_id" not in created
    assert "owner_email" not in created

    with sqlite3.connect(hypothesis_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM health_hypotheses").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM hypothesis_evidence").fetchone() == (3,)
        assert connection.execute("SELECT COUNT(*) FROM hypothesis_events").fetchone() == (1,)


def test_evidence_version_change_recalculates_confidence_and_records_why(
    hypothesis_database,
):
    repository = hypotheses.SqliteHypothesisRepository()
    created = _create(repository)
    previous_version = created["evidence_input_version"]

    revised = repository.revise_evidence(
        created["id"],
        hypotheses.ReviseEvidenceBody(
            reason="Synthetic source version changed after review.",
            evidence=_evidence(version="v2", opposing_weight=1),
        ),
        _actor(),
    )

    assert revised["evidence_revision"] == 2
    assert revised["evidence_input_version"] != previous_version
    assert revised["confidence_score"] == 0.4
    assert revised["events"][-1]["action"] == "evidence_revised"
    assert revised["events"][-1]["reason"] == "Synthetic source version changed after review."
    assert revised["events"][-1]["before"]["confidence_score"] == 0.5
    assert revised["events"][-1]["after"]["confidence_score"] == 0.4

    with sqlite3.connect(hypothesis_database) as connection:
        # Both evidence revisions remain immutable and independently replayable.
        assert connection.execute("SELECT COUNT(*) FROM hypothesis_evidence").fetchone() == (6,)
        with pytest.raises(sqlite3.IntegrityError, match="hypothesis evidence is immutable"):
            connection.execute("DELETE FROM hypothesis_evidence")
        with pytest.raises(sqlite3.IntegrityError, match="hypothesis events are immutable"):
            connection.execute("UPDATE hypothesis_events SET reason='changed'")


def test_terminal_transitions_require_attributable_clinician_authority(
    hypothesis_database,
):
    repository = hypotheses.SqliteHypothesisRepository()
    created = _create(repository)
    reviewing = repository.transition(
        created["id"],
        hypotheses.TransitionBody(
            status="under_review",
            reason="Synthetic owner requested clinical review.",
        ),
        _actor(),
    )
    assert reviewing["status"] == "under_review"

    with pytest.raises(hypotheses.HypothesisError, match="clinician review"):
        repository.transition(
            created["id"],
            hypotheses.TransitionBody(
                status="confirmed",
                reason="An algorithm must not confirm this.",
                decision_authority="clinician",
                reviewer="Synthetic Algorithm",
            ),
            _actor(kind="algorithm", role="algorithm", label="synthetic-rule/1.0"),
        )

    with pytest.raises(hypotheses.HypothesisError, match="clinician review"):
        repository.transition(
            created["id"],
            hypotheses.TransitionBody(
                status="confirmed",
                reason="Reviewer identity is required.",
            ),
            _actor(),
        )

    confirmed = repository.transition(
        created["id"],
        hypotheses.TransitionBody(
            status="confirmed",
            reason="Synthetic clinician reviewed both sides.",
            decision_authority="clinician",
            reviewer="Dr. Synthetic Reviewer",
        ),
        _actor(),
    )
    assert confirmed["status"] == "confirmed"
    assert confirmed["decided_by"] == "Dr. Synthetic Reviewer"
    assert confirmed["events"][-1]["actor_kind"] == "clinician"
    assert confirmed["events"][-1]["action"] == "review_recorded"
    with pytest.raises(hypotheses.HypothesisError, match="terminal"):
        repository.revise_evidence(
            created["id"],
            hypotheses.ReviseEvidenceBody(reason="Too late.", evidence=[]),
            _actor(),
        )
    with pytest.raises(hypotheses.HypothesisError, match="not permitted"):
        repository.transition(
            created["id"],
            hypotheses.TransitionBody(status="archived", reason="Terminal rows stay final."),
            _actor(),
        )


def test_algorithm_entrypoint_can_only_create_a_proposal(hypothesis_database):
    created = hypotheses.create_algorithm_hypothesis(
        title="Synthetic algorithm proposal",
        algorithm_id="synthetic-algorithm/1.0",
        evidence=[item.model_dump() for item in _evidence()],
    )
    assert created["origin_kind"] == "algorithm"
    assert created["status"] == "proposed"
    assert created["decided_by"] is None


def _session(secret, role):
    payload = base64.b64encode(
        json.dumps(
            {"logged_in": True, "role": role, "provider_name": "Synthetic Provider"}
        ).encode()
    )
    return TimestampSigner(secret).sign(payload).decode()


def test_provider_can_read_but_cannot_mutate_hypotheses(hypothesis_database):
    secret = "synthetic-hypothesis-test-secret"
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=secret)
    app.include_router(hypotheses.router)
    client = TestClient(app)

    client.cookies.set("session", _session(secret, "provider"))
    response = client.get("/api/hypotheses")
    assert response.status_code == 200
    assert response.json()["can_edit"] is False
    assert client.post(
        "/api/hypotheses", json={"title": "Provider mutation must fail"}
    ).status_code == 403

    client.cookies.set("session", _session(secret, "admin"))
    created = client.post(
        "/api/hypotheses",
        json={
            "title": "Synthetic patient proposal",
            "suggested_verification": "Review with a clinician.",
            # Even a forged client field cannot skip the guarded lifecycle.
            "status": "confirmed",
        },
    )
    assert created.status_code == 200
    assert created.json()["status"] == "proposed"


def test_legacy_suspected_condition_is_not_a_confirmed_diagnosis(
    hypothesis_database,
):
    db.create_entity(
        "Diagnosis",
        {
            "name": "Synthetic legacy suspicion",
            "status": "suspected",
            "owner_email": OWNER_EMAIL,
        },
    )
    db.create_entity(
        "Diagnosis",
        {
            "name": "Synthetic confirmed condition",
            "status": "active",
            "owner_email": OWNER_EMAIL,
        },
    )

    assert [item["name"] for item in conditions.report_block()] == [
        "Synthetic confirmed condition"
    ]
    report_hypotheses = hypotheses.report_block()
    assert report_hypotheses[0]["title"] == "Synthetic legacy suspicion"
    assert report_hypotheses[0]["legacy"] is True
    assert report_hypotheses[0]["status"] == "proposed"

    bundle = build_bundle(
        EvidenceBundleQuery(
            start=datetime(2010, 1, 1, tzinfo=timezone.utc),
            end=datetime(2030, 1, 1, tzinfo=timezone.utc),
            domains=(EvidenceDomain.CLINICAL,),
            question_intent="diagnoses",
            item_budget=20,
        )
    )
    diagnosis_names = {
        item["data"].get("name")
        for item in bundle["evidence"]["direct_observations"]
        if item["entity_type"] == "Diagnosis"
    }
    assert diagnosis_names == {"Synthetic confirmed condition"}


def test_verified_backup_preserves_hypothesis_ledger(hypothesis_database):
    repository = hypotheses.SqliteHypothesisRepository()
    created = _create(repository)
    repository.revise_evidence(
        created["id"],
        hypotheses.ReviseEvidenceBody(
            reason="Synthetic backup revision.",
            evidence=_evidence(version="backup-v2"),
        ),
        _actor(),
    )

    backup, created_verification = create_verified_backup(
        hypothesis_database.parent,
        hypothesis_database.parent.parent / "backups",
        reason="synthetic-hypothesis-ledger",
    )
    assert created_verification["health_hypothesis_count"] == 1
    assert created_verification["proposed_health_hypothesis_count"] == 1
    assert created_verification["hypothesis_evidence_count"] == 6
    assert created_verification["hypothesis_event_count"] == 2
    assert verify_backup(backup) == created_verification
