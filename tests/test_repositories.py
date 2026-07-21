from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from server import db, health_summary, insights, insulin
from server.migrations import run_migrations
from server.repositories import (
    EvidenceRepository,
    GlucoseRepository,
    LabRepository,
    LegacyRepositoryCatalog,
    RelationshipRepository,
    TreatmentRepository,
    WearableRepository,
    get_repositories,
    use_repositories,
)
from server.unit_of_work import SqliteUnitOfWork, UnitOfWorkError


@pytest.fixture
def repository_database(tmp_path, monkeypatch):
    database = tmp_path / "app.sqlite3"
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    return database


def test_legacy_domain_repositories_preserve_entity_api_behavior(repository_database):
    repositories = LegacyRepositoryCatalog()
    assert isinstance(repositories.glucose, GlucoseRepository)
    assert isinstance(repositories.treatments, TreatmentRepository)
    assert isinstance(repositories.labs, LabRepository)
    assert isinstance(repositories.oura_daily, WearableRepository)
    assert isinstance(repositories.relationships, RelationshipRepository)
    assert isinstance(repositories.evidence, EvidenceRepository)

    older = repositories.glucose.create(
        {
            "owner_email": "owner",
            "timestamp": "2026-07-21T10:00:00Z",
            "value": 110,
        }
    )
    newer = repositories.glucose.create(
        {
            "owner_email": "owner",
            "timestamp": "2026-07-21T10:05:00Z",
            "value": 125,
        }
    )
    assert [
        row["id"]
        for row in repositories.glucose.query(
            {"owner_email": "owner", "timestamp": {"$gte": "2026-07-21T10:01:00Z"}},
            "-timestamp",
            10,
        )
    ] == [newer["id"]]
    assert repositories.glucose.get(older["id"])["value"] == 110
    assert repositories.glucose.update(older["id"], {"value": 111})["value"] == 111
    assert repositories.glucose.delete(newer["id"])
    assert repositories.glucose.delete_where({"owner_email": "owner"}) == 1


def test_unit_of_work_commits_multiple_domain_writes_together(repository_database):
    with SqliteUnitOfWork() as work:
        work.repositories.glucose.create({"owner_email": "owner", "timestamp": "2026-07-21T10:00:00Z", "value": 100})
        work.repositories.treatments.create(
            {
                "owner_email": "owner",
                "timestamp": "2026-07-21T10:00:00Z",
                "type": "insulin",
                "amount": 1,
            }
        )
        work.commit()

    repositories = LegacyRepositoryCatalog()
    assert len(repositories.glucose.query()) == 1
    assert len(repositories.treatments.query()) == 1


def test_unit_of_work_rolls_back_without_commit_and_after_commit_request_on_error(
    repository_database,
):
    with SqliteUnitOfWork() as work:
        work.repositories.glucose.create({"owner_email": "owner", "value": 100})

    assert LegacyRepositoryCatalog().glucose.query() == []

    with SqliteUnitOfWork() as work:
        work.repositories.glucose.create({"owner_email": "owner", "value": 102})
        work.rollback()
        work.commit()

    assert LegacyRepositoryCatalog().glucose.query() == []

    with pytest.raises(RuntimeError, match="reject operation"):
        with SqliteUnitOfWork() as work:
            work.repositories.glucose.create({"owner_email": "owner", "value": 101})
            work.commit()
            raise RuntimeError("reject operation")

    assert LegacyRepositoryCatalog().glucose.query() == []


def test_unit_of_work_is_atomic_across_entities_and_settings_tables(repository_database):
    with pytest.raises(RuntimeError, match="fail after both writes"):
        with SqliteUnitOfWork() as work:
            work.repositories.entity("HealthSummary").create({"owner_email": "owner", "generated_at": "now"})
            db.set_config_value(
                "health_summary_last_run",
                "now",
                connection=work.connection,
            )
            work.commit()
            raise RuntimeError("fail after both writes")

    with sqlite3.connect(repository_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM entities").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM app_settings").fetchone() == (0,)

    with SqliteUnitOfWork() as work:
        work.repositories.entity("HealthSummary").create({"owner_email": "owner", "generated_at": "now"})
        db.set_config_value(
            "health_summary_last_run",
            "now",
            connection=work.connection,
        )
        work.commit()

    with sqlite3.connect(repository_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM entities").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM app_settings").fetchone() == (1,)


def test_relationship_and_evidence_repositories_project_legacy_fields(repository_database):
    repositories = LegacyRepositoryCatalog()
    record = repositories.entity("MedicalRecord").create({"owner_email": "owner"})
    lab = repositories.labs.create({"owner_email": "owner", "record_id": record["id"], "test_name": "A1c"})
    pattern = repositories.entity("Pattern").create(
        {
            "owner_email": "owner",
            "supporting_evidence": '[{"reading_ids":["one","two"]}]',
        }
    )

    lab_edges = repositories.relationships.for_entity("owner", "LabResult", lab["id"])
    assert [(edge.predicate, edge.object_id) for edge in lab_edges] == [("extracted_from", record["id"])]
    record_edges = repositories.relationships.for_entity("owner", "MedicalRecord", record["id"])
    assert [(edge.predicate, edge.object_id) for edge in record_edges] == [("has_lab_result", lab["id"])]
    evidence = repositories.evidence.for_claim("owner", "Pattern", pattern["id"])
    assert len(evidence) == 1
    assert evidence[0].evidence_kind == "legacy_inline"
    assert evidence[0].value == {"reading_ids": ["one", "two"]}
    assert repositories.evidence.for_claim("other", "Pattern", pattern["id"]) == []


class InMemoryRepository:
    def __init__(self, entity_type, records=()):
        self.entity_type = entity_type
        self.records = [dict(record) for record in records]

    def query(self, filters=None, sort=None, limit=None, skip=0):
        rows = list(self.records)
        for key, value in (filters or {}).items():
            rows = [row for row in rows if row.get(key) == value]
        return rows[skip : skip + limit if limit else None]

    def get(self, entity_id):
        return next((row for row in self.records if row.get("id") == entity_id), None)

    def create(self, data):
        record = {**data, "id": str(len(self.records) + 1)}
        self.records.append(record)
        return record

    def create_many(self, records):
        return [self.create(record) for record in records]

    def update(self, entity_id, patch):
        record = self.get(entity_id)
        if record:
            record.update(patch)
        return record

    def delete(self, entity_id):
        before = len(self.records)
        self.records = [row for row in self.records if row.get("id") != entity_id]
        return len(self.records) != before

    def delete_where(self, filters):
        before = len(self.records)
        self.records = [row for row in self.records if not all(row.get(key) == value for key, value in filters.items())]
        return before - len(self.records)


class InMemoryCatalog:
    def __init__(self):
        self._entities = {
            "Treatment": InMemoryRepository(
                "Treatment",
                [
                    {
                        "owner_email": "owner@glucopilot.local",
                        "type": "insulin",
                        "event_type": "Daily Total",
                        "timestamp": "2026-07-21T00:00:00Z",
                        "notes": "Bolus: 4U | Basal: 20U | Total: 24U",
                    }
                ],
            )
        }
        empty = InMemoryRepository("empty")
        self.glucose = empty
        self.treatments = self._entities["Treatment"]
        self.labs = empty
        self.oura_daily = empty
        self.oura_heart_rate = empty
        self.fitbit_daily = empty
        self.fitbit_heart_rate = empty
        self.relationships = None
        self.evidence = None

    def entity(self, entity_type):
        return self._entities.setdefault(entity_type, InMemoryRepository(entity_type))


def test_core_read_can_swap_repository_implementation_without_sqlite():
    fake = InMemoryCatalog()
    with use_repositories(fake):
        assert insulin._daily_tdd() == {"2026-07-21": {"total": 24.0, "basal": 20.0, "bolus": 4.0}}
    assert get_repositories() is not fake


def test_named_core_modules_do_not_query_the_storage_module_directly():
    modules = (insulin, insights, health_summary)
    for module in modules:
        assert "db.query_entities" not in inspect.getsource(module)

    root = Path(__file__).parents[1] / "server"
    for filename in ("report.py", "companion.py"):
        assert "db.query_entities" not in (root / filename).read_text(encoding="utf-8")


def test_unit_of_work_rejects_lifecycle_calls_when_inactive():
    work = SqliteUnitOfWork()
    with pytest.raises(UnitOfWorkError, match="not active"):
        work.commit()
    with pytest.raises(UnitOfWorkError, match="not active"):
        _ = work.repositories
