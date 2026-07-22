"""Swappable domain repositories over the legacy JSON entity store.

Core modules depend on these interfaces, not on SQLite or the JSON table. The
legacy implementations intentionally preserve existing query and mutation
semantics while future typed repositories can implement the same contracts.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from collections.abc import Iterator

from . import db


Entity = dict[str, Any]
EntityFilters = dict[str, Any]


@runtime_checkable
class EntityRepository(Protocol):
    entity_type: str

    def query(
        self,
        filters: EntityFilters | None = None,
        sort: str | None = None,
        limit: int | None = None,
        skip: int = 0,
    ) -> list[Entity]: ...

    def get(self, entity_id: str) -> Entity | None: ...

    def create(self, data: Entity) -> Entity: ...

    def create_many(self, records: list[Entity]) -> list[Entity]: ...

    def update(self, entity_id: str, patch: Entity) -> Entity | None: ...

    def delete(self, entity_id: str) -> bool: ...

    def delete_where(self, filters: EntityFilters) -> int: ...


@runtime_checkable
class GlucoseRepository(EntityRepository, Protocol):
    """Continuous-glucose observations."""


@runtime_checkable
class TreatmentRepository(EntityRepository, Protocol):
    """Insulin, carbohydrate, basal, and pump events."""


@runtime_checkable
class LabRepository(EntityRepository, Protocol):
    """Document-derived or clinician-entered lab observations."""


@runtime_checkable
class WearableRepository(EntityRepository, Protocol):
    """Provider-day or intraday wearable observations."""


@dataclass(frozen=True)
class RelationshipEdge:
    subject_type: str
    subject_id: str
    predicate: str
    object_type: str
    object_id: str


@runtime_checkable
class RelationshipRepository(Protocol):
    def for_entity(
        self,
        owner_email: str,
        entity_type: str,
        entity_id: str,
    ) -> list[RelationshipEdge]: ...


@dataclass(frozen=True)
class EvidenceReference:
    claim_type: str
    claim_id: str
    evidence_kind: str
    locator: str
    value: Any


@runtime_checkable
class EvidenceRepository(Protocol):
    def for_claim(
        self,
        owner_email: str,
        claim_type: str,
        claim_id: str,
    ) -> list[EvidenceReference]: ...


@runtime_checkable
class SourceArchiveRepository(Protocol):
    """Immutable raw payloads, file references, and sync-run metadata."""

    def start_sync_run(
        self,
        source_type: str,
        parser_version: str,
        *,
        started_at: str | None = None,
        run_kind: str = "archive",
        trigger_type: str = "unknown",
        connector_version: str = "legacy",
    ) -> dict[str, Any]: ...

    def finish_sync_run(
        self,
        run_id: str,
        status: str,
        *,
        completed_at: str | None = None,
        error_summary: str | None = None,
        fetched_count: int = 0,
        created_count: int = 0,
        updated_count: int = 0,
        skipped_count: int = 0,
        failed_count: int = 0,
        stale_count: int = 0,
        last_successful_data_at: str | None = None,
    ) -> dict[str, Any]: ...

    def archive_payload(
        self,
        source_type: str,
        payload: Any,
        parser_version: str,
        **metadata: Any,
    ) -> tuple[dict[str, Any], bool]: ...

    def register_file(
        self,
        source_type: str,
        relative_path: str,
        file_hash: str,
        byte_size: int,
        parser_version: str,
        **metadata: Any,
    ) -> tuple[dict[str, Any], bool]: ...

    def link_entity(
        self,
        entity_type: str,
        entity_id: str,
        sync_run_id: str,
        parser_version: str,
        *,
        source_record_id: str | None = None,
        source_file_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]: ...

    def links_for_entity(self, entity_type: str, entity_id: str) -> list[dict[str, Any]]: ...

    def recent_sync_runs(
        self,
        source_type: str | None = None,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]: ...

    def read_payload(self, record_id: str) -> Any: ...

    def stats(self) -> dict[str, Any]: ...

    def prune_before(self, cutoff: str) -> dict[str, int]: ...


@runtime_checkable
class ClinicalTimeRepository(Protocol):
    """Canonical event, effective, recorded, and ingestion-time sidecar."""

    def sync_entity(self, entity_type: str, entity: Entity) -> list[dict[str, Any]]: ...

    def sync_entities(self, entity_type: str, entities: list[Entity]) -> list[dict[str, Any]]: ...

    def for_entity(self, entity_type: str, entity_id: str) -> list[dict[str, Any]]: ...

    def timeline(
        self,
        start: str,
        end: str,
        *,
        entity_types: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class BasalSegmentRepository(Protocol):
    """Strict pump basal and suspension intervals."""

    def for_treatment(self, entity_id: str) -> dict[str, Any] | None: ...


@runtime_checkable
class PumpDailyTotalRepository(Protocol):
    """Authoritative pump total projections parsed from Treatment records."""

    def for_treatment(self, entity_id: str) -> dict[str, Any] | None: ...

    def by_date(self, start: str, end: str) -> list[dict[str, Any]]: ...


@runtime_checkable
class RepositoryCatalog(Protocol):
    glucose: GlucoseRepository
    treatments: TreatmentRepository
    labs: LabRepository
    oura_daily: WearableRepository
    oura_heart_rate: WearableRepository
    fitbit_daily: WearableRepository
    fitbit_heart_rate: WearableRepository
    relationships: RelationshipRepository
    evidence: EvidenceRepository
    source_archive: SourceArchiveRepository
    clinical_time: ClinicalTimeRepository
    basal_segments: BasalSegmentRepository
    pump_daily_totals: PumpDailyTotalRepository

    def entity(self, entity_type: str) -> EntityRepository: ...


class LegacyJsonEntityRepository:
    """Compatibility adapter for one type in the legacy JSON entity table."""

    def __init__(
        self,
        entity_type: str,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self.entity_type = entity_type
        self._connection = connection

    def query(
        self,
        filters: EntityFilters | None = None,
        sort: str | None = None,
        limit: int | None = None,
        skip: int = 0,
    ) -> list[Entity]:
        return db.query_entities(
            self.entity_type,
            filters,
            sort,
            limit,
            skip,
            connection=self._connection,
        )

    def get(self, entity_id: str) -> Entity | None:
        rows = self.query({"id": entity_id}, limit=1)
        return rows[0] if rows else None

    def create(self, data: Entity) -> Entity:
        return db.create_entity(
            self.entity_type,
            data,
            connection=self._connection,
        )

    def create_many(self, records: list[Entity]) -> list[Entity]:
        return db.bulk_create_entities(
            self.entity_type,
            records,
            connection=self._connection,
        )

    def update(self, entity_id: str, patch: Entity) -> Entity | None:
        return db.update_entity(
            self.entity_type,
            entity_id,
            patch,
            connection=self._connection,
        )

    def delete(self, entity_id: str) -> bool:
        return db.delete_entity(
            self.entity_type,
            entity_id,
            connection=self._connection,
        )

    def delete_where(self, filters: EntityFilters) -> int:
        return db.delete_entities_where(
            self.entity_type,
            filters,
            connection=self._connection,
        )


class LegacyRelationshipRepository:
    """Project current parent references as relationship edges without writes."""

    def __init__(self, catalog: LegacyRepositoryCatalog) -> None:
        self._catalog = catalog

    def for_entity(
        self,
        owner_email: str,
        entity_type: str,
        entity_id: str,
    ) -> list[RelationshipEdge]:
        edges: list[RelationshipEdge] = []
        if entity_type == "LabResult":
            lab = self._catalog.labs.get(entity_id)
            if lab and lab.get("owner_email") == owner_email and lab.get("record_id"):
                edges.append(
                    RelationshipEdge(
                        "LabResult",
                        entity_id,
                        "extracted_from",
                        "MedicalRecord",
                        str(lab["record_id"]),
                    )
                )
        elif entity_type == "MedicalRecord":
            for lab in self._catalog.labs.query({"owner_email": owner_email, "record_id": entity_id}):
                edges.append(
                    RelationshipEdge(
                        "MedicalRecord",
                        entity_id,
                        "has_lab_result",
                        "LabResult",
                        lab["id"],
                    )
                )
        elif entity_type == "ChatMessage":
            message = self._catalog.entity("ChatMessage").get(entity_id)
            if message and message.get("owner_email") == owner_email and message.get("thread_id"):
                edges.append(
                    RelationshipEdge(
                        "ChatMessage",
                        entity_id,
                        "member_of_thread",
                        "CompanionThread",
                        str(message["thread_id"]),
                    )
                )
        elif entity_type == "CompanionThread":
            for message in self._catalog.entity("ChatMessage").query(
                {"owner_email": owner_email, "thread_id": entity_id}
            ):
                edges.append(
                    RelationshipEdge(
                        "CompanionThread",
                        entity_id,
                        "has_message",
                        "ChatMessage",
                        message["id"],
                    )
                )
        return edges


class LegacyEvidenceRepository:
    """Expose existing inline claim support through an evidence interface."""

    _FIELDS = {
        "Pattern": ("supporting_evidence", "legacy_inline"),
        "Insight": ("supporting_data", "legacy_inline"),
        "ChatMessage": ("sources", "external_source"),
    }

    def __init__(self, catalog: LegacyRepositoryCatalog) -> None:
        self._catalog = catalog

    def for_claim(
        self,
        owner_email: str,
        claim_type: str,
        claim_id: str,
    ) -> list[EvidenceReference]:
        mapping = self._FIELDS.get(claim_type)
        if not mapping:
            return []
        claim = self._catalog.entity(claim_type).get(claim_id)
        if not claim or claim.get("owner_email") != owner_email:
            return []
        field, kind = mapping
        value = claim.get(field)
        if value in (None, "", [], {}):
            return []
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                pass
        values = value if isinstance(value, list) else [value]
        return [
            EvidenceReference(
                claim_type,
                claim_id,
                kind,
                f"{claim_type}.{field}[{index}]",
                item,
            )
            for index, item in enumerate(values)
        ]


class LegacyRepositoryCatalog:
    """Repository bundle backed by the current JSON entity store."""

    def __init__(self, connection: sqlite3.Connection | None = None) -> None:
        self._connection = connection
        self._entities: dict[str, LegacyJsonEntityRepository] = {}
        self.glucose = self.entity("GlucoseReading")
        legacy_treatments = self.entity("Treatment")
        self.labs = self.entity("LabResult")
        self.oura_daily = self.entity("OuraDaily")
        self.oura_heart_rate = self.entity("OuraHeartRate")
        self.fitbit_daily = self.entity("FitbitDaily")
        self.fitbit_heart_rate = self.entity("FitbitHeartRate")
        self.relationships = LegacyRelationshipRepository(self)
        self.evidence = LegacyEvidenceRepository(self)
        from .source_archive import SqliteSourceArchiveRepository
        from .canonical_time import SqliteClinicalTimeRepository
        from .typed_treatments import (
            SqliteBasalSegmentRepository,
            SqlitePumpDailyTotalRepository,
            SqliteTypedTreatmentRepository,
            TreatmentCompatibilityRepository,
        )

        self.source_archive = SqliteSourceArchiveRepository(connection)
        self.clinical_time = SqliteClinicalTimeRepository(connection)
        self.typed_treatments = SqliteTypedTreatmentRepository(connection)
        self.basal_segments = SqliteBasalSegmentRepository(connection)
        self.pump_daily_totals = SqlitePumpDailyTotalRepository(connection)
        self.treatments = TreatmentCompatibilityRepository(
            legacy_treatments,
            self.typed_treatments,
        )

    def entity(self, entity_type: str) -> LegacyJsonEntityRepository:
        if entity_type not in self._entities:
            self._entities[entity_type] = LegacyJsonEntityRepository(
                entity_type,
                self._connection,
            )
        return self._entities[entity_type]


_DEFAULT_REPOSITORIES = LegacyRepositoryCatalog()
_REPOSITORY_OVERRIDE: ContextVar[RepositoryCatalog | None] = ContextVar(
    "glucopilot_repository_override",
    default=None,
)


def get_repositories() -> RepositoryCatalog:
    return _REPOSITORY_OVERRIDE.get() or _DEFAULT_REPOSITORIES


@contextmanager
def use_repositories(repositories: RepositoryCatalog) -> Iterator[RepositoryCatalog]:
    """Temporarily inject repositories for a test or isolated operation."""
    token = _REPOSITORY_OVERRIDE.set(repositories)
    try:
        yield repositories
    finally:
        _REPOSITORY_OVERRIDE.reset(token)
