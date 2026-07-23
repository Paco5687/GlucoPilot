"""H1 platform diagnostics freshness, privacy, and authorization coverage."""

import base64
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from server import db, diagnostics
from server.backup import create_verified_backup
from server.migrations import run_migrations
from server.source_archive import SqliteSourceArchiveRepository


pytestmark = pytest.mark.risk_critical
OWNER = "owner@glucopilot.local"
AS_OF = datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc)


def _session(role: str) -> str:
    payload = base64.b64encode(json.dumps({
        "logged_in": True,
        "role": role,
        "provider_name": "Synthetic Provider",
    }).encode())
    return TimestampSigner("test-secret-key").sign(payload).decode()


@pytest.fixture
def diagnostic_database(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    database = data_dir / "app.sqlite3"
    data_dir.mkdir()
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setenv("RELATIONSHIP_READS_ENABLED", "false")
    monkeypatch.setenv("RELATIONSHIP_PROJECTION_WRITES_ENABLED", "false")

    db.create_entity("DexcomConnection", {
        "owner_email": OWNER,
        "connected": True,
        "last_sync": "2026-07-23T14:00:00Z",
        "access_token": "synthetic-token-must-not-leak",
    })
    db.create_entity("GoogleHealthConnection", {
        "owner_email": OWNER,
        "connected": True,
        "access_token": "google-token-must-not-leak",
    })
    db.create_entity("FitbitConnection", {
        "owner_email": OWNER,
        "connected": True,
        "access_token": "fitbit-token-must-not-leak",
    })
    db.create_entity("FitbitDaily", {
        "owner_email": OWNER,
        "date": "2026-07-22",
        "steps": 1234,
    })
    db.create_entity("UserSettings", {
        "owner_email": OWNER,
        "nightscout_connected": True,
        "nightscout_url": "https://private-nightscout.example.test",
        "last_nightscout_sync": "2026-07-23T13:30:00Z",
    })
    db.create_entity("GlucoseReading", {
        "owner_email": OWNER,
        "source": "nightscout",
        "timestamp": "2026-07-23T13:25:00Z",
        "value": 111,
    })
    db.create_entity("GlucoseReading", {
        "owner_email": OWNER,
        "source": "dexcom",
        "timestamp": "2026-07-22T12:00:00Z",
        "value": 123,
    })
    db.create_entity("OuraDaily", {
        "owner_email": OWNER,
        "date": "2026-06-01",
        "sleep_score": 88,
    })
    db.create_entity("LabResult", {
        "owner_email": OWNER,
        "test_name": "Synthetic private result",
        "verification_status": "unverified",
        "validation_status": "valid",
        "private_note": "private@example.test token=secret-value",
    })
    db.create_entity("Pattern", {
        "owner_email": OWNER,
        "title": "Synthetic pattern",
        "is_active": True,
        "date_generated": "2026-07-23T10:00:00Z",
        "analytics_confidence": {"confidence_score": 0.8},
    })

    archive = SqliteSourceArchiveRepository()
    successful = archive.start_sync_run(
        "dexcom",
        "synthetic-parser",
        started_at="2026-07-23T13:55:00Z",
    )
    archive.finish_sync_run(
        successful["id"],
        "succeeded",
        completed_at="2026-07-23T14:00:00Z",
        fetched_count=12,
        created_count=10,
        skipped_count=2,
        last_successful_data_at="2026-07-22T12:00:00Z",
    )
    failed = archive.start_sync_run(
        "glooko",
        "synthetic-parser",
        started_at="2026-07-23T14:30:00Z",
    )
    archive.finish_sync_run(
        failed["id"],
        "failed",
        completed_at="2026-07-23T14:31:00Z",
        error_summary=(
            "private@example.test token=secret-value "
            "https://private.example.test/path"
        ),
        failed_count=1,
    )

    backup_root = tmp_path / "backups"
    create_verified_backup(data_dir, backup_root, reason="synthetic-diagnostics")
    monkeypatch.setattr(diagnostics, "MIGRATION_BACKUP_DIR", backup_root)
    return database, backup_root


def _source(body, key):
    return next(item for item in body["sources"] if item["source"] == key)


def test_diagnostics_unify_freshness_failures_quality_graph_analytics_and_backup(
    diagnostic_database,
):
    _database, backup_root = diagnostic_database
    body = diagnostics.build_diagnostics(as_of=AS_OF, backup_root=backup_root)

    assert body["contract_version"] == diagnostics.CONTRACT_VERSION
    assert body["semantics"]["not_health_findings"] is True
    assert body["status"] == "critical"

    dexcom = _source(body, "dexcom")
    assert dexcom["status"] == "current"
    assert dexcom["tracking"] == "governed"
    assert dexcom["last_successful_sync_at"] == "2026-07-23T14:00:00Z"
    assert dexcom["data_through"] == "2026-07-22T12:00:00Z"
    assert dexcom["import_lag_seconds"] == 93_600
    assert dexcom["latest_run_counts"] == {
        "fetched_count": 12,
        "created_count": 10,
        "updated_count": 0,
        "skipped_count": 2,
        "failed_count": 0,
        "stale_count": 0,
    }

    assert _source(body, "oura")["status"] == "stale"
    assert _source(body, "fitbit")["status"] == "current"
    assert _source(body, "fitbit")["data_through"] == "2026-07-22T00:00:00Z"
    assert _source(body, "nightscout")["last_successful_sync_at"] == (
        "2026-07-23T13:30:00Z"
    )
    assert _source(body, "google_health")["status"] == "error"
    assert _source(body, "glooko")["status"] == "error"
    assert body["quality"]["counters"]["sync_failed_runs"] == 1
    assert body["quality"]["counters"]["sync_duplicate_or_skipped_items"] == 2
    assert body["quality"]["counters"]["unverified_records"] == 1
    assert body["graph"]["status"] == "inactive"
    assert body["analytics"]["status"] == "current"
    assert body["storage"]["database_bytes"] > 0
    assert body["storage"]["backup"]["status"] == "current"


def test_diagnostics_never_expose_secrets_raw_phi_paths_or_error_text(
    diagnostic_database,
):
    _database, backup_root = diagnostic_database
    body = diagnostics.build_diagnostics(as_of=AS_OF, backup_root=backup_root)
    encoded = json.dumps(body, sort_keys=True).lower()

    for forbidden in (
        "private@example.test",
        "secret-value",
        "private.example.test",
        "private-nightscout.example.test",
        "fitbit-token-must-not-leak",
        "synthetic private result",
        OWNER,
        str(backup_root).lower(),
        "access_token",
        "error_summary",
        "sha256:",
    ):
        assert forbidden not in encoded
    with db.connect() as connection:
        run_ids = [row[0] for row in connection.execute("SELECT id FROM sync_runs")]
    assert all(run_id not in encoded for run_id in run_ids)
    assert set(body["semantics"]) == {
        "category",
        "not_health_findings",
        "message",
    }


def test_reasoning_context_excludes_backup_operations_but_keeps_source_caveats(
    diagnostic_database,
    monkeypatch,
):
    monkeypatch.setattr(
        diagnostics,
        "_storage",
        lambda *_args, **_kwargs: pytest.fail(
            "reasoning context must not inspect storage or backups"
        ),
    )
    context = diagnostics.reasoning_context(as_of=AS_OF)

    assert context["semantics"]["not_health_findings"] is True
    assert any(item["category"] == "source_health" for item in context["caveats"])
    assert all(item["category"] != "backup_freshness" for item in context["caveats"])
    assert all("latest_run_counts" not in source for source in context["sources"])


def test_diagnostics_api_requires_login_and_allows_read_only_provider(
    diagnostic_database,
):
    from server.main import app

    with TestClient(app) as client:
        assert client.get("/api/diagnostics").status_code == 401
        client.cookies.set("session", _session("provider"))
        provider = client.get("/api/diagnostics")
        client.cookies.clear()
        client.cookies.set("session", _session("admin"))
        owner = client.get("/api/diagnostics")

    assert provider.status_code == 200
    assert owner.status_code == 200
    assert provider.json()["contract_version"] == diagnostics.CONTRACT_VERSION
    assert provider.json()["semantics"]["not_health_findings"] is True
