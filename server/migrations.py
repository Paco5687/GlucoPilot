"""Ordered, checksummed, transactional SQLite schema migrations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from .config import DB_PATH
from .schema_registry import BASELINE_ENTITY_SCHEMAS, ENTITY_SCHEMAS


TRACKING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0)
)
"""


@dataclass(frozen=True)
class Statement:
    sql: str
    parameters: tuple[Any, ...] = ()


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[Statement, ...]

    @property
    def checksum(self) -> str:
        payload = {
            "version": self.version,
            "name": self.name,
            "statements": [
                {"sql": statement.sql.strip(), "parameters": statement.parameters}
                for statement in self.statements
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        return hashlib.sha256(encoded).hexdigest()


class MigrationError(RuntimeError):
    """Raised when the database cannot be safely brought to the expected schema."""


def _registry_statements() -> tuple[Statement, ...]:
    insert = """
        INSERT INTO entity_schema_registry (
            entity_type, schema_version, storage_kind, domain, owner_scope,
            api_exposure, lifecycle, description
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """
    return tuple(
        Statement(
            insert,
            (
                schema.name,
                schema.schema_version,
                schema.storage_kind,
                schema.domain,
                schema.owner_scope,
                schema.api_exposure,
                schema.lifecycle,
                schema.description,
            ),
        )
        for schema in BASELINE_ENTITY_SCHEMAS
    )


MIGRATIONS = (
    Migration(
        1,
        "legacy_json_store_baseline",
        (
            Statement(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            ),
            Statement(
                """
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_date TEXT NOT NULL,
                    updated_date TEXT NOT NULL
                )
                """
            ),
            Statement("CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type)"),
            Statement(
                "CREATE INDEX IF NOT EXISTS idx_entities_type_ts "
                "ON entities(type, json_extract(data, '$.timestamp'))"
            ),
            Statement(
                "CREATE INDEX IF NOT EXISTS idx_entities_type_date "
                "ON entities(type, json_extract(data, '$.date'))"
            ),
        ),
    ),
    Migration(
        2,
        "entity_schema_registry",
        (
            Statement(
                """
                CREATE TABLE entity_schema_registry (
                    entity_type TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL CHECK(schema_version > 0),
                    storage_kind TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    owner_scope TEXT NOT NULL,
                    api_exposure TEXT NOT NULL,
                    lifecycle TEXT NOT NULL,
                    description TEXT NOT NULL
                )
                """
            ),
            *_registry_statements(),
        ),
    ),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate_definitions(migrations: tuple[Migration, ...]) -> None:
    versions = [migration.version for migration in migrations]
    if versions != list(range(1, len(migrations) + 1)):
        raise MigrationError("migration versions must be contiguous, ordered, and start at 1")
    names = [migration.name for migration in migrations]
    if len(set(names)) != len(names):
        raise MigrationError("migration names must be unique")
    if any(not migration.statements for migration in migrations):
        raise MigrationError("every migration must contain at least one statement")


def _connect(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    uri = f"file:{quote(str(resolved), safe='/')}?mode=rwc"
    connection = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    deadline = time.monotonic() + 30
    while True:
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).lower() or time.monotonic() >= deadline:
                connection.close()
                raise MigrationError(f"could not configure SQLite WAL mode: {error}") from error
            time.sleep(0.05)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _validate_applied(
    applied: list[sqlite3.Row], migrations: tuple[Migration, ...]
) -> int:
    expected_versions = list(range(1, len(applied) + 1))
    actual_versions = [row["version"] for row in applied]
    if actual_versions != expected_versions:
        raise MigrationError(
            f"applied migration versions are not a contiguous prefix: {actual_versions}"
        )
    if len(applied) > len(migrations):
        raise MigrationError(
            "database schema is newer than this application; use a compatible release"
        )
    for row, migration in zip(applied, migrations):
        if row["name"] != migration.name:
            raise MigrationError(
                f"migration {migration.version} name drift: database={row['name']!r}, "
                f"application={migration.name!r}"
            )
        if row["checksum"] != migration.checksum:
            raise MigrationError(
                f"migration {migration.version} checksum drift for {migration.name!r}"
            )
    return len(applied)


def _validate_schema_registry(connection: sqlite3.Connection) -> None:
    actual = connection.execute(
        """
        SELECT entity_type, schema_version, storage_kind, domain, owner_scope,
               api_exposure, lifecycle, description
        FROM entity_schema_registry
        ORDER BY entity_type
        """
    ).fetchall()
    expected = sorted(
        (
            schema.name,
            schema.schema_version,
            schema.storage_kind,
            schema.domain,
            schema.owner_scope,
            schema.api_exposure,
            schema.lifecycle,
            schema.description,
        )
        for schema in ENTITY_SCHEMAS
    )
    if [tuple(row) for row in actual] != expected:
        raise MigrationError(
            "entity schema registry drift; add an ordered migration for registry changes"
        )


def run_migrations(
    path: Path = DB_PATH, migrations: Iterable[Migration] = MIGRATIONS
) -> list[int]:
    """Apply all pending migrations under one exclusive writer transaction.

    Returns the versions applied by this invocation. Any error rolls back all
    migrations attempted by this invocation and prevents application startup.
    """
    ordered = tuple(migrations)
    _validate_definitions(ordered)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = _connect(path)
    current: Migration | None = None
    applied_now: list[int] = []
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as error:
            raise MigrationError(f"could not acquire schema migration lock: {error}") from error
        connection.execute(TRACKING_TABLE_SQL)
        applied = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        applied_count = _validate_applied(applied, ordered)

        for current in ordered[applied_count:]:
            started = time.perf_counter()
            for statement in current.statements:
                connection.execute(statement.sql, statement.parameters)
            duration_ms = max(0, round((time.perf_counter() - started) * 1_000))
            connection.execute(
                """
                INSERT INTO schema_migrations (version, name, checksum, applied_at, duration_ms)
                VALUES (?, ?, ?, ?, ?)
                """,
                (current.version, current.name, current.checksum, _now_iso(), duration_ms),
            )
            applied_now.append(current.version)
        _validate_schema_registry(connection)
        connection.commit()
        return applied_now
    except MigrationError:
        connection.rollback()
        raise
    except sqlite3.Error as error:
        connection.rollback()
        label = f"migration {current.version} ({current.name})" if current else "migration bootstrap"
        raise MigrationError(f"{label} failed: {error}") from error
    finally:
        connection.close()
