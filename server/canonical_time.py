"""Canonical clinical-time sidecar for legacy JSON entities.

The JSON entity remains the compatibility record.  This module derives a
lossless, queryable timeline without inventing instants for partial, ambiguous,
nonexistent, or invalid source times.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import APP_TIMEZONE, DB_PATH
from .data_contracts import (
    DEPLOYMENT_OWNER_ID,
    ENTITY_CONTRACTS,
    DstResolution,
    TimeBasis,
    TimeMeaning,
    TimePrecision,
)


NORMALIZER_VERSION = "canonical-time/1.0.0"
_TRUE = {"1", "true", "yes", "on"}
_DATE_LENGTHS = {4: TimePrecision.YEAR, 7: TimePrecision.MONTH, 10: TimePrecision.DAY}
_CLINICAL_ROLES = {"observed", "effective_start", "effective_end"}


def canonical_time_enabled() -> bool:
    return os.getenv("CANONICAL_TIME_ENABLED", "false").strip().lower() in _TRUE


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _offset_text(value: timedelta | None) -> str | None:
    if value is None:
        return None
    minutes = int(value.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, remainder = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{remainder:02d}"


def _role(meaning: TimeMeaning) -> str:
    return {
        TimeMeaning.OBSERVED: "observed",
        TimeMeaning.RECORDED: "recorded",
        TimeMeaning.RECEIVED: "received",
        TimeMeaning.EFFECTIVE: "effective_start",
    }[meaning]


def _basis(entity_type: str, entity: dict[str, Any], role: str) -> TimeBasis:
    if role in {"received", "recorded"}:
        return TimeBasis.EXACT
    source = str(entity.get("source") or "").lower()
    inferred_markers = (
        entity.get("inferred"),
        entity.get("time_inferred"),
        entity.get("inferred_time"),
        entity.get("date_inferred"),
    )
    if (
        any(value is True for value in inferred_markers)
        or str(entity.get("time_basis")) == "inferred"
        or "inferred" in source
    ):
        return TimeBasis.INFERRED
    contract = ENTITY_CONTRACTS[entity_type]
    source_classes = {item.value for item in contract.source_classes}
    if source in {"manual", "patient", "self_reported"} or source_classes == {"patient"}:
        return TimeBasis.PATIENT_REPORTED
    return TimeBasis.SOURCE_REPORTED


def _detect_precision(text: str) -> TimePrecision:
    if len(text) in _DATE_LENGTHS and text[:4].isdigit():
        try:
            if len(text) == 10:
                datetime.strptime(text, "%Y-%m-%d")
            elif len(text) == 7:
                datetime.strptime(text, "%Y-%m")
            return _DATE_LENGTHS[len(text)]
        except ValueError:
            return TimePrecision.UNKNOWN
    if "T" not in text and " " not in text:
        return TimePrecision.UNKNOWN
    time_text = text.split("T", 1)[-1] if "T" in text else text.split(" ", 1)[-1]
    time_text = time_text.removesuffix("Z")
    time_text = time_text.split("+", 1)[0]
    if time_text.count("-") > 0:
        time_text = time_text.rsplit("-", 1)[0]
    pieces = time_text.split(":")
    if len(pieces) == 1:
        return TimePrecision.HOUR
    if len(pieces) == 2:
        return TimePrecision.MINUTE
    return TimePrecision.SECOND


def _local_candidates(value: datetime, zone: ZoneInfo) -> list[tuple[datetime, timedelta | None]]:
    candidates: list[tuple[datetime, timedelta | None]] = []
    for fold in (0, 1):
        aware = value.replace(tzinfo=zone, fold=fold)
        as_utc = aware.astimezone(timezone.utc)
        if as_utc.astimezone(zone).replace(tzinfo=None) == value:
            candidate = (as_utc, aware.utcoffset())
            if candidate not in candidates:
                candidates.append(candidate)
    return sorted(candidates, key=lambda item: item[0])


def _base_row(
    entity_type: str,
    entity: dict[str, Any],
    field_name: str,
    role: str,
    source_text: str,
    basis: TimeBasis,
) -> dict[str, Any]:
    return {
        "owner_id": DEPLOYMENT_OWNER_ID,
        "entity_type": entity_type,
        "entity_id": str(entity["id"]),
        "field_name": field_name,
        "role": role,
        "source_text": source_text,
        "normalized_value": None,
        "canonical_at": None,
        "local_date": None,
        "timezone": None,
        "utc_offset": None,
        "precision": TimePrecision.UNKNOWN.value,
        "basis": basis.value,
        "dst_resolution": DstResolution.NOT_APPLICABLE.value,
        "normalization_status": "invalid",
        "inferred": int(basis == TimeBasis.INFERRED),
        "duration_seconds": None,
        "normalizer_version": NORMALIZER_VERSION,
    }


def normalize_time_value(
    entity_type: str,
    entity: dict[str, Any],
    field_name: str,
    role: str,
    value: Any,
    *,
    default_timezone: str,
) -> dict[str, Any]:
    """Normalize one source value without manufacturing an uncertain instant."""
    source_text = str(value).strip()
    basis = _basis(entity_type, entity, role)
    row = _base_row(entity_type, entity, field_name, role, source_text, basis)
    precision = _detect_precision(source_text)
    row["precision"] = precision.value

    if precision in {TimePrecision.DAY, TimePrecision.MONTH, TimePrecision.YEAR}:
        row["normalized_value"] = source_text
        row["local_date"] = source_text if precision == TimePrecision.DAY else None
        row["timezone"] = str(entity.get("timezone") or entity.get("time_zone") or default_timezone)
        row["normalization_status"] = "partial"
        return row
    if precision == TimePrecision.UNKNOWN:
        row["basis"] = TimeBasis.UNKNOWN.value
        row["inferred"] = 0
        return row

    try:
        parsed = datetime.fromisoformat(source_text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        row["basis"] = TimeBasis.UNKNOWN.value
        row["inferred"] = 0
        return row

    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        row["canonical_at"] = _utc_iso(parsed)
        row["normalized_value"] = row["canonical_at"]
        row["local_date"] = parsed.date().isoformat()
        row["utc_offset"] = _offset_text(parsed.utcoffset())
        named_zone = entity.get("timezone") or entity.get("time_zone")
        row["timezone"] = str(named_zone) if named_zone else (
            "UTC" if parsed.utcoffset() == timedelta(0) else None
        )
        row["dst_resolution"] = DstResolution.UNAMBIGUOUS.value
        row["normalization_status"] = "resolved"
        return row

    zone_name = str(entity.get("timezone") or entity.get("time_zone") or default_timezone)
    row["timezone"] = zone_name
    row["local_date"] = parsed.date().isoformat()
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        row["basis"] = TimeBasis.UNKNOWN.value
        row["inferred"] = 0
        return row
    candidates = _local_candidates(parsed, zone)
    if not candidates:
        row["dst_resolution"] = DstResolution.NONEXISTENT_LOCAL_TIME.value
        row["normalization_status"] = "nonexistent"
        return row
    if len(candidates) == 1:
        chosen = candidates[0]
        row["dst_resolution"] = DstResolution.UNAMBIGUOUS.value
    else:
        requested = str(entity.get("dst_resolution") or "")
        requested_offset = str(entity.get("utc_offset") or "")
        choices = {
            DstResolution.AMBIGUOUS_EARLIER_OFFSET.value: candidates[0],
            DstResolution.AMBIGUOUS_LATER_OFFSET.value: candidates[-1],
        }
        chosen = choices.get(requested)
        if chosen is None and requested_offset:
            chosen = next((item for item in candidates if _offset_text(item[1]) == requested_offset), None)
        if chosen is None:
            row["dst_resolution"] = DstResolution.UNRESOLVED.value
            row["normalization_status"] = "ambiguous"
            return row
        row["dst_resolution"] = (
            DstResolution.AMBIGUOUS_EARLIER_OFFSET.value
            if chosen == candidates[0]
            else DstResolution.AMBIGUOUS_LATER_OFFSET.value
        )
    row["canonical_at"] = _utc_iso(chosen[0])
    row["normalized_value"] = row["canonical_at"]
    row["utc_offset"] = _offset_text(chosen[1])
    row["normalization_status"] = "resolved"
    return row


def normalize_entity_times(
    entity_type: str,
    entity: dict[str, Any],
    *,
    default_timezone: str = APP_TIMEZONE,
) -> list[dict[str, Any]]:
    contract = ENTITY_CONTRACTS.get(entity_type)
    if contract is None or not entity.get("id"):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for field in contract.time_fields:
        value = entity.get(field.field)
        if value in (None, ""):
            continue
        role = _role(field.meaning)
        rows.append(
            normalize_time_value(
                entity_type,
                entity,
                field.field,
                role,
                value,
                default_timezone=default_timezone,
            )
        )
        seen.add((field.field, role))

    for field_name, role in (("effective_start", "effective_start"), ("effective_end", "effective_end")):
        value = entity.get(field_name)
        if value not in (None, "") and (field_name, role) not in seen:
            rows.append(
                normalize_time_value(
                    entity_type,
                    entity,
                    field_name,
                    role,
                    value,
                    default_timezone=default_timezone,
                )
            )

    start = next((row for row in rows if row["role"] == "effective_start"), None)
    duration = entity.get("duration")
    if start and start["canonical_at"] and duration not in (None, "") and not any(
        row["role"] == "effective_end" for row in rows
    ):
        try:
            duration_seconds = float(duration) * 60
            if duration_seconds >= 0:
                end_at = datetime.fromisoformat(start["canonical_at"].replace("Z", "+00:00")) + timedelta(
                    seconds=duration_seconds
                )
                end = dict(start)
                end.update(
                    field_name="duration",
                    role="effective_end",
                    source_text=start["source_text"],
                    normalized_value=_utc_iso(end_at),
                    canonical_at=_utc_iso(end_at),
                    local_date=end_at.astimezone(ZoneInfo(start["timezone"] or "UTC")).date().isoformat(),
                    duration_seconds=duration_seconds,
                )
                rows.append(end)
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            pass
    return rows


_COLUMNS = (
    "owner_id", "entity_type", "entity_id", "timeline_role", "timeline_at", "observed_at",
    "recorded_at", "received_at", "effective_start", "effective_end", "event_field",
    "source_text", "recorded_source_text", "received_source_text", "normalized_value",
    "local_date", "timezone", "utc_offset", "precision", "basis", "dst_resolution",
    "normalization_status", "inferred", "duration_seconds", "additional_times_json",
    "normalizer_version", "created_at", "updated_at",
)


def _compact_time_row(
    entity_type: str,
    entity: dict[str, Any],
    times: list[dict[str, Any]],
) -> dict[str, Any]:
    received = next((item for item in times if item["field_name"] == "created_date"), None)
    recorded = next((item for item in times if item["field_name"] == "updated_date"), None)
    event = next((item for item in times if item["role"] == "observed"), None)
    if event is None:
        event = next((item for item in times if item["role"] == "effective_start"), None)
    effective_end = next((item for item in times if item["role"] == "effective_end"), None)
    selected_ids = {id(item) for item in (received, recorded, event, effective_end) if item}
    extra_keys = (
        "field_name", "role", "source_text", "normalized_value", "canonical_at", "local_date",
        "timezone", "utc_offset", "precision", "basis", "dst_resolution",
        "normalization_status", "inferred", "duration_seconds",
    )
    additional = [
        {key: item[key] for key in extra_keys}
        for item in times
        if id(item) not in selected_ids
    ]
    metadata = event or {
        "field_name": None,
        "source_text": None,
        "normalized_value": None,
        "canonical_at": None,
        "local_date": None,
        "timezone": None,
        "utc_offset": None,
        "precision": TimePrecision.UNKNOWN.value,
        "basis": TimeBasis.UNKNOWN.value,
        "dst_resolution": DstResolution.NOT_APPLICABLE.value,
        "normalization_status": "not_applicable",
        "inferred": 0,
        "duration_seconds": None,
    }
    observed_at = metadata["canonical_at"] if event and event["role"] == "observed" else None
    effective_start = (
        metadata["canonical_at"] if event and event["role"] == "effective_start" else None
    )
    return {
        "owner_id": DEPLOYMENT_OWNER_ID,
        "entity_type": entity_type,
        "entity_id": str(entity["id"]),
        "timeline_role": event["role"] if event else None,
        "timeline_at": metadata["canonical_at"],
        "observed_at": observed_at,
        "recorded_at": recorded["canonical_at"] if recorded else None,
        "received_at": received["canonical_at"] if received else None,
        "effective_start": effective_start,
        "effective_end": effective_end["canonical_at"] if effective_end else None,
        "event_field": metadata["field_name"],
        "source_text": metadata["source_text"],
        "recorded_source_text": recorded["source_text"] if recorded else None,
        "received_source_text": received["source_text"] if received else None,
        "normalized_value": metadata["normalized_value"],
        "local_date": metadata["local_date"],
        "timezone": metadata["timezone"],
        "utc_offset": metadata["utc_offset"],
        "precision": metadata["precision"],
        "basis": metadata["basis"],
        "dst_resolution": metadata["dst_resolution"],
        "normalization_status": metadata["normalization_status"],
        "inferred": metadata["inferred"],
        "duration_seconds": effective_end["duration_seconds"] if effective_end else None,
        "additional_times_json": json.dumps(additional, sort_keys=True, separators=(",", ":")),
        "normalizer_version": NORMALIZER_VERSION,
    }


def _default_timezone(connection: sqlite3.Connection) -> str:
    row = connection.execute("SELECT value FROM app_settings WHERE key='cfg_app_timezone'").fetchone()
    return str(row[0]) if row and row[0] else APP_TIMEZONE


class SqliteClinicalTimeRepository:
    def __init__(self, connection: sqlite3.Connection | None = None) -> None:
        self._connection = connection

    @contextmanager
    def _scope(self):
        if self._connection is not None:
            yield self._connection
            return
        from .db import connect

        connection = connect()
        try:
            yield connection
        finally:
            connection.close()

    def sync_entity(self, entity_type: str, entity: dict[str, Any]) -> list[dict[str, Any]]:
        return self.sync_entities(entity_type, [entity])

    def sync_entities(
        self,
        entity_type: str,
        entities: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        with self._scope() as connection:
            default_timezone = _default_timezone(connection)
            rows = [
                _compact_time_row(entity_type, entity, normalize_entity_times(
                    entity_type,
                    entity,
                    default_timezone=default_timezone,
                ))
                for entity in entities
            ]
            now = _now_iso()
            connection.executemany(
                "DELETE FROM canonical_times WHERE entity_id=?",
                [
                    (entity["id"],)
                    for entity in entities
                ],
            )
            if rows:
                placeholders = ",".join("?" for _ in _COLUMNS)
                for row in rows:
                    row["created_at"] = now
                    row["updated_at"] = now
                connection.executemany(
                    f"INSERT INTO canonical_times ({','.join(_COLUMNS)}) VALUES ({placeholders})",
                    [tuple(row[column] for column in _COLUMNS) for row in rows],
                )
            if self._connection is None:
                connection.commit()
            return rows

    def for_entity(self, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
        with self._scope() as connection:
            rows = connection.execute(
                "SELECT * FROM canonical_times WHERE owner_id=? AND entity_type=? AND entity_id=? "
                "LIMIT 1",
                (DEPLOYMENT_OWNER_ID, entity_type, entity_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def timeline(
        self,
        start: str,
        end: str,
        *,
        entity_types: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        try:
            start = _utc_iso(datetime.fromisoformat(start.replace("Z", "+00:00")))
            end = _utc_iso(datetime.fromisoformat(end.replace("Z", "+00:00")))
        except (TypeError, ValueError) as error:
            raise ValueError("timeline boundaries must be ISO 8601 instants") from error
        where = ["owner_id=?", "timeline_at>=?", "timeline_at<=?"]
        parameters: list[Any] = [DEPLOYMENT_OWNER_ID, start, end]
        if entity_types:
            where.append(f"entity_type IN ({','.join('?' for _ in entity_types)})")
            parameters.extend(entity_types)
        with self._scope() as connection:
            rows = connection.execute(
                f"SELECT * FROM canonical_times WHERE {' AND '.join(where)} "
                "ORDER BY timeline_at, entity_type, entity_id",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]


def record_entity_times(
    entity_type: str,
    entities: list[dict[str, Any]],
    *,
    connection: sqlite3.Connection,
) -> None:
    if not canonical_time_enabled():
        return
    repository = SqliteClinicalTimeRepository(connection)
    repository.sync_entities(entity_type, entities)


def temporal_metadata(
    entity_type: str,
    entity: dict[str, Any],
    *,
    default_timezone: str = APP_TIMEZONE,
) -> dict[str, dict[str, Any]]:
    """Compatibility projection used by reports even before sidecar rollout."""
    projected: dict[str, dict[str, Any]] = {}
    for row in normalize_entity_times(entity_type, entity, default_timezone=default_timezone):
        value = {key: row[key] for key in (
            "role", "source_text", "normalized_value", "canonical_at", "local_date", "timezone",
            "utc_offset", "precision", "basis", "dst_resolution", "normalization_status", "inferred",
            "duration_seconds", "normalizer_version",
        )}
        projected[row["role"]] = value
    return projected


def backfill_canonical_times(database: Path = DB_PATH, *, batch_size: int = 1000) -> dict[str, int]:
    if not canonical_time_enabled():
        raise RuntimeError("CANONICAL_TIME_ENABLED must be true for backfill")
    from .migrations import run_migrations

    run_migrations(database)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    repository = SqliteClinicalTimeRepository(connection)
    scanned = written = 0
    last_rowid = 0
    try:
        while True:
            rows = connection.execute(
                "SELECT rowid, * FROM entities WHERE rowid>? ORDER BY rowid LIMIT ?",
                (last_rowid, batch_size),
            ).fetchall()
            if not rows:
                break
            by_type: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                entity = json.loads(row["data"])
                entity.update(id=row["id"], created_date=row["created_date"], updated_date=row["updated_date"])
                by_type.setdefault(row["type"], []).append(entity)
                scanned += 1
                last_rowid = row["rowid"]
            for entity_type, entities in by_type.items():
                written += len(repository.sync_entities(entity_type, entities))
            connection.commit()
    finally:
        connection.close()
    return {"entities_scanned": scanned, "time_rows_written": written}


def _main() -> None:
    parser = argparse.ArgumentParser(description="Backfill canonical clinical-time metadata")
    parser.add_argument("backfill", nargs="?")
    parser.add_argument("--database", type=Path, default=DB_PATH)
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()
    if args.backfill != "backfill":
        parser.error("expected: backfill")
    print(json.dumps(backfill_canonical_times(args.database, batch_size=max(1, args.batch_size))))


if __name__ == "__main__":
    _main()
