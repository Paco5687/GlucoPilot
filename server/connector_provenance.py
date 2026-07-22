"""Feature-flagged connector run lifecycle and normalized evidence links.

Connector code records raw provider responses at fetch boundaries. The active
run context observes legacy entity creates/updates without changing their JSON
shape, then links those normalized rows to an immutable manifest of the raw
source records. No context is installed while the feature flag is disabled.
"""

from __future__ import annotations

import os
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, TypeVar

from .source_archive import SqliteSourceArchiveRepository, source_archive_enabled

T = TypeVar("T")

CONNECTOR_VERSIONS = {
    "dexcom": "dexcom-v3-parser-1",
    "dexcom_share": "dexcom-share-parser-1",
    "nightscout": "nightscout-parser-1",
    "tandem": "tconnectsync-parser-1",
    "glooko": "glooko-v2-parser-1",
    "fitbit": "fitbit-web-parser-1",
    "google_health": "google-health-v4-parser-1",
    "oura": "oura-v2-parser-1",
    "medical_record_upload": "medical-record-parser-1",
    "cycle_ingest": "cycle-ingest-parser-1",
}

LINKABLE_ENTITY_TYPES = {
    "dexcom": {"GlucoseReading", "Treatment"},
    "dexcom_share": {"GlucoseReading"},
    "nightscout": {"GlucoseReading", "Treatment", "NightscoutProfile"},
    "tandem": {"Treatment"},
    "glooko": {"GlucoseReading", "Treatment"},
    "fitbit": {"FitbitDaily"},
    "google_health": {"FitbitDaily", "FitbitHeartRate"},
    "oura": {"OuraDaily", "OuraHeartRate"},
    "medical_record_upload": {"MedicalRecord", "LabResult"},
    "cycle_ingest": {"PeriodLog"},
}


def connector_provenance_enabled() -> bool:
    requested = os.getenv("CONNECTOR_PROVENANCE_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return requested and source_archive_enabled()


def _utc(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def latest_observed(records: list[dict[str, Any]], *keys: str) -> str | None:
    latest = None
    for record in records:
        if not isinstance(record, dict):
            continue
        for key in keys:
            value = record.get(key)
            if isinstance(value, (int, float)) and value > 10_000_000_000:
                value = datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
            elif isinstance(value, str) and len(value) == 10 and value[4:5] == "-":
                value += "T00:00:00Z"
            normalized = _utc(str(value)) if value is not None else None
            if normalized and (latest is None or normalized > latest):
                latest = normalized
    return latest


def latest_payload_observed(payload: Any) -> str | None:
    """Best-effort latest clinical timestamp from nested provider JSON."""
    candidates: list[str] = []
    time_keys = {
        "timestamp",
        "datetime",
        "datetimeutc",
        "datestring",
        "dateofsleep",
        "day",
        "date",
        "systemtime",
        "displaytime",
        "sampletime",
        "starttime",
        "endtime",
        "physicaltime",
        "created_at",
        "pumptimestamp",
        "devicetimestamp",
    }
    normalized_time_keys = {item.replace("_", "") for item in time_keys}

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            if key.lower() == "date" and {"year", "month", "day"} <= value.keys():
                try:
                    candidates.append(
                        f"{int(value['year']):04d}-{int(value['month']):02d}-{int(value['day']):02d}T00:00:00Z"
                    )
                except (TypeError, ValueError):
                    pass
            for child_key, child in value.items():
                walk(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        elif key.lower().replace("_", "") in normalized_time_keys:
            if isinstance(value, (int, float)) and value > 10_000_000_000:
                candidates.append(datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat())
            elif isinstance(value, str):
                candidates.append(value + "T00:00:00Z" if len(value) == 10 else value)

    walk(payload)
    normalized = [_utc(candidate) for candidate in candidates]
    return max((value for value in normalized if value), default=None)


@dataclass
class ConnectorRunContext:
    source_type: str
    parser_version: str
    run_id: str
    repository: SqliteSourceArchiveRepository
    linkable_types: set[str]
    fetched_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    stale_count: int = 0
    writes: list[tuple[str, str, str]] = field(default_factory=list)
    source_record_ids: list[str] = field(default_factory=list)
    source_file_ids: list[str] = field(default_factory=list)
    latest_data_at: str | None = None
    errors: list[str] = field(default_factory=list)

    def capture_payload(
        self,
        payload: Any,
        *,
        external_id: str | None = None,
        observed_at: str | None = None,
        fetched_count: int = 1,
    ) -> str:
        record, _ = self.repository.archive_payload(
            self.source_type,
            payload,
            self.parser_version,
            external_id=external_id,
            observed_at=observed_at,
            sync_run_id=self.run_id,
        )
        if record["id"] not in self.source_record_ids:
            self.source_record_ids.append(record["id"])
        self.fetched_count += max(0, int(fetched_count))
        self.observe(observed_at)
        return record["id"]

    def capture_file(
        self,
        relative_path: str,
        file_hash: str,
        byte_size: int,
        *,
        external_id: str | None = None,
        observed_at: str | None = None,
        mime_type: str | None = None,
    ) -> str:
        source_file, _ = self.repository.register_file(
            self.source_type,
            relative_path,
            file_hash,
            byte_size,
            self.parser_version,
            external_id=external_id,
            observed_at=observed_at,
            mime_type=mime_type,
            sync_run_id=self.run_id,
        )
        if source_file["id"] not in self.source_file_ids:
            self.source_file_ids.append(source_file["id"])
        self.fetched_count += 1
        self.observe(observed_at)
        return source_file["id"]

    def observe(self, value: str | None) -> None:
        normalized = _utc(value)
        if normalized and (self.latest_data_at is None or normalized > self.latest_data_at):
            self.latest_data_at = normalized

    def failure(self, error: Any, count: int = 1) -> None:
        self.failed_count += max(1, int(count))
        self.note_error(error)

    def note_error(self, error: Any) -> None:
        message = str(error).strip()
        if message:
            self.errors.append(message[:300])

    def record_writes(self, operation: str, entity_type: str, entity_ids: list[str]) -> None:
        if entity_type not in self.linkable_types:
            return
        self.writes.extend((operation, entity_type, entity_id) for entity_id in entity_ids)

    def absorb_result(self, result: Any) -> None:
        if not isinstance(result, dict):
            return
        for key, value in result.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                continue
            lowered = key.lower()
            if lowered == "skipped" or lowered.endswith("_skipped"):
                self.skipped_count += value
            elif lowered == "stale" or lowered.endswith("_stale"):
                self.stale_count += value
            elif lowered == "failed" or lowered.endswith("_failed"):
                self.failed_count += value
        try:
            response_status = int(result.get("_status") or 200)
        except (TypeError, ValueError):
            response_status = 500
        if result.get("error") or response_status >= 400:
            error = result.get("error") or f"connector returned status {result.get('_status')}"
            if self.failed_count:
                self.note_error(error)
            else:
                self.failure(error)

    def link_writes(self) -> None:
        unique_writes = list(dict.fromkeys((entity_type, entity_id) for _, entity_type, entity_id in self.writes))
        if unique_writes and not (self.source_record_ids or self.source_file_ids):
            self.failure("normalized writes completed without captured source evidence")
            return

        manifest_id = None
        if self.source_record_ids:
            manifest, _ = self.repository.archive_payload(
                f"{self.source_type}-manifest",
                {
                    "source_type": self.source_type,
                    "parser_version": self.parser_version,
                    "source_record_ids": self.source_record_ids,
                },
                self.parser_version,
                external_id=self.run_id,
                observed_at=self.latest_data_at,
                sync_run_id=self.run_id,
            )
            manifest_id = manifest["id"]

        for entity_type, entity_id in unique_writes:
            if manifest_id:
                self.repository.link_entity(
                    entity_type,
                    entity_id,
                    self.run_id,
                    self.parser_version,
                    source_record_id=manifest_id,
                )
            for source_file_id in self.source_file_ids:
                self.repository.link_entity(
                    entity_type,
                    entity_id,
                    self.run_id,
                    self.parser_version,
                    source_file_id=source_file_id,
                )

    @property
    def created_count(self) -> int:
        return sum(operation == "created" for operation, _, _ in self.writes)

    @property
    def updated_count(self) -> int:
        return sum(operation == "updated" for operation, _, _ in self.writes)


_CURRENT_RUN: ContextVar[ConnectorRunContext | None] = ContextVar("connector_provenance_run", default=None)


def current_run() -> ConnectorRunContext | None:
    return _CURRENT_RUN.get()


def record_entity_writes(operation: str, entity_type: str, entity_ids: list[str]) -> None:
    run = current_run()
    if run:
        run.record_writes(operation, entity_type, entity_ids)


def capture_payload(
    payload: Any,
    *,
    external_id: str | None = None,
    observed_at: str | None = None,
    fetched_count: int = 1,
) -> str | None:
    run = current_run()
    return (
        run.capture_payload(
            payload,
            external_id=external_id,
            observed_at=observed_at,
            fetched_count=fetched_count,
        )
        if run
        else None
    )


def capture_records(
    records: list[Any],
    *,
    external_id: str,
    observed_at: str | None = None,
    metadata: dict[str, Any] | None = None,
    chunk_size: int = 250,
) -> list[str]:
    """Archive bounded record chunks so provider backfills respect size policy."""
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    chunks = [records[index : index + chunk_size] for index in range(0, len(records), chunk_size)] or [[]]
    archived = []
    for index, chunk in enumerate(chunks):
        record_id = capture_payload(
            {
                **(metadata or {}),
                "chunk_index": index,
                "chunk_count": len(chunks),
                "records": chunk,
            },
            external_id=f"{external_id}#chunk-{index + 1}-of-{len(chunks)}",
            observed_at=observed_at or latest_payload_observed(chunk),
            fetched_count=len(chunk),
        )
        if record_id:
            archived.append(record_id)
    return archived


def capture_file(
    relative_path: str,
    file_hash: str,
    byte_size: int,
    *,
    external_id: str | None = None,
    observed_at: str | None = None,
    mime_type: str | None = None,
) -> str | None:
    run = current_run()
    return (
        run.capture_file(
            relative_path,
            file_hash,
            byte_size,
            external_id=external_id,
            observed_at=observed_at,
            mime_type=mime_type,
        )
        if run
        else None
    )


def source_failure(error: Any, count: int = 1) -> None:
    run = current_run()
    if run:
        run.failure(error, count)


def can_advance_freshness() -> bool:
    run = current_run()
    return run is None or run.failed_count == 0


async def run_connector(
    source_type: str,
    action: str,
    operation: Callable[[], Awaitable[T]],
    *,
    trigger_type: str = "manual",
    run_kind: str = "connector",
) -> T:
    """Run one sync/upload with complete outcome recording when enabled."""
    if not connector_provenance_enabled():
        return await operation()
    if source_type not in CONNECTOR_VERSIONS:
        raise ValueError(f"unsupported provenance source: {source_type}")

    parser_version = CONNECTOR_VERSIONS[source_type]
    if action == "backfill":
        trigger_type = "backfill"
    repository = SqliteSourceArchiveRepository()
    run = repository.start_sync_run(
        source_type,
        parser_version,
        run_kind=run_kind,
        trigger_type=trigger_type,
        connector_version=parser_version,
    )
    context = ConnectorRunContext(
        source_type,
        parser_version,
        run["id"],
        repository,
        LINKABLE_ENTITY_TYPES[source_type],
    )
    token: Token[ConnectorRunContext | None] = _CURRENT_RUN.set(context)
    result: T
    raised: BaseException | None = None
    try:
        result = await operation()
        context.absorb_result(result)
    except BaseException as error:
        raised = error
        if context.failed_count:
            context.note_error(error)
        else:
            context.failure(error)
        raise
    finally:
        try:
            try:
                context.link_writes()
            except Exception as link_error:
                context.failure(f"provenance linking failed: {link_error}")
            has_progress = bool(
                context.writes
                or context.source_record_ids
                or context.source_file_ids
                or context.fetched_count
            )
            status = "succeeded"
            if context.failed_count:
                status = "partial" if has_progress else "failed"
            completed = repository.finish_sync_run(
                run["id"],
                status,
                error_summary="; ".join(context.errors) or None,
                fetched_count=context.fetched_count,
                created_count=context.created_count,
                updated_count=context.updated_count,
                skipped_count=context.skipped_count,
                failed_count=context.failed_count,
                stale_count=context.stale_count,
                last_successful_data_at=context.latest_data_at,
            )
            if raised is None and isinstance(result, dict):
                result["provenance_run_id"] = completed["id"]
                result["provenance_status"] = completed["status"]
        finally:
            _CURRENT_RUN.reset(token)
    return result
