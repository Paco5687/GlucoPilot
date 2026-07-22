from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from server import db, dexcom, dexcom_share, fitbit, functions, glooko, google_health, nightscout, oura, tandem
from server.backup import create_verified_backup, verify_backup
from server.connector_provenance import (
    LINKABLE_ENTITY_TYPES,
    can_advance_freshness,
    capture_file,
    capture_payload,
    capture_records,
    connector_provenance_enabled,
    run_connector,
    source_failure,
)
from server.migrations import run_migrations
from server.source_archive import SqliteSourceArchiveRepository

pytestmark = pytest.mark.risk_critical


@pytest.fixture
def provenance_database(tmp_path, monkeypatch):
    database = tmp_path / "app.sqlite3"
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setenv("SOURCE_ARCHIVE_ENABLED", "true")
    monkeypatch.setenv("CONNECTOR_PROVENANCE_ENABLED", "true")
    return database


CONNECTOR_CASES = [
    ("dexcom", dexcom, "handle", {"action": "sync"}, "dexcom"),
    ("dexcomShare", dexcom_share, "handle", {"action": "sync"}, "dexcom_share"),
    ("nightscout", nightscout, "handle", {"action": "sync"}, "nightscout"),
    ("tandem", tandem, "handle", {"action": "sync"}, "tandem"),
    ("glooko", glooko, "handle", {"action": "sync"}, "glooko"),
    ("fitbit", fitbit, "handle", {"action": "sync"}, "fitbit"),
    ("googleHealth", google_health, "handle", {"action": "sync"}, "google_health"),
    ("ouraSync", oura, "handle_sync", {"days": 2}, "oura"),
]


@pytest.mark.parametrize(("function_name", "module", "handler", "body", "source_type"), CONNECTOR_CASES)
def test_every_connector_records_complete_success_and_normalized_source_link(
    provenance_database,
    monkeypatch,
    function_name,
    module,
    handler,
    body,
    source_type,
):
    entity_type = sorted(LINKABLE_ENTITY_TYPES[source_type])[0]

    async def synthetic_handler(_body):
        capture_payload(
            {
                "external_id": f"synthetic-{source_type}-1",
                "observed_at": "2026-06-01T12:00:00Z",
                "value": 123,
                "access_token": "synthetic-token-must-not-persist",
            },
            external_id=f"synthetic-{source_type}-1",
            observed_at="2026-06-01T12:00:00Z",
        )
        created = db.create_entity(
            entity_type,
            {"owner_email": "owner@glucopilot.local", "synthetic": source_type},
        )
        return {"ok": True, "created_id": created["id"]}

    monkeypatch.setattr(module, handler, synthetic_handler)
    result = asyncio.run(functions._dispatch(function_name, body))

    assert result["provenance_status"] == "succeeded"
    with sqlite3.connect(provenance_database) as connection:
        connection.row_factory = sqlite3.Row
        run = connection.execute("SELECT * FROM sync_runs").fetchone()
        assert run["source_type"] == source_type
        assert run["run_kind"] == "connector"
        assert run["trigger_type"] == "manual"
        assert run["fetched_count"] == 1
        assert run["created_count"] == 1
        assert run["updated_count"] == 0
        assert run["failed_count"] == 0
        assert run["last_successful_data_at"] == "2026-06-01T12:00:00.000Z"
        link = connection.execute("SELECT * FROM normalized_source_links").fetchone()
        assert link["entity_type"] == entity_type
        assert link["sync_run_id"] == run["id"]
        entity = connection.execute("SELECT data FROM entities WHERE id=?", (link["entity_id"],)).fetchone()
        assert "source_record_id" not in entity["data"]

    repository = SqliteSourceArchiveRepository()
    assert repository.recent_sync_runs(source_type, limit=1)[0]["id"] == result["provenance_run_id"]
    assert repository.links_for_entity(entity_type, link["entity_id"])[0]["id"] == link["id"]
    manifest = repository.read_payload(link["source_record_id"])
    assert manifest["source_type"] == source_type
    assert len(manifest["source_record_ids"]) == 1
    raw = repository.read_payload(manifest["source_record_ids"][0])
    assert raw["access_token"] == "[REDACTED]"


def test_partial_run_links_completed_writes_but_does_not_advance_freshness(provenance_database):
    async def partial_operation():
        capture_payload(
            {"records": [{"id": "first", "timestamp": "2026-06-02T10:00:00Z"}]},
            observed_at="2026-06-02T10:00:00Z",
        )
        created = db.create_entity(
            "GlucoseReading",
            {"owner_email": "owner@glucopilot.local", "timestamp": "2026-06-02T10:00:00Z"},
        )
        db.update_entity("GlucoseReading", created["id"], {"value": 124})
        source_failure("synthetic provider page two failed")
        assert can_advance_freshness() is False
        return {"ok": True, "records_skipped": 3, "records_stale": 2}

    result = asyncio.run(run_connector("dexcom", "sync", partial_operation))
    assert result["provenance_status"] == "partial"

    with sqlite3.connect(provenance_database) as connection:
        connection.row_factory = sqlite3.Row
        run = connection.execute("SELECT * FROM sync_runs").fetchone()
        assert run["status"] == "partial"
        assert run["created_count"] == 1
        assert run["updated_count"] == 1
        assert run["failed_count"] == 1
        assert run["skipped_count"] == 3
        assert run["stale_count"] == 2
        assert run["last_successful_data_at"] is None
        assert "provider page two failed" in run["error_summary"]
        assert connection.execute("SELECT COUNT(*) FROM normalized_source_links").fetchone()[0] == 1

    stats = SqliteSourceArchiveRepository().stats()
    assert stats["sync_runs"]["partial"] == 1
    assert stats["sync_runs"]["succeeded"] == 0
    assert stats["sync_runs"]["last_successful_data_at"] is None


def test_failed_run_has_complete_outcome_and_no_false_freshness(provenance_database):
    async def failed_operation():
        raise RuntimeError("synthetic connector unavailable")

    with pytest.raises(RuntimeError, match="synthetic connector unavailable"):
        asyncio.run(run_connector("fitbit", "sync", failed_operation, trigger_type="scheduled"))

    with sqlite3.connect(provenance_database) as connection:
        connection.row_factory = sqlite3.Row
        run = connection.execute("SELECT * FROM sync_runs").fetchone()
        assert run["status"] == "failed"
        assert run["trigger_type"] == "scheduled"
        assert run["failed_count"] == 1
        assert run["last_successful_data_at"] is None
        assert connection.execute("SELECT COUNT(*) FROM normalized_source_links").fetchone()[0] == 0


def test_normalized_write_without_evidence_is_visible_as_partial(provenance_database):
    async def operation():
        db.create_entity(
            "FitbitDaily",
            {"owner_email": "owner@glucopilot.local", "date": "2026-06-02"},
        )
        return {"success": True}

    result = asyncio.run(run_connector("fitbit", "sync", operation))
    assert result["provenance_status"] == "partial"
    with sqlite3.connect(provenance_database) as connection:
        connection.row_factory = sqlite3.Row
        run = connection.execute("SELECT * FROM sync_runs").fetchone()
        assert run["failed_count"] == 1
        assert run["last_successful_data_at"] is None
        assert "without captured source evidence" in run["error_summary"]


def test_upload_and_ingest_runs_link_file_and_payload_evidence(provenance_database):
    async def upload_operation():
        capture_file(
            "synthetic-record.pdf",
            "sha256:" + "d" * 64,
            512,
            external_id="synthetic-record",
            mime_type="application/pdf",
        )
        return db.create_entity(
            "MedicalRecord",
            {"owner_email": "owner@glucopilot.local", "stored_as": "synthetic-record.pdf"},
        )

    async def ingest_operation():
        capture_payload(
            {"rows": [{"date": "2026-06-03", "flow": "medium"}]},
            observed_at="2026-06-03T00:00:00Z",
        )
        return db.create_entity(
            "PeriodLog",
            {"owner_email": "owner@glucopilot.local", "date": "2026-06-03"},
        )

    asyncio.run(
        run_connector(
            "medical_record_upload",
            "upload",
            upload_operation,
            trigger_type="upload",
            run_kind="upload",
        )
    )
    asyncio.run(
        run_connector(
            "cycle_ingest",
            "ingest",
            ingest_operation,
            trigger_type="ingest",
            run_kind="ingest",
        )
    )

    with sqlite3.connect(provenance_database) as connection:
        connection.row_factory = sqlite3.Row
        runs = connection.execute("SELECT * FROM sync_runs ORDER BY started_at").fetchall()
        assert [(run["run_kind"], run["trigger_type"], run["status"]) for run in runs] == [
            ("upload", "upload", "succeeded"),
            ("ingest", "ingest", "succeeded"),
        ]
        links = connection.execute("SELECT * FROM normalized_source_links ORDER BY entity_type").fetchall()
        assert len(links) == 2
        assert any(link["source_file_id"] and not link["source_record_id"] for link in links)
        assert any(link["source_record_id"] and not link["source_file_id"] for link in links)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE normalized_source_links SET entity_type='changed' WHERE id=?",
                (links[0]["id"],),
            )

    period = db.query_entities("PeriodLog", limit=1)[0]
    assert db.delete_entity("PeriodLog", period["id"]) is True
    with sqlite3.connect(provenance_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM normalized_source_links").fetchone()[0] == 1


def test_repeated_payload_deduplicates_while_each_run_keeps_its_link(provenance_database):
    async def operation():
        capture_payload(
            {"records": [{"id": "same", "timestamp": "2026-06-04T00:00:00Z", "value": 130}]},
            observed_at="2026-06-04T00:00:00Z",
        )
        return db.create_entity(
            "GlucoseReading",
            {"owner_email": "owner@glucopilot.local", "timestamp": "2026-06-04T00:00:00Z"},
        )

    first = asyncio.run(run_connector("dexcom", "sync", operation))
    second = asyncio.run(run_connector("dexcom", "sync", operation))
    assert first["provenance_status"] == second["provenance_status"] == "succeeded"

    with sqlite3.connect(provenance_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sync_runs").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM source_records").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM normalized_source_links").fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(DISTINCT sync_run_id) FROM normalized_source_links"
        ).fetchone() == (2,)


def test_large_fetches_are_archived_in_bounded_chunks(provenance_database):
    async def operation():
        records = [
            {"id": f"record-{index}", "timestamp": "2026-06-05T00:00:00Z", "value": index}
            for index in range(501)
        ]
        capture_records(records, external_id="synthetic-page", chunk_size=250)
        return db.create_entity(
            "GlucoseReading",
            {"owner_email": "owner@glucopilot.local", "timestamp": "2026-06-05T00:00:00Z"},
        )

    result = asyncio.run(run_connector("dexcom", "backfill", operation))
    with sqlite3.connect(provenance_database) as connection:
        connection.row_factory = sqlite3.Row
        run = connection.execute("SELECT * FROM sync_runs").fetchone()
        link = connection.execute("SELECT * FROM normalized_source_links").fetchone()
        assert run["trigger_type"] == "backfill"
        assert run["fetched_count"] == 501
        assert connection.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 4
    manifest = SqliteSourceArchiveRepository().read_payload(link["source_record_id"])
    assert len(manifest["source_record_ids"]) == 3
    assert result["provenance_status"] == "succeeded"


def test_verified_backup_preserves_normalized_source_links(provenance_database):
    async def operation():
        capture_payload(
            {"id": "backup-source", "timestamp": "2026-06-06T00:00:00Z"},
            observed_at="2026-06-06T00:00:00Z",
        )
        return db.create_entity(
            "GlucoseReading",
            {"owner_email": "owner@glucopilot.local", "timestamp": "2026-06-06T00:00:00Z"},
        )

    asyncio.run(run_connector("dexcom", "sync", operation))
    backup_dir, verification = create_verified_backup(
        provenance_database.parent,
        provenance_database.parent.parent / "provenance-backups",
        reason="synthetic connector provenance",
    )
    manifest = json.loads((backup_dir / "manifest.json").read_text())
    assert manifest["database"]["source_archive"]["normalized_source_links"] == {"count": 1}
    assert verify_backup(backup_dir) == verification


def test_feature_flags_keep_existing_connector_behavior_unchanged(provenance_database, monkeypatch):
    monkeypatch.setenv("CONNECTOR_PROVENANCE_ENABLED", "false")
    assert connector_provenance_enabled() is False
    monkeypatch.setenv("CONNECTOR_PROVENANCE_ENABLED", "true")
    monkeypatch.setenv("SOURCE_ARCHIVE_ENABLED", "false")
    assert connector_provenance_enabled() is False

    async def operation():
        return {"ok": True, "created": 0}

    assert asyncio.run(run_connector("dexcom", "sync", operation)) == {"ok": True, "created": 0}
    with sqlite3.connect(provenance_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sync_runs").fetchone() == (0,)
