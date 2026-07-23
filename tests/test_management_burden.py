"""Risk-critical regression coverage for P5 management-effort metrics."""

import base64
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from itsdangerous import TimestampSigner

from server import db
from server.backup import create_verified_backup
from server.config import OWNER_EMAIL
from server.data_contracts import DEPLOYMENT_OWNER_ID
from server.evidence_bundle import (
    EvidenceBundleQuery,
    EvidenceDomain,
    build_bundle,
    clear_bundle_cache,
)
from server.management_burden import (
    ALGORITHM_VERSION,
    _confidence,
    _insert,
    analysis_for_range,
)
from server.migrations import run_migrations


pytestmark = pytest.mark.risk_critical
BASE = datetime(2026, 7, 1, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def burden_database(tmp_path, monkeypatch):
    database = tmp_path / "data" / "app.sqlite3"
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setenv("RELATIONSHIP_READS_ENABLED", "false")
    monkeypatch.setenv("EVIDENCE_SET_READS_ENABLED", "false")
    clear_bundle_cache()
    yield database
    clear_bundle_cache()


def _entity(entity_type, entity_id, **data):
    now = "2026-07-01T00:00:00Z"
    payload = {"owner_email": OWNER_EMAIL, **data}
    with db.connect() as connection:
        connection.execute(
            """
            INSERT INTO entities(id,type,data,created_date,updated_date)
            VALUES (?,?,?,?,?)
            """,
            (entity_id, entity_type, json.dumps(payload), now, now),
        )


def _session(role):
    payload = base64.b64encode(
        json.dumps(
            {
                "logged_in": True,
                "role": role,
                "provider_name": "Synthetic Provider",
            }
        ).encode()
    )
    return TimestampSigner("test-secret-key").sign(payload).decode()


def test_components_are_visible_and_missing_sources_reduce_confidence(burden_database):
    for index in range(14):
        instant = BASE + timedelta(days=index)
        timestamp = instant.isoformat().replace("+00:00", "Z")
        _entity(
            "Treatment",
            f"bolus-{index}",
            timestamp=timestamp,
            type="insulin",
            event_type="Bolus",
            amount=1,
            source="synthetic-pump",
        )
        _entity(
            "FingerstickReading",
            f"finger-{index}",
            timestamp=(instant + timedelta(hours=8)).isoformat().replace("+00:00", "Z"),
            value=110,
            source="synthetic-meter",
        )
        _entity(
            "GlucoseReading",
            f"glucose-{index}",
            timestamp=(instant + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            value=120,
            source="synthetic-cgm",
        )

    result = analysis_for_range(BASE, BASE + timedelta(days=13, hours=23))

    assert result["algorithm_version"] == ALGORITHM_VERSION
    assert result["semantic_class"] == "calculated_descriptive_management_effort"
    assert {item["category"] for item in result["components"]} >= {
        "bolus",
        "fingerstick",
        "awakening",
    }
    bolus = next(item for item in result["components"] if item["category"] == "bolus")
    assert bolus == {
        "category": "bolus",
        "events": 14,
        "minutes": 28.0,
        "interactions": 14,
        "weighted_points": 14,
        "weight": 1,
    }
    assert "ketones" in result["source_coverage"]["missing"]
    assert result["analytics_confidence"]["source_coverage"] == result["source_coverage"]
    assert result["analytics_confidence"]["confidence_score"] < (
        result["analytics_confidence"]["event_confidence_mean"]
    )
    assert "not counted as zero" in result["language"]["missing_sources"]
    assert result["outcome_vs_effort"]["causal_allowed"] is False


def test_rescue_carbs_and_device_changes_use_declared_source_context(burden_database):
    _entity(
        "GlucoseReading",
        "low",
        timestamp=BASE.isoformat().replace("+00:00", "Z"),
        value=55,
        source="synthetic-cgm",
    )
    _entity(
        "Treatment",
        "carbs",
        timestamp=(BASE + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        type="carb",
        amount=15,
        source="synthetic-pump",
    )
    _entity(
        "Treatment",
        "site-change",
        timestamp=(BASE + timedelta(hours=8)).isoformat().replace("+00:00", "Z"),
        type="note",
        event_type="Pump site change",
        source="synthetic-pump",
    )

    result = analysis_for_range(BASE - timedelta(minutes=1), BASE + timedelta(days=1))
    by_category = {item["category"]: item for item in result["components"]}

    assert by_category["rescue_carbs"]["events"] == 1
    assert by_category["rescue_carbs"]["weight"] == 2
    assert by_category["device_change"]["minutes"] == 15
    rescue = next(event for event in result["events"] if event["category"] == "rescue_carbs")
    assert rescue["origin_kind"] == "inferred"
    assert rescue["confidence"]["basis"].startswith("carbohydrate record")


def test_append_only_correction_excludes_without_deleting_source(burden_database):
    _entity(
        "Treatment",
        "bolus",
        timestamp=BASE.isoformat().replace("+00:00", "Z"),
        type="insulin",
        event_type="Bolus",
        amount=1,
        source="synthetic-pump",
    )
    initial = analysis_for_range(BASE - timedelta(minutes=1), BASE + timedelta(days=1))
    original = next(event for event in initial["events"] if event["category"] == "bolus")

    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _insert(
            connection,
            occurred_at=original["occurred_at"],
            category="bolus",
            origin_kind="correction",
            duration_minutes=0,
            interaction_count=0,
            confidence=_confidence(1, "test correction"),
            source_entity_type=None,
            source_entity_id=None,
            identity={"correction_of": original["id"], "test": True},
            correction_of_id=original["id"],
            excluded=True,
            actor_role="admin",
            actor_label="Test owner",
            reason="Synthetic duplicate.",
        )
        connection.commit()

    result = analysis_for_range(BASE - timedelta(minutes=1), BASE + timedelta(days=1))
    corrected = next(event for event in result["events"] if event["original_event_id"] == original["id"])
    assert corrected["effective"] is False
    assert corrected["corrected_by"]
    assert not any(item["category"] == "bolus" for item in result["components"])
    with sqlite3.connect(burden_database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM management_burden_events WHERE id=?",
            (original["id"],),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM management_burden_events WHERE correction_of_id=?",
            (original["id"],),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM management_burden_audit WHERE actor_role='admin'",
        ).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute(
                "DELETE FROM management_burden_events WHERE owner_id=?",
                (DEPLOYMENT_OWNER_ID,),
            )


def test_good_control_can_surface_high_measured_effort(burden_database):
    for index in range(14):
        instant = BASE + timedelta(days=index, hours=8)
        for event_index in range(7):
            _entity(
                "Treatment",
                f"bolus-{index}-{event_index}",
                timestamp=(instant + timedelta(minutes=event_index)).isoformat().replace("+00:00", "Z"),
                type="insulin",
                event_type="Bolus",
                amount=0.5,
                source="synthetic-pump",
            )
        _entity(
            "GlucoseReading",
            f"glucose-{index}",
            timestamp=instant.isoformat().replace("+00:00", "Z"),
            value=120,
            source="synthetic-cgm",
        )

    result = analysis_for_range(BASE, BASE + timedelta(days=13, hours=23))

    assert result["outcomes"]["time_in_range_pct"] == 100
    assert result["summary"]["measured_effort_index"] >= 60
    assert result["outcome_vs_effort"]["sustainability_review_flag"] is True
    assert "sustainability" in result["outcome_vs_effort"]["language"].lower()


def test_verified_backup_covers_burden_ledger(burden_database, tmp_path):
    _entity(
        "Treatment",
        "bolus",
        timestamp=BASE.isoformat().replace("+00:00", "Z"),
        type="insulin",
        event_type="Bolus",
        amount=1,
        source="synthetic-pump",
    )
    analysis_for_range(BASE - timedelta(minutes=1), BASE + timedelta(days=1))

    _, verification = create_verified_backup(
        burden_database.parent,
        tmp_path / "backups",
        reason="p5-test",
    )

    assert verification["management_burden_event_count"] >= 1
    assert verification["observed_management_burden_event_count"] == 1
    assert verification["management_burden_audit_count"] >= 1
    assert verification["integrity_check"] == "ok"


def test_evidence_bundle_exposes_source_linked_burden_summary(burden_database):
    _entity(
        "Treatment",
        "bolus",
        timestamp=BASE.isoformat().replace("+00:00", "Z"),
        type="insulin",
        event_type="Bolus",
        amount=1,
        source="synthetic-pump",
    )

    bundle = build_bundle(
        EvidenceBundleQuery(
            start=BASE - timedelta(minutes=1),
            end=BASE + timedelta(days=1),
            domains=[EvidenceDomain.ANALYTICS],
            question_intent="management effort burden bolus",
            item_budget=20,
        )
    )

    item = next(
        candidate
        for candidate in bundle["evidence"]["derived_metrics"]
        if candidate["entity_type"] == "ManagementBurdenSummary"
    )
    assert bundle["bundle_version"] == "2.5.0"
    assert item["data"]["semantic_class"] == (
        "calculated_descriptive_management_effort"
    )
    assert item["confidence"]["limitations"]
    assert any(
        link["entity_type"] == "Treatment"
        for link in item["source_links"]
    )


def test_provider_can_read_but_only_admin_can_write(client, burden_database):
    client.cookies.set("session", _session("provider"))
    response = client.get("/api/management-burden?days=30")
    assert response.status_code == 200
    assert response.json()["can_edit"] is False
    denied = client.post(
        "/api/management-burden/events",
        json={
            "occurred_at": BASE.isoformat(),
            "category": "ketone",
            "duration_minutes": 5,
        },
    )
    assert denied.status_code == 403

    client.cookies.set("session", _session("admin"))
    created = client.post(
        "/api/management-burden/events",
        json={
            "occurred_at": BASE.isoformat(),
            "category": "ketone",
            "duration_minutes": 5,
            "notes": "Synthetic only",
        },
    )
    assert created.status_code == 200
    assert created.json()["origin_kind"] == "manual"
    client.cookies.clear()
