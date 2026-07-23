import base64
import json
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from starlette.middleware.sessions import SessionMiddleware

from server import db, episodes
from server.backup import create_verified_backup, verify_backup
from server.config import OWNER_EMAIL
from server.evidence_bundle import (
    EvidenceBundleQuery,
    EvidenceDomain,
    build_bundle,
    clear_bundle_cache,
)
from server.migrations import run_migrations


pytestmark = pytest.mark.risk_critical
ACTOR = {"role": "admin", "label": "Synthetic Owner"}


@pytest.fixture
def episode_database(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database = data_dir / "app.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", database)
    run_migrations(database)
    clear_bundle_cache()
    yield database
    clear_bundle_cache()


def _source(entity_type, **values):
    return db.create_entity(entity_type, {"owner_email": OWNER_EMAIL, **values})


def _member(entity_type, row, observed, role=None):
    return episodes.MemberBody(
        entity_type=entity_type,
        entity_id=row["id"],
        role=role,
        observed_start=observed,
        source_version=row["updated_date"],
        summary=f"Synthetic {entity_type}",
    )


def _create_episode(repository):
    symptom = _source("SymptomLog", title="Fatigue", entry_date="2026-07-20")
    glucose = _source(
        "GlucoseReading",
        timestamp="2026-07-20T12:00:00Z",
        value=140,
        source="synthetic",
    )
    cycle = _source("PeriodLog", date="2026-07-21", phase="menstrual")
    return repository.create(
        episodes.EpisodeCreateBody(
            episode_type="symptom_flare",
            title="Synthetic multi-day flare",
            description="Canonical interval contract test.",
            start_time="2026-07-20",
            end_time="2026-07-22",
            members=[
                _member("SymptomLog", symptom, "2026-07-20"),
                _member("GlucoseReading", glucose, "2026-07-20"),
                _member("PeriodLog", cycle, "2026-07-21"),
            ],
        ),
        ACTOR,
    )


def test_episode_preserves_range_multi_source_members_and_noncausal_semantics(
    episode_database,
):
    created = _create_episode(episodes.SqliteEpisodeRepository())

    assert created["status"] == "proposed"
    assert created["time_precision"] == "date"
    assert created["start_time"] == "2026-07-20"
    assert created["end_time"] == "2026-07-22"
    assert created["association_only"] == 1
    assert {item["entity_type"] for item in created["members"]} == {
        "SymptomLog",
        "GlucoseReading",
        "PeriodLog",
    }
    assert all(item["causation_asserted"] is False for item in created["members"])
    assert created["confidence"]["language"]["causal_allowed"] is False

    with sqlite3.connect(episode_database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                """
                INSERT INTO episode_members (
                    id,episode_id,membership_revision,ordinal,entity_type,entity_id,
                    member_role,relationship_kind,observed_start,observed_end,
                    source_version,summary,causation_asserted,created_at
                ) VALUES (
                    'forged-causal-member',?,99,0,'SymptomLog','forged','symptom',
                    'within_episode','2026-07-20','2026-07-20','forged','forged',1,
                    '2026-07-20T00:00:00Z'
                )
                """,
                (created["id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="episode members are immutable"):
            connection.execute(
                "UPDATE episode_members SET summary='rewritten' WHERE episode_id=?",
                (created["id"],),
            )


def test_rule_and_model_proposals_require_attributable_versions(episode_database):
    repository = episodes.SqliteEpisodeRepository()
    with pytest.raises(episodes.EpisodeError, match="version label"):
        repository.create(
            episodes.EpisodeCreateBody(
                episode_type="detected_pattern",
                title="Unattributed rule proposal",
                origin_kind="rule",
                start_time="2026-07-20",
                end_time="2026-07-20",
            ),
            ACTOR,
        )

    created = repository.create(
        episodes.EpisodeCreateBody(
            episode_type="detected_pattern",
            title="Attributed model proposal",
            origin_kind="model",
            origin_label="synthetic-episode-model/1.0",
            confidence_score=0.72,
            start_time="2026-07-20",
            end_time="2026-07-20",
        ),
        ACTOR,
    )
    assert created["origin_label"] == "synthetic-episode-model/1.0"
    assert created["confidence"]["confidence_label"] == "medium"
    assert created["status"] == "proposed"


def test_episode_correction_appends_membership_revision_and_decision_event(
    episode_database,
):
    repository = episodes.SqliteEpisodeRepository()
    created = _create_episode(repository)
    treatment = _source(
        "Treatment",
        timestamp="2026-07-21T12:00:00Z",
        event_type="meal bolus",
    )
    revised_members = [
        episodes.MemberBody(**{
            key: value
            for key, value in member.items()
            if key != "causation_asserted"
        })
        for member in created["members"]
    ]
    revised_members.append(_member("Treatment", treatment, "2026-07-21"))

    revised = repository.correct(
        created["id"],
        episodes.EpisodeCorrectionBody(
            reason="Added a temporally overlapping treatment.",
            end_time="2026-07-23",
            members=revised_members,
        ),
        ACTOR,
    )
    assert revised["membership_revision"] == 2
    assert revised["end_time"] == "2026-07-23"
    assert len(revised["members"]) == 4
    assert revised["events"][-1]["action"] == "members_revised"

    confirmed = repository.decide(
        created["id"],
        episodes.DecisionBody(
            status="confirmed",
            reason="Owner confirmed the dates and temporal membership.",
        ),
        ACTOR,
    )
    assert confirmed["status"] == "confirmed"
    assert confirmed["decided_by"] == "Synthetic Owner"
    assert confirmed["events"][-1]["action"] == "confirmed"
    with pytest.raises(episodes.EpisodeError, match="decided"):
        repository.correct(
            created["id"],
            episodes.EpisodeCorrectionBody(reason="Too late.", title="Changed"),
            ACTOR,
        )

    with sqlite3.connect(episode_database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM episode_members WHERE episode_id=?", (created["id"],)
        ).fetchone() == (7,)
        assert connection.execute(
            "SELECT COUNT(*) FROM episode_events WHERE episode_id=?", (created["id"],)
        ).fetchone() == (3,)


def test_medication_exposure_supports_open_end_correction_and_confirmation(
    episode_database,
):
    medication = _source("Medication", name="Synthetic medicine", dose="5 mg")
    repository = episodes.SqliteExposureRepository()
    created = repository.create(
        episodes.ExposureCreateBody(
            medication_entity_id=medication["id"],
            medication_name="Synthetic medicine",
            dose="5 mg",
            formulation="tablet",
            frequency="daily",
            start_time="2026-07-01",
        ),
        ACTOR,
    )
    assert created["end_time"] is None
    assert created["status"] == "proposed"

    corrected = repository.correct(
        created["id"],
        episodes.ExposureCorrectionBody(
            reason="Owner supplied the stop date.",
            end_time="2026-07-21",
        ),
        ACTOR,
    )
    assert corrected["end_time"] == "2026-07-21"
    reopened = repository.correct(
        created["id"],
        episodes.ExposureCorrectionBody(
            reason="Owner corrected this to an ongoing exposure.",
            end_time=None,
        ),
        ACTOR,
    )
    assert reopened["end_time"] is None
    confirmed = repository.decide(
        created["id"],
        episodes.DecisionBody(status="confirmed", reason="Owner verified medication use."),
        ACTOR,
    )
    assert confirmed["status"] == "confirmed"
    assert [event["action"] for event in confirmed["events"]] == [
        "created",
        "corrected",
        "corrected",
        "confirmed",
    ]


def test_temporal_candidates_are_bounded_and_keep_date_precision(episode_database):
    symptom = _source("SymptomLog", title="Headache", entry_date="2026-07-20")
    _source("HistoryEntry", title="Old note", entry_date="2026-06-01")
    exposure = episodes.SqliteExposureRepository().create(
        episodes.ExposureCreateBody(
            medication_name="Synthetic overlapping exposure",
            start_time="2026-07-01",
        ),
        ACTOR,
    )

    candidates = episodes.temporal_candidates("2026-07-19", "2026-07-21")
    assert {(item["entity_type"], item["entity_id"]) for item in candidates} == {
        ("SymptomLog", symptom["id"]),
        ("MedicationExposure", exposure["id"]),
    }
    relationships = {
        item["entity_type"]: item["relationship_kind"]
        for item in candidates
    }
    assert relationships == {
        "SymptomLog": "within_episode",
        "MedicationExposure": "temporal_overlap",
    }
    assert all(item["causation_asserted"] == 0 for item in candidates)
    with pytest.raises(episodes.EpisodeError, match="bounded limit"):
        episodes.temporal_candidates("2026-07-19", "2026-07-21", limit=0)


def _session(secret, role):
    payload = base64.b64encode(
        json.dumps(
            {"logged_in": True, "role": role, "provider_name": "Synthetic Provider"}
        ).encode()
    )
    return TimestampSigner(secret).sign(payload).decode()


def test_provider_can_read_but_cannot_mutate_episode_ledgers(episode_database):
    _create_episode(episodes.SqliteEpisodeRepository())
    secret = "synthetic-episode-test-secret"
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=secret)
    app.include_router(episodes.router)
    client = TestClient(app)

    client.cookies.set("session", _session(secret, "provider"))
    response = client.get("/api/episodes")
    assert response.status_code == 200
    assert response.json()["can_edit"] is False
    assert client.post(
        "/api/episodes",
        json={
            "episode_type": "forged",
            "title": "Provider mutation",
            "start_time": "2026-07-20",
            "end_time": "2026-07-20",
        },
    ).status_code == 403


def test_evidence_bundle_and_verified_backup_include_episode_ledgers(episode_database):
    created = _create_episode(episodes.SqliteEpisodeRepository())
    episodes.SqliteExposureRepository().create(
        episodes.ExposureCreateBody(
            medication_name="Synthetic exposure",
            start_time="2026-07-01",
        ),
        ACTOR,
    )

    bundle = build_bundle(
        EvidenceBundleQuery(
            start=datetime(2026, 7, 1, tzinfo=timezone.utc),
            end=datetime(2026, 7, 31, tzinfo=timezone.utc),
            domains=(EvidenceDomain.CLINICAL,),
            question_intent="episode medication exposure",
            item_budget=20,
        )
    )
    canonical = [
        item
        for item in [
            *bundle["evidence"]["direct_observations"],
            *bundle["evidence"]["derived_metrics"],
        ]
        if item["entity_type"] in {"HealthEpisode", "MedicationExposure"}
    ]
    assert {item["entity_type"] for item in canonical} == {
        "HealthEpisode",
        "MedicationExposure",
    }
    episode_item = next(item for item in canonical if item["entity_id"] == created["id"])
    assert episode_item["data"]["temporal_association_only"] is True
    assert episode_item["confidence"]["limitations"] == [
        "Temporal membership and co-occurrence do not establish causation."
    ]

    backup, verification = create_verified_backup(
        episode_database.parent,
        episode_database.parent.parent / "backups",
        reason="synthetic-episode-ledger",
    )
    assert verification["health_episode_count"] == 1
    assert verification["episode_member_count"] == 3
    assert verification["episode_event_count"] == 1
    assert verification["medication_exposure_count"] == 1
    assert verification["open_ended_medication_exposure_count"] == 1
    assert verification["medication_exposure_event_count"] == 1
    assert verify_backup(backup) == verification
