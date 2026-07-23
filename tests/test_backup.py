import hashlib
import json
import sqlite3

import pytest

pytestmark = pytest.mark.risk_critical

from server import backup, db
from server.backup import BackupError, create_verified_backup, preflight_backup, restore_backup, verify_backup
from server.migrations import MIGRATIONS, run_migrations


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _legacy_data_dir(tmp_path, *, missing_record=False):
    data_dir = tmp_path / "data"
    records_dir = data_dir / "records"
    records_dir.mkdir(parents=True)
    record_name = "record.pdf"
    if not missing_record:
        (records_dir / record_name).write_bytes(b"private record bytes")
    database = data_dir / "app.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.executescript(
        """
        CREATE TABLE app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE entities (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            data TEXT NOT NULL,
            created_date TEXT NOT NULL,
            updated_date TEXT NOT NULL
        );
        CREATE INDEX idx_entities_type ON entities(type);
        CREATE INDEX idx_entities_type_ts
            ON entities(type, json_extract(data, '$.timestamp'));
        CREATE INDEX idx_entities_type_date
            ON entities(type, json_extract(data, '$.date'));
        """
    )
    connection.execute(
        "INSERT INTO entities VALUES (?, ?, ?, ?, ?)",
        (
            "record",
            "MedicalRecord",
            json.dumps({"stored_as": record_name, "filename": "private-name.pdf"}),
            "before",
            "before",
        ),
    )
    connection.execute(
        "INSERT INTO entities VALUES (?, ?, ?, ?, ?)",
        ("reading", "GlucoseReading", json.dumps({"value": 123}), "before", "before"),
    )
    connection.commit()
    return data_dir, connection


def test_online_backup_captures_wal_and_restores_records(tmp_path):
    data_dir, source_connection = _legacy_data_dir(tmp_path)
    backup_root = tmp_path / "backups"
    try:
        assert (data_dir / "app.sqlite3-wal").stat().st_size > 0
        backup_dir, verification = create_verified_backup(
            data_dir, backup_root, reason="test migration"
        )
    finally:
        source_connection.close()

    assert verification == {
        "integrity_check": "ok",
        "entity_total": 2,
        "record_file_count": 1,
        "referenced_record_count": 1,
        "missing_record_count": 0,
    }
    manifest = json.loads((backup_dir / "manifest.json").read_text())
    assert manifest["database"]["entity_counts"] == {
        "GlucoseReading": 1,
        "MedicalRecord": 1,
    }
    serialized = json.dumps(manifest)
    assert "private-name.pdf" not in serialized
    assert '"value": 123' not in serialized

    restored = tmp_path / "restored"
    restore_backup(backup_dir, restored)
    with sqlite3.connect(restored / "app.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM entities").fetchone() == (2,)
    assert (restored / "records" / "record.pdf").read_bytes() == b"private record bytes"


def test_low_space_preflight_does_not_modify_source(tmp_path):
    data_dir, connection = _legacy_data_dir(tmp_path)
    connection.close()
    database = data_dir / "app.sqlite3"
    record = data_dir / "records" / "record.pdf"
    before = (_hash(database), _hash(record))

    with pytest.raises(BackupError, match="insufficient backup space"):
        preflight_backup(data_dir, tmp_path / "backups", available_bytes=0)

    assert (_hash(database), _hash(record)) == before


def test_missing_record_reference_rejects_and_cleans_partial_backup(tmp_path):
    data_dir, connection = _legacy_data_dir(tmp_path, missing_record=True)
    connection.close()
    backup_root = tmp_path / "backups"

    with pytest.raises(BackupError, match="missing record files"):
        create_verified_backup(data_dir, backup_root)

    assert list(backup_root.iterdir()) == []
    with sqlite3.connect(data_dir / "app.sqlite3") as source:
        assert source.execute("SELECT COUNT(*) FROM entities").fetchone() == (2,)


def test_tampering_breaks_backup_verification(tmp_path):
    data_dir, connection = _legacy_data_dir(tmp_path)
    connection.close()
    backup_dir, _ = create_verified_backup(data_dir, tmp_path / "backups")
    (backup_dir / "records" / "record.pdf").write_bytes(b"changed")

    with pytest.raises(BackupError, match="record checksum mismatch"):
        verify_backup(backup_dir)


def test_restore_accepts_empty_directory_but_refuses_nonempty_target(tmp_path):
    data_dir, connection = _legacy_data_dir(tmp_path)
    connection.close()
    backup_dir, _ = create_verified_backup(data_dir, tmp_path / "backups")
    target = tmp_path / "existing"
    target.mkdir()

    restore_backup(backup_dir, target)
    assert (target / "app.sqlite3").is_file()

    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "keep.txt").write_text("keep")

    with pytest.raises(BackupError, match="new or empty"):
        restore_backup(backup_dir, nonempty)
    assert (nonempty / "keep.txt").read_text() == "keep"


def test_backup_failure_prevents_startup_migration(tmp_path, monkeypatch):
    data_dir, connection = _legacy_data_dir(tmp_path)
    connection.close()
    database = data_dir / "app.sqlite3"
    before = _hash(database)
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "MIGRATION_BACKUP_DIR", tmp_path / "backups")

    def fail_backup(*_args, **_kwargs):
        raise BackupError("preflight failed")

    monkeypatch.setattr(backup, "create_verified_backup", fail_backup)
    with pytest.raises(BackupError, match="preflight failed"):
        db.init_db()

    assert _hash(database) == before
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='schema_migrations'"
        ).fetchone() is None


def test_startup_backs_up_existing_database_before_migration(tmp_path, monkeypatch):
    data_dir, connection = _legacy_data_dir(tmp_path)
    connection.close()
    database = data_dir / "app.sqlite3"
    backup_root = tmp_path / "backups"
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "MIGRATION_BACKUP_DIR", backup_root)

    db.init_db()

    backups = list(backup_root.iterdir())
    assert len(backups) == 1
    assert verify_backup(backups[0])["entity_total"] == 2
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(migration.version,) for migration in MIGRATIONS]


def test_current_backup_verifies_clinical_review_audit_tables(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "records").mkdir(parents=True)
    run_migrations(data_dir / "app.sqlite3")

    backup_dir, verification = create_verified_backup(
        data_dir, tmp_path / "backups", reason="clinical review audit"
    )

    assert verification["clinical_review_thread_count"] == 0
    assert verification["clinical_review_event_count"] == 0
    restored = verify_backup(backup_dir)
    assert restored["clinical_review_thread_count"] == 0
    assert restored["clinical_review_event_count"] == 0
