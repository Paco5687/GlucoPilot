from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from server import db
from server.backup import create_verified_backup, verify_backup
from server.data_contracts import DEPLOYMENT_OWNER_ID
from server.migrations import run_migrations
from server.repositories import LegacyRepositoryCatalog, SourceArchiveRepository
from server.source_archive import (
    ArchivePolicy,
    SourceArchiveError,
    SqliteSourceArchiveRepository,
    source_archive_enabled,
)
from server.unit_of_work import SqliteUnitOfWork

pytestmark = pytest.mark.risk_critical


@pytest.fixture
def archive_database(tmp_path, monkeypatch):
    database = tmp_path / "app.sqlite3"
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    return database


@pytest.fixture
def archive(archive_database):
    return SqliteSourceArchiveRepository(policy=ArchivePolicy(retention_days=30, max_payload_bytes=4096))


def test_migration_adds_typed_archive_tables_constraints_and_repository(archive_database):
    with sqlite3.connect(archive_database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        triggers = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert {"source_records", "source_files", "sync_runs"} <= tables
    assert {"source_records_immutable", "source_files_immutable", "normalized_source_links_immutable"} <= triggers
    assert "normalized_source_links" in tables
    assert isinstance(LegacyRepositoryCatalog().source_archive, SourceArchiveRepository)


def test_payloads_are_scrubbed_before_hash_compression_and_deduplication(
    archive,
    archive_database,
):
    run = archive.start_sync_run(
        "synthetic-api",
        "parser-1.0.0",
        started_at="2026-01-01T00:00:00Z",
    )
    first_payload = {
        "reading": {"external_id": "synthetic-reading-1", "value": 123},
        "access_token": "synthetic-oauth-secret-one",
        "credentials": {"opaque": "synthetic-credential-object-one"},
        "nested": {"password": "synthetic-password-one"},
        "headers": {"Authorization": "Bearer synthetic-bearer-one"},
        "callback": "https://archive.example.invalid/cb?api_key=synthetic-query-one",
    }
    second_payload = {
        **first_payload,
        "access_token": "synthetic-oauth-secret-two",
        "credentials": {"opaque": "synthetic-credential-object-two"},
        "nested": {"password": "synthetic-password-two"},
        "headers": {"Authorization": "Bearer synthetic-bearer-two"},
        "callback": "https://archive.example.invalid/cb?api_key=synthetic-query-two",
    }

    first, first_created = archive.archive_payload(
        "synthetic-api",
        first_payload,
        "parser-1.0.0",
        external_id="Bearer synthetic-external-secret",
        observed_at="2026-01-01T00:00:00Z",
        received_at="2026-01-01T00:00:02Z",
        sync_run_id=run["id"],
    )
    second, second_created = archive.archive_payload(
        "synthetic-api",
        second_payload,
        "parser-1.0.0",
        external_id="synthetic-reading-1",
        observed_at="2026-01-01T00:00:00Z",
        received_at="2026-01-01T00:00:03Z",
        sync_run_id=run["id"],
    )

    assert first_created is True
    assert second_created is False
    assert second["id"] == first["id"]
    assert second["payload_hash"] == first["payload_hash"]
    assert first["content_encoding"] == "json+gzip"
    assert first["owner_id"] == DEPLOYMENT_OWNER_ID
    assert first["external_id"] == "Bearer [REDACTED]"
    assert first["stored_bytes"] > 0
    assert first["uncompressed_bytes"] > 0

    stored = archive.read_payload(first["id"])
    encoded = json.dumps(stored, sort_keys=True)
    assert stored["access_token"] == "[REDACTED]"
    assert stored["credentials"] == "[REDACTED]"
    assert stored["nested"]["password"] == "[REDACTED]"
    assert stored["headers"]["Authorization"] == "[REDACTED]"
    assert "api_key=[REDACTED]" in stored["callback"]
    for secret in (
        "synthetic-oauth-secret",
        "synthetic-password",
        "synthetic-bearer",
        "synthetic-query",
        "synthetic-external-secret",
        "synthetic-credential-object",
    ):
        assert secret not in encoded

    completed = archive.finish_sync_run(
        run["id"],
        "partial",
        completed_at="2026-01-01T00:01:00Z",
        error_summary="Bearer synthetic-error-secret access_token=synthetic-assignment",
    )
    assert completed["records_seen"] == 2
    assert completed["records_archived"] == 1
    assert completed["records_deduplicated"] == 1
    assert "synthetic-error-secret" not in completed["error_summary"]
    assert "synthetic-assignment" not in completed["error_summary"]

    second_run = archive.start_sync_run("synthetic-api", "parser-1.0.0")
    quoted = archive.finish_sync_run(
        second_run["id"],
        "failed",
        error_summary='provider returned {"access_token":"synthetic-json-token"}',
    )
    assert "synthetic-json-token" not in quoted["error_summary"]

    with sqlite3.connect(archive_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_records").fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE source_records SET external_id='changed' WHERE id=?",
                (first["id"],),
            )


def test_file_archive_stores_only_safe_references_and_deduplicates(
    archive,
    archive_database,
):
    file_hash = "sha256:" + hashlib.sha256(b"synthetic document bytes").hexdigest()
    first, first_created = archive.register_file(
        "synthetic-upload",
        "records/synthetic-record.pdf",
        file_hash,
        24,
        "records-parser-1.0.0",
        external_id="synthetic-record-1",
        received_at="2026-02-01T00:00:00Z",
        mime_type="application/pdf",
    )
    repeated, repeated_created = archive.register_file(
        "synthetic-upload",
        "duplicate-name.pdf",
        file_hash,
        24,
        "records-parser-1.0.0",
        received_at="2026-02-01T00:01:00Z",
    )

    assert first_created is True
    assert repeated_created is False
    assert repeated["id"] == first["id"]
    assert repeated["relative_path"] == "records/synthetic-record.pdf"

    valid_dotted, valid_dotted_created = archive.register_file(
        "synthetic-upload",
        "records/synthetic..version.pdf",
        "sha256:" + "b" * 64,
        25,
        "records-parser-1.0.0",
    )
    assert valid_dotted_created is True
    assert valid_dotted["relative_path"] == "records/synthetic..version.pdf"

    with sqlite3.connect(archive_database) as connection:
        columns = {row[1]: row[2].upper() for row in connection.execute("PRAGMA table_info(source_files)")}
        assert "payload" not in columns
        assert "BLOB" not in columns.values()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE source_files SET relative_path='records/changed.pdf' WHERE id=?",
                (first["id"],),
            )

    for unsafe in (".", "/absolute/record.pdf", "../private.pdf", "records/../private.pdf"):
        with pytest.raises(SourceArchiveError, match="safe relative path"):
            archive.register_file(
                "synthetic-upload",
                unsafe,
                "sha256:" + "a" * 64,
                1,
                "records-parser-1.0.0",
            )


def test_archive_size_retention_and_limits_are_observable(archive, archive_database):
    archive.archive_payload(
        "synthetic-api",
        {"id": "old", "value": 100},
        "parser-1.0.0",
        received_at="2026-01-01T00:00:00Z",
    )
    archive.archive_payload(
        "synthetic-api",
        {"id": "new", "value": 110},
        "parser-1.0.0",
        received_at="2026-03-01T00:00:00Z",
    )
    archive.register_file(
        "synthetic-upload",
        "records/old.pdf",
        "sha256:" + "a" * 64,
        100,
        "records-parser-1.0.0",
        received_at="2026-01-01T00:00:00Z",
    )
    archive.register_file(
        "synthetic-upload",
        "records/new.pdf",
        "sha256:" + "b" * 64,
        200,
        "records-parser-1.0.0",
        received_at="2026-03-01T00:00:00Z",
    )

    before = archive.stats()
    assert before["policy"] == {
        "retention_days": 30,
        "max_payload_bytes": 4096,
        "compression": "gzip",
    }
    assert before["records"]["count"] == 2
    assert before["records"]["stored_bytes"] > 0
    assert before["files"] == {"count": 2, "referenced_bytes": 300}

    assert archive.prune_before("2026-02-01T00:00:00Z") == {
        "source_records": 1,
        "source_files": 1,
    }
    after = archive.stats()
    assert after["records"]["count"] == 1
    assert after["files"]["count"] == 1

    tiny = SqliteSourceArchiveRepository(policy=ArchivePolicy(max_payload_bytes=20))
    with pytest.raises(SourceArchiveError, match="exceeds 20 byte"):
        tiny.archive_payload(
            "synthetic-api",
            {"payload": "this synthetic payload is intentionally too large"},
            "parser-1.0.0",
        )
    with sqlite3.connect(archive_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_records").fetchone() == (1,)


def test_archive_writes_join_the_existing_unit_of_work(archive_database):
    with pytest.raises(RuntimeError, match="reject archive transaction"):
        with SqliteUnitOfWork() as work:
            archived, created = work.repositories.source_archive.archive_payload(
                "synthetic-api",
                {"id": "atomic", "value": 120},
                "parser-1.0.0",
                received_at="2026-04-01T00:00:00Z",
            )
            assert created and archived["id"].startswith("src_")
            work.repositories.glucose.create(
                {
                    "owner_email": "owner@glucopilot.local",
                    "timestamp": "2026-04-01T00:00:00Z",
                    "value": 120,
                }
            )
            work.commit()
            raise RuntimeError("reject archive transaction")

    with sqlite3.connect(archive_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_records").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM entities").fetchone() == (0,)


def test_verified_backup_preserves_source_archive_metadata(
    archive,
    archive_database,
):
    run = archive.start_sync_run(
        "synthetic-api",
        "parser-1.0.0",
        started_at="2026-05-01T00:00:00Z",
    )
    archive.archive_payload(
        "synthetic-api",
        {"id": "backup-record", "value": 125},
        "parser-1.0.0",
        received_at="2026-05-01T00:00:01Z",
        sync_run_id=run["id"],
    )
    archive.register_file(
        "synthetic-upload",
        "records/backup-record.pdf",
        "sha256:" + "c" * 64,
        256,
        "records-parser-1.0.0",
        received_at="2026-05-01T00:00:02Z",
        sync_run_id=run["id"],
    )
    archive.finish_sync_run(
        run["id"],
        "succeeded",
        completed_at="2026-05-01T00:01:00Z",
    )

    backup_dir, verification = create_verified_backup(
        archive_database.parent,
        archive_database.parent.parent / "backups",
        reason="synthetic source archive",
    )

    manifest = json.loads((backup_dir / "manifest.json").read_text())
    archive_metadata = manifest["database"]["source_archive"]
    assert archive_metadata["source_records"]["count"] == 1
    assert archive_metadata["source_records"]["uncompressed_bytes"] > 0
    assert archive_metadata["source_records"]["stored_bytes"] > 0
    assert archive_metadata["source_files"] == {"count": 1, "referenced_bytes": 256}
    assert archive_metadata["sync_runs"] == {"count": 1}
    assert archive_metadata["normalized_source_links"] == {"count": 0}
    assert verification["source_record_count"] == 1
    assert verification["source_file_reference_count"] == 1
    assert verify_backup(backup_dir) == verification


def test_archive_feature_flag_is_off_by_default(monkeypatch):
    monkeypatch.delenv("SOURCE_ARCHIVE_ENABLED", raising=False)
    assert source_archive_enabled() is False
    monkeypatch.setenv("SOURCE_ARCHIVE_ENABLED", "true")
    assert source_archive_enabled() is True
