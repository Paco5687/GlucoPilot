import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

pytestmark = pytest.mark.risk_critical

from server.migrations import (
    MIGRATIONS,
    Migration,
    MigrationError,
    Statement,
    pending_migration_versions,
    run_migrations,
)
from server.schema_registry import ENTITY_SCHEMAS, GENERIC_API_TYPES

MIGRATION_VERSIONS = [migration.version for migration in MIGRATIONS]


def _create_legacy_database(path):
    with sqlite3.connect(path) as connection:
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
            ("existing", "GlucoseReading", "{\"value\":123}", "before", "before"),
        )


def _schema_snapshot(path):
    with sqlite3.connect(path) as connection:
        table_names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        tables = {
            name: connection.execute(f'PRAGMA table_info("{name}")').fetchall()
            for name in table_names
        }
        index_names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        indexes = {
            name: connection.execute(f'PRAGMA index_xinfo("{name}")').fetchall()
            for name in index_names
        }
        registry = connection.execute(
            "SELECT * FROM entity_schema_registry ORDER BY entity_type"
        ).fetchall()
    return tables, indexes, registry


def test_clean_and_legacy_databases_converge_without_data_loss(tmp_path):
    clean = tmp_path / "clean.sqlite3"
    upgraded = tmp_path / "upgraded.sqlite3"
    _create_legacy_database(upgraded)

    assert run_migrations(clean) == MIGRATION_VERSIONS
    assert run_migrations(upgraded) == MIGRATION_VERSIONS

    assert _schema_snapshot(clean) == _schema_snapshot(upgraded)
    with sqlite3.connect(upgraded) as connection:
        assert connection.execute(
            "SELECT id FROM entities WHERE id='existing'"
        ).fetchone() == ("existing",)


def test_migrations_are_idempotent_and_checksummed(tmp_path):
    database = tmp_path / "app.sqlite3"

    assert pending_migration_versions(database) == MIGRATION_VERSIONS
    assert run_migrations(database) == MIGRATION_VERSIONS
    assert pending_migration_versions(database) == []
    assert run_migrations(database) == []

    with sqlite3.connect(database) as connection:
        applied = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert applied == [
        (migration.version, migration.name, migration.checksum) for migration in MIGRATIONS
    ]


def test_failed_pending_migration_rolls_back_the_entire_invocation(tmp_path):
    database = tmp_path / "app.sqlite3"
    run_migrations(database)
    broken = (
        *MIGRATIONS,
        Migration(
            len(MIGRATIONS) + 1,
            "broken_migration",
            (
                Statement("CREATE TABLE should_roll_back (id INTEGER)"),
                Statement("THIS IS NOT SQL"),
            ),
        ),
    )

    with pytest.raises(
        MigrationError,
        match=rf"migration {len(MIGRATIONS) + 1} \(broken_migration\) failed",
    ):
        run_migrations(database, broken)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='should_roll_back'"
        ).fetchone() is None
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (
            len(MIGRATIONS),
        )


def test_applied_migration_drift_prevents_startup(tmp_path):
    database = tmp_path / "app.sqlite3"
    run_migrations(database)
    changed = (
        Migration(
            MIGRATIONS[0].version,
            MIGRATIONS[0].name,
            (*MIGRATIONS[0].statements, Statement("SELECT 1")),
        ),
        *MIGRATIONS[1:],
    )

    with pytest.raises(MigrationError, match="checksum drift"):
        run_migrations(database, changed)


def test_schema_registry_drift_prevents_startup(tmp_path):
    database = tmp_path / "app.sqlite3"
    run_migrations(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE entity_schema_registry SET description='changed' "
            "WHERE entity_type='GlucoseReading'"
        )

    with pytest.raises(MigrationError, match="schema registry drift"):
        run_migrations(database)


def test_newer_database_prevents_downgrade_startup(tmp_path):
    database = tmp_path / "app.sqlite3"
    run_migrations(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?, 'future', 'future', 'future', 0)",
            (len(MIGRATIONS) + 1,),
        )

    with pytest.raises(MigrationError, match="newer than this application"):
        run_migrations(database)


def test_concurrent_runners_apply_each_migration_once(tmp_path):
    database = tmp_path / "app.sqlite3"
    barrier = Barrier(2)

    def migrate():
        barrier.wait()
        return run_migrations(database)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: migrate(), range(2)))

    assert sorted(results, key=len) == [[], MIGRATION_VERSIONS]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone() == (
            len(MIGRATIONS),
        )


def test_registry_covers_all_known_types_without_expanding_generic_api():
    assert len(ENTITY_SCHEMAS) == 34
    assert len({schema.name for schema in ENTITY_SCHEMAS}) == 34
    assert len(GENERIC_API_TYPES) == 19
    assert "GoogleHealthConnection" not in GENERIC_API_TYPES
    assert "GlucoseReading" in GENERIC_API_TYPES
