from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from server.migrations import MIGRATIONS, pending_migration_versions, run_migrations
from server.schema_registry import ENTITY_SCHEMAS

pytestmark = pytest.mark.risk_critical

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "migrations"
MIGRATION_VERSIONS = [migration.version for migration in MIGRATIONS]


def _load_snapshot(name: str) -> str:
    sql = (FIXTURE_DIR / name).read_text(encoding="utf-8")
    return sql.replace("{{MIGRATION_1_CHECKSUM}}", MIGRATIONS[0].checksum)


def _schema_signature(database: Path) -> tuple[dict[str, list[tuple]], dict[str, list[tuple]]]:
    with sqlite3.connect(database) as connection:
        table_names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        tables = {name: connection.execute(f'PRAGMA table_info("{name}")').fetchall() for name in table_names}
        index_names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        indexes = {name: connection.execute(f'PRAGMA index_xinfo("{name}")').fetchall() for name in index_names}
    return tables, indexes


@pytest.mark.parametrize(
    ("snapshot", "pending", "applied", "entity_id"),
    [
        (
            "pre_registry_v0.sql",
            MIGRATION_VERSIONS,
            MIGRATION_VERSIONS,
            "synthetic-pre-registry-row",
        ),
        (
            "tracked_baseline_v1.sql",
            MIGRATION_VERSIONS[1:],
            MIGRATION_VERSIONS[1:],
            "synthetic-tracked-baseline-row",
        ),
    ],
)
def test_prior_release_schema_snapshots_upgrade_without_data_loss(
    tmp_path,
    snapshot,
    pending,
    applied,
    entity_id,
):
    database = tmp_path / "prior-release.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(_load_snapshot(snapshot))

    assert pending_migration_versions(database) == pending
    assert run_migrations(database) == applied
    assert pending_migration_versions(database) == []

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT id FROM entities WHERE id=?",
            (entity_id,),
        ).fetchone() == (entity_id,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT COUNT(*) FROM entity_schema_registry").fetchone() == (len(ENTITY_SCHEMAS),)

    clean = tmp_path / "clean.sqlite3"
    assert run_migrations(clean) == MIGRATION_VERSIONS
    assert _schema_signature(database) == _schema_signature(clean)


def test_migration_snapshots_are_manifestly_synthetic_and_contain_no_credentials():
    for snapshot in sorted(FIXTURE_DIR.glob("*.sql")):
        sql = snapshot.read_text(encoding="utf-8")
        assert "SYNTHETIC FIXTURE ONLY" in sql
        assert "owner@glucopilot.local" in sql
        assert "password" not in sql.lower()
        assert "access_token" not in sql.lower()
        assert "refresh_token" not in sql.lower()
