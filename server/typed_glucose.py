"""Strict typed projections and compatibility repositories for glucose data.

Legacy JSON remains authoritative. Feature-gated hooks maintain rebuildable
typed rows in the same transaction; shadow reads compare value-free parity;
and an independent read flag permits an immediate compatibility rollback.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import logging
import math
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import db
from .canonical_time import normalize_entity_times
from .config import APP_TIMEZONE, DB_PATH, OWNER_EMAIL
from .data_contracts import (
    DEPLOYMENT_OWNER_ID,
    canonical_entity_id,
    canonical_source_record_id,
)


MAPPING_VERSION = "typed-glucose/1.0.0"
READING_TOLERANCE_SECONDS = 240
_TRUE = {"1", "true", "yes", "on"}
log = logging.getLogger("glucopilot.typed_glucose")

_GLUCOSE_COLUMNS = (
    "entity_id",
    "canonical_id",
    "owner_id",
    "owner_email",
    "source",
    "source_record_id",
    "source_record_canonical_id",
    "observed_at",
    "source_timestamp",
    "local_date",
    "value_mg_dl",
    "trend",
    "assertion_kind",
    "source_class",
    "legacy_fingerprint",
    "mapping_version",
    "received_at",
    "recorded_at",
    "created_at",
    "updated_at",
)
_FINGERSTICK_COLUMNS = (
    "entity_id",
    "canonical_id",
    "owner_id",
    "owner_email",
    "observed_at",
    "source_timestamp",
    "local_date",
    "value_mg_dl",
    "source",
    "note",
    "paired_glucose_entity_id",
    "paired_glucose_value_mg_dl",
    "paired_glucose_source_timestamp",
    "paired_glucose_observed_at",
    "paired_glucose_source",
    "paired_delta_mg_dl",
    "assertion_kind",
    "source_class",
    "legacy_fingerprint",
    "mapping_version",
    "received_at",
    "recorded_at",
    "created_at",
    "updated_at",
)


class GlucoseMappingError(ValueError):
    """A legacy glucose row cannot truthfully satisfy the strict schema."""


@dataclass(frozen=True)
class TypedGlucoseProjection:
    table: str
    row: dict[str, Any]


def typed_glucose_writes_enabled() -> bool:
    return os.getenv("TYPED_GLUCOSE_WRITES_ENABLED", "false").strip().lower() in _TRUE


def typed_glucose_shadow_reads_enabled() -> bool:
    return os.getenv("TYPED_GLUCOSE_SHADOW_READS_ENABLED", "false").strip().lower() in _TRUE


def typed_glucose_reads_enabled() -> bool:
    return os.getenv("TYPED_GLUCOSE_READS_ENABLED", "false").strip().lower() in _TRUE


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _number(value: Any, field: str, minimum: float, maximum: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise GlucoseMappingError(f"{field} is not numeric") from error
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise GlucoseMappingError(f"{field} must be between {minimum:g} and {maximum:g}")
    return result


def _observed(entity_type: str, entity: dict[str, Any]) -> tuple[str, str]:
    rows = normalize_entity_times(entity_type, entity, default_timezone=APP_TIMEZONE)
    observed = next((row for row in rows if row["role"] == "observed"), None)
    if not observed or not observed.get("canonical_at"):
        raise GlucoseMappingError("timestamp is not an unambiguous instant")
    return str(observed["canonical_at"]), str(observed.get("local_date") or observed["canonical_at"][:10])


def _canonical_optional_instant(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise GlucoseMappingError("paired CGM timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GlucoseMappingError("paired CGM timestamp is not an unambiguous instant")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _envelope(entity: dict[str, Any], observed_at: str) -> tuple[str, str]:
    received = str(entity.get("created_date") or observed_at)
    recorded = str(entity.get("updated_date") or received)
    if not received.endswith("Z"):
        received = observed_at
    if not recorded.endswith("Z"):
        recorded = received
    return received, recorded


def _fingerprint(row: dict[str, Any]) -> str:
    excluded = {"legacy_fingerprint", "created_at", "updated_at"}
    payload = {key: row[key] for key in sorted(row) if key not in excluded}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def map_legacy_glucose(entity: dict[str, Any]) -> TypedGlucoseProjection:
    entity_id = str(entity.get("id") or "").strip()
    if not entity_id:
        raise GlucoseMappingError("entity id is required")
    observed_at, local_date = _observed("GlucoseReading", entity)
    value = _number(entity.get("value"), "value", 20, 600)
    source = str(entity.get("source") or "legacy").strip().lower() or "legacy"
    source_record_id = str(entity.get("ns_id") or entity.get("record_id") or "").strip() or None
    received_at, recorded_at = _envelope(entity, observed_at)
    now = _now_iso()
    row = {
        "entity_id": entity_id,
        "canonical_id": canonical_entity_id("GlucoseReading", entity_id),
        "owner_id": DEPLOYMENT_OWNER_ID,
        "owner_email": str(entity.get("owner_email") or OWNER_EMAIL).strip() or OWNER_EMAIL,
        "source": source,
        "source_record_id": source_record_id,
        "source_record_canonical_id": canonical_source_record_id(source, source_record_id)
        if source_record_id
        else None,
        "observed_at": observed_at,
        "source_timestamp": str(entity.get("timestamp")),
        "local_date": local_date,
        "value_mg_dl": value,
        "trend": str(entity.get("trend")) if entity.get("trend") not in (None, "") else None,
        "assertion_kind": "source_fact",
        "source_class": "import" if source in {"csv", "import", "base44", "demo", "legacy"} else "device_provider",
        "legacy_fingerprint": "",
        "mapping_version": MAPPING_VERSION,
        "received_at": received_at,
        "recorded_at": recorded_at,
        "created_at": now,
        "updated_at": now,
    }
    row["legacy_fingerprint"] = _fingerprint(row)
    return TypedGlucoseProjection("glucose_readings", row)


def map_legacy_fingerstick(entity: dict[str, Any]) -> TypedGlucoseProjection:
    entity_id = str(entity.get("id") or "").strip()
    if not entity_id:
        raise GlucoseMappingError("entity id is required")
    observed_at, local_date = _observed("FingerstickReading", entity)
    value = _number(entity.get("value"), "value", 10, 800)
    paired_value = None
    if entity.get("cgm_value") not in (None, ""):
        paired_value = _number(entity.get("cgm_value"), "cgm_value", 10, 800)
    delta = None
    if entity.get("delta") not in (None, ""):
        delta = float(entity["delta"])
        if not math.isfinite(delta) or paired_value is None:
            raise GlucoseMappingError("delta requires a finite paired CGM value")
        if abs((paired_value - value) - delta) > 0.11:
            raise GlucoseMappingError("delta does not match paired CGM minus fingerstick")
    received_at, recorded_at = _envelope(entity, observed_at)
    now = _now_iso()
    row = {
        "entity_id": entity_id,
        "canonical_id": canonical_entity_id("FingerstickReading", entity_id),
        "owner_id": DEPLOYMENT_OWNER_ID,
        "owner_email": str(entity.get("owner_email") or OWNER_EMAIL).strip() or OWNER_EMAIL,
        "observed_at": observed_at,
        "source_timestamp": str(entity.get("timestamp")),
        "local_date": local_date,
        "value_mg_dl": value,
        "source": str(entity.get("source") or "manual").strip().lower() or "manual",
        "note": str(entity.get("note") or ""),
        "paired_glucose_entity_id": str(entity.get("cgm_reading_id") or "").strip() or None,
        "paired_glucose_value_mg_dl": paired_value,
        "paired_glucose_source_timestamp": str(entity.get("cgm_timestamp"))
        if entity.get("cgm_timestamp") not in (None, "")
        else None,
        "paired_glucose_observed_at": _canonical_optional_instant(entity.get("cgm_timestamp")),
        "paired_glucose_source": str(entity.get("cgm_source") or "").strip() or None,
        "paired_delta_mg_dl": delta,
        "assertion_kind": "patient_report",
        "source_class": "patient",
        "legacy_fingerprint": "",
        "mapping_version": MAPPING_VERSION,
        "received_at": received_at,
        "recorded_at": recorded_at,
        "created_at": now,
        "updated_at": now,
    }
    row["legacy_fingerprint"] = _fingerprint(row)
    return TypedGlucoseProjection("fingerstick_readings", row)


def map_legacy(entity_type: str, entity: dict[str, Any]) -> TypedGlucoseProjection:
    if entity_type == "GlucoseReading":
        return map_legacy_glucose(entity)
    if entity_type == "FingerstickReading":
        return map_legacy_fingerstick(entity)
    raise GlucoseMappingError(f"unsupported entity type: {entity_type}")


@contextmanager
def _scope(
    connection: sqlite3.Connection | None,
    database: Path | None = None,
) -> Iterator[tuple[sqlite3.Connection, bool]]:
    if connection is not None:
        yield connection, False
        return
    if database is None:
        opened = db.connect()
    else:
        opened = sqlite3.connect(database)
        opened.execute("PRAGMA foreign_keys=ON")
    opened.row_factory = sqlite3.Row
    try:
        yield opened, True
    finally:
        opened.close()


def _upsert(connection: sqlite3.Connection, projection: TypedGlucoseProjection) -> None:
    columns = _GLUCOSE_COLUMNS if projection.table == "glucose_readings" else _FINGERSTICK_COLUMNS
    updates = [column for column in columns[1:] if column != "created_at"]
    connection.execute(
        f"INSERT INTO {projection.table} ({','.join(columns)}) "
        f"VALUES ({','.join('?' for _ in columns)}) ON CONFLICT(entity_id) DO UPDATE SET "
        + ",".join(f"{column}=excluded.{column}" for column in updates),
        tuple(projection.row[column] for column in columns),
    )


def _query_value(field: str, value: Any) -> Any:
    if field not in {"timestamp", "cgm_timestamp"} or not isinstance(value, str):
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return value
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _typed_query(
    connection: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
    filters: dict[str, Any] | None,
    sort: str | None,
    limit: int | None,
    skip: int,
) -> list[sqlite3.Row]:
    # Typed projections are deployment-owner scoped. Keeping the stable owner
    # identity in every query both enforces that boundary and makes the
    # owner/time indexes serve the compatibility repository's common paths.
    where: list[str] = ["owner_id = ?"]
    parameters: list[Any] = [DEPLOYMENT_OWNER_ID]
    for key, value in (filters or {}).items():
        column = columns[key]
        if isinstance(value, dict) and any(str(operator).startswith("$") for operator in value):
            for operator, operand in value.items():
                if operator == "$in":
                    if not isinstance(operand, list) or not operand:
                        raise ValueError(f"$in requires a non-empty list for {key}")
                    where.append(f"{column} IN ({','.join('?' for _ in operand)})")
                    parameters.extend(_query_value(key, item) for item in operand)
                else:
                    sql_operator = {
                        "$gte": ">=", "$gt": ">", "$lte": "<=", "$lt": "<", "$ne": "!="
                    }.get(operator)
                    if not sql_operator:
                        raise ValueError(f"Unsupported filter operator: {operator}")
                    where.append(f"{column} {sql_operator} ?")
                    parameters.append(_query_value(key, operand))
        elif value is None:
            where.append(f"{column} IS NULL")
        else:
            where.append(f"{column} = ?")
            parameters.append(_query_value(key, value))
    selected_sort = (sort or "-created_date").strip()
    direction = "DESC" if selected_sort.startswith("-") else "ASC"
    sql = f"SELECT * FROM {table}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {columns[selected_sort.lstrip('-')]} {direction}, entity_id {direction} LIMIT ? OFFSET ?"
    parameters.extend([int(limit) if limit else -1, int(skip or 0)])
    return connection.execute(sql, parameters).fetchall()


def _glucose_legacy_shape(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    result = {
        "id": row["entity_id"],
        "owner_email": row["owner_email"],
        "timestamp": row["source_timestamp"],
        "value": row["value_mg_dl"],
        "source": row["source"],
        "created_date": row["received_at"],
        "updated_date": row["recorded_at"],
    }
    if row["trend"] is not None:
        result["trend"] = row["trend"]
    if row["source_record_id"] is not None:
        result["ns_id"] = row["source_record_id"]
    return result


def _fingerstick_legacy_shape(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    result = {
        "id": row["entity_id"],
        "owner_email": row["owner_email"],
        "timestamp": row["source_timestamp"],
        "value": row["value_mg_dl"],
        "source": row["source"],
        "note": row["note"],
        "created_date": row["received_at"],
        "updated_date": row["recorded_at"],
    }
    optional = {
        "paired_glucose_entity_id": "cgm_reading_id",
        "paired_glucose_value_mg_dl": "cgm_value",
        "paired_glucose_source_timestamp": "cgm_timestamp",
        "paired_glucose_source": "cgm_source",
        "paired_delta_mg_dl": "delta",
    }
    for source, target in optional.items():
        if row[source] is not None:
            result[target] = row[source]
    return result


class SqliteTypedGlucoseRepository:
    entity_type = "GlucoseReading"
    table = "glucose_readings"
    _columns = {
        "id": "entity_id",
        "owner_email": "owner_email",
        "timestamp": "observed_at",
        "value": "value_mg_dl",
        "trend": "trend",
        "source": "source",
        "ns_id": "source_record_id",
        "created_date": "received_at",
        "updated_date": "recorded_at",
    }

    def __init__(self, connection: sqlite3.Connection | None = None, *, database: Path | None = None):
        self._connection = connection
        self._database = database

    def sync_entities(self, entities: list[dict[str, Any]]) -> list[TypedGlucoseProjection | None]:
        results = []
        with _scope(self._connection, self._database) as (connection, owns_connection):
            for entity in entities:
                try:
                    projection = map_legacy_glucose(entity)
                except GlucoseMappingError:
                    if entity.get("id"):
                        connection.execute("DELETE FROM glucose_readings WHERE entity_id=?", (entity["id"],))
                    results.append(None)
                    continue
                _upsert(connection, projection)
                results.append(projection)
            if owns_connection:
                connection.commit()
        return results

    def get(self, entity_id: str) -> dict[str, Any] | None:
        with _scope(self._connection, self._database) as (connection, _):
            row = connection.execute("SELECT * FROM glucose_readings WHERE entity_id=?", (entity_id,)).fetchone()
        return _glucose_legacy_shape(row) if row else None

    def supports_query(self, filters=None, sort=None) -> bool:
        return set(filters or {}).issubset(self._columns) and (sort or "-created_date").lstrip("-") in self._columns

    def query(self, filters=None, sort=None, limit=None, skip=0):
        if not self.supports_query(filters, sort):
            raise ValueError("typed GlucoseReading query contains an unsupported compatibility field")
        with _scope(self._connection, self._database) as (connection, _):
            rows = _typed_query(connection, self.table, self._columns, filters, sort, limit, skip)
        return [_glucose_legacy_shape(row) for row in rows]


class SqliteTypedFingerstickRepository:
    entity_type = "FingerstickReading"
    table = "fingerstick_readings"
    _columns = {
        "id": "entity_id",
        "owner_email": "owner_email",
        "timestamp": "observed_at",
        "value": "value_mg_dl",
        "source": "source",
        "note": "note",
        "cgm_reading_id": "paired_glucose_entity_id",
        "cgm_value": "paired_glucose_value_mg_dl",
        "cgm_timestamp": "paired_glucose_observed_at",
        "cgm_source": "paired_glucose_source",
        "delta": "paired_delta_mg_dl",
        "created_date": "received_at",
        "updated_date": "recorded_at",
    }

    def __init__(self, connection: sqlite3.Connection | None = None, *, database: Path | None = None):
        self._connection = connection
        self._database = database

    def sync_entities(self, entities: list[dict[str, Any]]) -> list[TypedGlucoseProjection | None]:
        results = []
        with _scope(self._connection, self._database) as (connection, owns_connection):
            for entity in entities:
                try:
                    projection = map_legacy_fingerstick(entity)
                except GlucoseMappingError:
                    if entity.get("id"):
                        connection.execute("DELETE FROM fingerstick_readings WHERE entity_id=?", (entity["id"],))
                    results.append(None)
                    continue
                _upsert(connection, projection)
                results.append(projection)
            if owns_connection:
                connection.commit()
        return results

    def get(self, entity_id: str) -> dict[str, Any] | None:
        with _scope(self._connection, self._database) as (connection, _):
            row = connection.execute("SELECT * FROM fingerstick_readings WHERE entity_id=?", (entity_id,)).fetchone()
        return _fingerstick_legacy_shape(row) if row else None

    def supports_query(self, filters=None, sort=None) -> bool:
        return set(filters or {}).issubset(self._columns) and (sort or "-created_date").lstrip("-") in self._columns

    def query(self, filters=None, sort=None, limit=None, skip=0):
        if not self.supports_query(filters, sort):
            raise ValueError("typed FingerstickReading query contains an unsupported compatibility field")
        with _scope(self._connection, self._database) as (connection, _):
            rows = _typed_query(connection, self.table, self._columns, filters, sort, limit, skip)
        return [_fingerstick_legacy_shape(row) for row in rows]


def _normalized_result(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalized_result(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalized_result(item) for item in value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return value


def _result_checksum(rows: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        _normalized_result(rows),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def compare_query_results(legacy: list[dict[str, Any]], typed: list[dict[str, Any]]) -> dict[str, Any]:
    legacy_ids = [str(row.get("id")) for row in legacy]
    typed_ids = [str(row.get("id")) for row in typed]
    legacy_sum = sum(float(row.get("value") or 0) for row in legacy)
    typed_sum = sum(float(row.get("value") or 0) for row in typed)
    return {
        "legacy_count": len(legacy),
        "typed_count": len(typed),
        "count_match": len(legacy) == len(typed),
        "legacy_checksum": _result_checksum(legacy),
        "typed_checksum": _result_checksum(typed),
        "checksum_match": _result_checksum(legacy) == _result_checksum(typed),
        "ordering_match": legacy_ids == typed_ids,
        "aggregate_match": abs(legacy_sum - typed_sum) < 0.001,
    }


class GlucoseCompatibilityRepository:
    """Legacy authority with repository-owned dedup, shadowing, and typed rollback."""

    entity_type = "GlucoseReading"

    def __init__(self, legacy: Any, typed: SqliteTypedGlucoseRepository, connection=None):
        self._legacy = legacy
        self._typed = typed
        self._connection = connection

    def query(self, filters=None, sort=None, limit=None, skip=0):
        if not self._typed.supports_query(filters, sort):
            return self._legacy.query(filters, sort, limit, skip)
        if not (typed_glucose_reads_enabled() or typed_glucose_shadow_reads_enabled()):
            return self._legacy.query(filters, sort, limit, skip)
        if typed_glucose_reads_enabled() and not typed_glucose_shadow_reads_enabled():
            return self._typed.query(filters, sort, limit, skip)
        started = time.perf_counter()
        legacy = self._legacy.query(filters, sort, limit, skip)
        legacy_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        typed = self._typed.query(filters, sort, limit, skip)
        typed_ms = (time.perf_counter() - started) * 1000
        if typed_glucose_shadow_reads_enabled():
            comparison = compare_query_results(legacy, typed)
            log.info(
                "typed glucose shadow %s",
                json.dumps({**comparison, "legacy_ms": round(legacy_ms, 3), "typed_ms": round(typed_ms, 3)}, sort_keys=True),
            )
        return typed if typed_glucose_reads_enabled() else legacy

    def get(self, entity_id: str):
        if typed_glucose_reads_enabled():
            typed = self._typed.get(entity_id)
            if typed is not None:
                return typed
        return self._legacy.get(entity_id)

    def create(self, data):
        return self._legacy.create(data)

    def create_many(self, records):
        return self._legacy.create_many(records)

    def update(self, entity_id, patch):
        return self._legacy.update(entity_id, patch)

    def delete(self, entity_id):
        return self._legacy.delete(entity_id)

    def delete_where(self, filters):
        return self._legacy.delete_where(filters)

    def create_deduplicated(
        self,
        records: list[dict[str, Any]],
        *,
        tolerance_seconds: int = READING_TOLERANCE_SECONDS,
    ) -> tuple[list[dict[str, Any]], int]:
        parsed: list[tuple[float, int, dict[str, Any]]] = []
        skipped = 0
        for index, record in enumerate(records):
            epoch = _epoch(record.get("timestamp"))
            if epoch is None:
                skipped += 1
                continue
            parsed.append((epoch, index, {**record, "owner_email": record.get("owner_email") or OWNER_EMAIL}))
        parsed.sort(key=lambda item: (item[0], item[1]))

        owns_connection = self._connection is None
        connection = self._connection or db.connect()
        try:
            if owns_connection:
                connection.execute("BEGIN IMMEDIATE")
            existing = db.query_entities(
                "GlucoseReading",
                {"owner_email": OWNER_EMAIL},
                "timestamp",
                1000000,
                connection=connection,
            )
            stamps = sorted(epoch for row in existing if (epoch := _epoch(row.get("timestamp"))) is not None)
            accepted = []
            for epoch, _, record in parsed:
                position = bisect.bisect_left(stamps, epoch)
                if any(
                    0 <= candidate < len(stamps)
                    and abs(stamps[candidate] - epoch) <= tolerance_seconds
                    for candidate in (position - 1, position)
                ):
                    skipped += 1
                    continue
                accepted.append(record)
                bisect.insort(stamps, epoch)
            created = db.bulk_create_entities("GlucoseReading", accepted, connection=connection) if accepted else []
            if owns_connection:
                connection.commit()
            return created, skipped
        except Exception:
            if owns_connection:
                connection.rollback()
            raise
        finally:
            if owns_connection:
                connection.close()


class FingerstickCompatibilityRepository:
    entity_type = "FingerstickReading"

    def __init__(self, legacy: Any, typed: SqliteTypedFingerstickRepository):
        self._legacy = legacy
        self._typed = typed

    def query(self, filters=None, sort=None, limit=None, skip=0):
        if not self._typed.supports_query(filters, sort):
            return self._legacy.query(filters, sort, limit, skip)
        if not (typed_glucose_reads_enabled() or typed_glucose_shadow_reads_enabled()):
            return self._legacy.query(filters, sort, limit, skip)
        if typed_glucose_reads_enabled() and not typed_glucose_shadow_reads_enabled():
            return self._typed.query(filters, sort, limit, skip)
        legacy = self._legacy.query(filters, sort, limit, skip)
        typed = self._typed.query(filters, sort, limit, skip)
        if typed_glucose_shadow_reads_enabled():
            log.info("typed fingerstick shadow %s", json.dumps(compare_query_results(legacy, typed), sort_keys=True))
        return typed if typed_glucose_reads_enabled() else legacy

    def get(self, entity_id: str):
        if typed_glucose_reads_enabled():
            typed = self._typed.get(entity_id)
            if typed is not None:
                return typed
        return self._legacy.get(entity_id)

    def create(self, data):
        return self._legacy.create(data)

    def create_many(self, records):
        return self._legacy.create_many(records)

    def update(self, entity_id, patch):
        return self._legacy.update(entity_id, patch)

    def delete(self, entity_id):
        return self._legacy.delete(entity_id)

    def delete_where(self, filters):
        return self._legacy.delete_where(filters)


def _epoch(value: Any) -> float | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.timestamp()


def record_typed_glucose(
    entity_type: str,
    entities: list[dict[str, Any]],
    *,
    connection: sqlite3.Connection,
) -> None:
    if not typed_glucose_writes_enabled():
        return
    if entity_type == "GlucoseReading":
        SqliteTypedGlucoseRepository(connection).sync_entities(entities)
    elif entity_type == "FingerstickReading":
        SqliteTypedFingerstickRepository(connection).sync_entities(entities)


def backfill_typed_glucose(database: Path = DB_PATH, *, batch_size: int = 1000) -> dict[str, int]:
    if not typed_glucose_writes_enabled():
        raise RuntimeError("TYPED_GLUCOSE_WRITES_ENABLED must be true for backfill")
    from .migrations import run_migrations

    run_migrations(database)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    repositories = {
        "GlucoseReading": SqliteTypedGlucoseRepository(connection),
        "FingerstickReading": SqliteTypedFingerstickRepository(connection),
    }
    counts = {
        "glucose_legacy_scanned": 0,
        "glucose_typed_written": 0,
        "glucose_unmappable": 0,
        "fingerstick_legacy_scanned": 0,
        "fingerstick_typed_written": 0,
        "fingerstick_unmappable": 0,
    }
    try:
        for entity_type, repository in repositories.items():
            last_rowid = 0
            prefix = "glucose" if entity_type == "GlucoseReading" else "fingerstick"
            while True:
                rows = connection.execute(
                    "SELECT rowid,* FROM entities WHERE type=? AND rowid>? ORDER BY rowid LIMIT ?",
                    (entity_type, last_rowid, max(1, batch_size)),
                ).fetchall()
                if not rows:
                    break
                entities = []
                for row in rows:
                    entity = json.loads(row["data"])
                    entity.update(id=row["id"], created_date=row["created_date"], updated_date=row["updated_date"])
                    entities.append(entity)
                    last_rowid = row["rowid"]
                results = repository.sync_entities(entities)
                counts[f"{prefix}_legacy_scanned"] += len(results)
                counts[f"{prefix}_typed_written"] += sum(result is not None for result in results)
                counts[f"{prefix}_unmappable"] += sum(result is None for result in results)
                connection.commit()
    finally:
        connection.close()
    return counts


def _domain_comparison(connection: sqlite3.Connection, entity_type: str) -> dict[str, Any]:
    table = "glucose_readings" if entity_type == "GlucoseReading" else "fingerstick_readings"
    mapper = map_legacy_glucose if entity_type == "GlucoseReading" else map_legacy_fingerstick
    shape = _glucose_legacy_shape if entity_type == "GlucoseReading" else _fingerstick_legacy_shape
    expected: dict[str, str] = {}
    expected_rows = []
    unmappable = 0
    for row in connection.execute("SELECT * FROM entities WHERE type=?", (entity_type,)):
        entity = json.loads(row["data"])
        entity.update(id=row["id"], created_date=row["created_date"], updated_date=row["updated_date"])
        try:
            projection = mapper(entity)
        except GlucoseMappingError:
            unmappable += 1
            continue
        expected[row["id"]] = projection.row["legacy_fingerprint"]
        expected_rows.append((projection.row["observed_at"], projection.row["entity_id"], shape(projection.row)))
    actual: dict[str, str] = {}
    stored: dict[str, str] = {}
    actual_rows = []
    for row in connection.execute(f"SELECT * FROM {table}"):
        item = dict(row)
        actual[item["entity_id"]] = _fingerprint(item)
        stored[item["entity_id"]] = item["legacy_fingerprint"]
        actual_rows.append((item["observed_at"], item["entity_id"], shape(item)))
    missing = set(expected) - set(actual)
    extra = set(actual) - set(expected)
    drift = {entity_id for entity_id in actual if actual[entity_id] != stored[entity_id]}
    mismatched = {
        entity_id for entity_id in set(expected) & set(actual) if expected[entity_id] != actual[entity_id]
    } | (drift & set(expected))
    legacy_rows = [row for _, _, row in sorted(expected_rows)]
    typed_rows = [row for _, _, row in sorted(actual_rows)]
    return {
        "legacy_total": len(expected) + unmappable,
        "mappable": len(expected),
        "unmappable": unmappable,
        "typed_total": len(actual),
        "matched": len(set(expected) & set(actual)) - len(mismatched),
        "missing": len(missing),
        "mismatched": len(mismatched),
        "fingerprint_drift": len(drift),
        "extra": len(extra),
        "query": compare_query_results(legacy_rows, typed_rows),
    }


def compare_glucose_stores(database: Path = DB_PATH) -> dict[str, Any]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        glucose = _domain_comparison(connection, "GlucoseReading")
        fingersticks = _domain_comparison(connection, "FingerstickReading")
    finally:
        connection.close()
    return {"glucose": glucose, "fingersticks": fingersticks, "mapping_version": MAPPING_VERSION}


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("backfill", "compare"))
    parser.add_argument("--database", type=Path, default=DB_PATH)
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()
    result = (
        backfill_typed_glucose(args.database, batch_size=max(1, args.batch_size))
        if args.command == "backfill"
        else compare_glucose_stores(args.database)
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    _main()
