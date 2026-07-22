"""Strict typed wearable projections with reversible compatibility reads.

Legacy JSON remains authoritative. Daily provider observations and intraday
heart-rate samples can be projected transactionally, backfilled in bounded
batches, shadow-compared without logging clinical values, and selected behind
an independent read flag.
"""

from __future__ import annotations

import argparse
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
from datetime import date, datetime, timezone
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


MAPPING_VERSION = "typed-wearables/1.0.0"
DAILY_TYPES = ("OuraDaily", "FitbitDaily")
SAMPLE_TYPES = ("OuraHeartRate", "FitbitHeartRate")
WEARABLE_TYPES = DAILY_TYPES + SAMPLE_TYPES
_TRUE = {"1", "true", "yes", "on"}
log = logging.getLogger("glucopilot.typed_wearables")

# field -> inclusive bounds. These bounds reject impossible/non-finite values
# without narrowing ordinary provider ranges or inventing missing metrics.
DAILY_METRICS: dict[str, tuple[float, float]] = {
    "sleep_score": (0, 100),
    "sleep_total_seconds": (0, 172800),
    "sleep_efficiency": (0, 100),
    "sleep_rem_seconds": (0, 172800),
    "sleep_deep_seconds": (0, 172800),
    "sleep_latency_seconds": (0, 172800),
    "readiness_score": (0, 100),
    "readiness_temperature_deviation": (-20, 20),
    "readiness_hrv_balance": (0, 100),
    "activity_score": (0, 100),
    "activity_steps": (0, 1_000_000),
    "activity_calories": (0, 100_000),
    "activity_active_calories": (0, 100_000),
    "average_heart_rate": (1, 400),
    "lowest_heart_rate": (1, 400),
    "spo2_average": (0, 100),
    "steps": (0, 1_000_000),
    "calories_out": (0, 100_000),
    "active_minutes": (0, 2880),
    "resting_heart_rate": (1, 400),
    "sleep_minutes": (0, 2880),
    "spo2_avg": (0, 100),
    "spo2_min": (0, 100),
    "breathing_rate": (0, 100),
    "skin_temp_deviation": (-20, 20),
    "hrv": (0, 1000),
    "nonrem_heart_rate": (1, 400),
}

_DAILY_COLUMNS = (
    "entity_id",
    "canonical_id",
    "owner_id",
    "owner_email",
    "entity_type",
    "provider",
    "source_present",
    "source_record_id",
    "source_record_canonical_id",
    "observed_date",
    "present_fields_json",
    "compatibility_extra_json",
    *DAILY_METRICS,
    "assertion_kind",
    "source_class",
    "legacy_fingerprint",
    "mapping_version",
    "received_at",
    "recorded_at",
    "created_at",
    "updated_at",
)
_SAMPLE_COLUMNS = (
    "entity_id",
    "canonical_id",
    "owner_id",
    "owner_email",
    "entity_type",
    "provider",
    "source_present",
    "source_record_id",
    "source_record_canonical_id",
    "metric_kind",
    "observed_at",
    "source_timestamp",
    "local_date",
    "value",
    "unit",
    "compatibility_extra_json",
    "assertion_kind",
    "source_class",
    "legacy_fingerprint",
    "mapping_version",
    "received_at",
    "recorded_at",
    "created_at",
    "updated_at",
)

_ENVELOPE_FIELDS = {"id", "created_date", "updated_date"}
_DAILY_STANDARD_FIELDS = {"owner_email", "date", "source", *DAILY_METRICS}
_SAMPLE_STANDARD_FIELDS = {"owner_email", "timestamp", "bpm", "source"}


class WearableMappingError(ValueError):
    """A compatibility wearable row cannot truthfully satisfy the schema."""


@dataclass(frozen=True)
class TypedWearableProjection:
    table: str
    row: dict[str, Any]


def typed_wearable_writes_enabled() -> bool:
    return os.getenv("TYPED_WEARABLE_WRITES_ENABLED", "false").strip().lower() in _TRUE


def typed_wearable_shadow_reads_enabled() -> bool:
    return os.getenv("TYPED_WEARABLE_SHADOW_READS_ENABLED", "false").strip().lower() in _TRUE


def typed_wearable_reads_enabled() -> bool:
    return os.getenv("TYPED_WEARABLE_READS_ENABLED", "false").strip().lower() in _TRUE


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _number(value: Any, field: str, minimum: float, maximum: float) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise WearableMappingError(f"{field} is not numeric") from error
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise WearableMappingError(f"{field} must be between {minimum:g} and {maximum:g}")
    return result


def _envelope(entity: dict[str, Any], fallback: str) -> tuple[str, str]:
    received = str(entity.get("created_date") or fallback)
    recorded = str(entity.get("updated_date") or received)
    if not received.endswith("Z"):
        received = fallback
    if not recorded.endswith("Z"):
        recorded = received
    return received, recorded


def _provider(entity_type: str, entity: dict[str, Any]) -> tuple[str, int]:
    source = str(entity.get("source") or "").strip().lower()
    return (source or ("oura" if entity_type.startswith("Oura") else "fitbit"), int(bool(source)))


def _source_record_id(entity: dict[str, Any]) -> str | None:
    for field in ("record_id", "provider_id", "oura_id", "fitbit_id"):
        value = str(entity.get(field) or "").strip()
        if value:
            return value
    return None


def _fingerprint(row: dict[str, Any]) -> str:
    excluded = {"legacy_fingerprint", "created_at", "updated_at"}
    payload = {key: row[key] for key in sorted(row) if key not in excluded}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _extras(entity: dict[str, Any], standard: set[str]) -> str:
    extra = {
        key: value
        for key, value in entity.items()
        if key not in standard and key not in _ENVELOPE_FIELDS
    }
    return json.dumps(extra, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def map_legacy_daily(entity_type: str, entity: dict[str, Any]) -> TypedWearableProjection:
    if entity_type not in DAILY_TYPES:
        raise WearableMappingError(f"unsupported daily type: {entity_type}")
    entity_id = str(entity.get("id") or "").strip()
    if not entity_id:
        raise WearableMappingError("entity id is required")
    observed_date = str(entity.get("date") or "")
    try:
        if date.fromisoformat(observed_date).isoformat() != observed_date:
            raise ValueError
    except ValueError as error:
        raise WearableMappingError("date must be YYYY-MM-DD") from error
    provider, source_present = _provider(entity_type, entity)
    source_record_id = _source_record_id(entity)
    fallback = f"{observed_date}T00:00:00.000Z"
    received_at, recorded_at = _envelope(entity, fallback)
    now = _now_iso()
    row: dict[str, Any] = {
        "entity_id": entity_id,
        "canonical_id": canonical_entity_id(entity_type, entity_id),
        "owner_id": DEPLOYMENT_OWNER_ID,
        "owner_email": str(entity.get("owner_email") or OWNER_EMAIL).strip() or OWNER_EMAIL,
        "entity_type": entity_type,
        "provider": provider,
        "source_present": source_present,
        "source_record_id": source_record_id,
        "source_record_canonical_id": canonical_source_record_id(provider, source_record_id)
        if source_record_id
        else None,
        "observed_date": observed_date,
        "present_fields_json": json.dumps(
            sorted(field for field in DAILY_METRICS if field in entity), separators=(",", ":")
        ),
        "compatibility_extra_json": _extras(entity, _DAILY_STANDARD_FIELDS),
        "assertion_kind": "source_fact",
        "source_class": "import"
        if provider in {"csv", "import", "base44", "demo", "legacy"}
        else "device_provider",
        "legacy_fingerprint": "",
        "mapping_version": MAPPING_VERSION,
        "received_at": received_at,
        "recorded_at": recorded_at,
        "created_at": now,
        "updated_at": now,
    }
    for field, (minimum, maximum) in DAILY_METRICS.items():
        row[field] = _number(entity.get(field), field, minimum, maximum)
    row["legacy_fingerprint"] = _fingerprint(row)
    return TypedWearableProjection("wearable_daily", row)


def map_legacy_sample(entity_type: str, entity: dict[str, Any]) -> TypedWearableProjection:
    if entity_type not in SAMPLE_TYPES:
        raise WearableMappingError(f"unsupported sample type: {entity_type}")
    entity_id = str(entity.get("id") or "").strip()
    if not entity_id:
        raise WearableMappingError("entity id is required")
    normalized = normalize_entity_times(entity_type, entity, default_timezone=APP_TIMEZONE)
    observed = next((item for item in normalized if item["role"] == "observed"), None)
    if not observed or not observed.get("canonical_at"):
        raise WearableMappingError("timestamp is not an unambiguous instant")
    bpm = _number(entity.get("bpm"), "bpm", 1, 400)
    if bpm is None:
        raise WearableMappingError("bpm is required")
    provider, source_present = _provider(entity_type, entity)
    source_record_id = _source_record_id(entity)
    observed_at = str(observed["canonical_at"])
    received_at, recorded_at = _envelope(entity, observed_at)
    now = _now_iso()
    row = {
        "entity_id": entity_id,
        "canonical_id": canonical_entity_id(entity_type, entity_id),
        "owner_id": DEPLOYMENT_OWNER_ID,
        "owner_email": str(entity.get("owner_email") or OWNER_EMAIL).strip() or OWNER_EMAIL,
        "entity_type": entity_type,
        "provider": provider,
        "source_present": source_present,
        "source_record_id": source_record_id,
        "source_record_canonical_id": canonical_source_record_id(provider, source_record_id)
        if source_record_id
        else None,
        "metric_kind": "heart_rate",
        "observed_at": observed_at,
        "source_timestamp": str(entity.get("timestamp")),
        "local_date": str(observed.get("local_date") or observed_at[:10]),
        "value": bpm,
        "unit": "bpm",
        "compatibility_extra_json": _extras(entity, _SAMPLE_STANDARD_FIELDS),
        "assertion_kind": "source_fact",
        "source_class": "import"
        if provider in {"csv", "import", "base44", "demo", "legacy"}
        else "device_provider",
        "legacy_fingerprint": "",
        "mapping_version": MAPPING_VERSION,
        "received_at": received_at,
        "recorded_at": recorded_at,
        "created_at": now,
        "updated_at": now,
    }
    row["legacy_fingerprint"] = _fingerprint(row)
    return TypedWearableProjection("wearable_samples", row)


def map_legacy(entity_type: str, entity: dict[str, Any]) -> TypedWearableProjection:
    return (
        map_legacy_daily(entity_type, entity)
        if entity_type in DAILY_TYPES
        else map_legacy_sample(entity_type, entity)
    )


@contextmanager
def _scope(
    connection: sqlite3.Connection | None,
    database: Path | None = None,
) -> Iterator[tuple[sqlite3.Connection, bool]]:
    if connection is not None:
        yield connection, False
        return
    opened = db.connect() if database is None else sqlite3.connect(database)
    opened.row_factory = sqlite3.Row
    opened.execute("PRAGMA foreign_keys=ON")
    try:
        yield opened, True
    finally:
        opened.close()


def _upsert_many(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    projections: list[TypedWearableProjection],
) -> None:
    if not projections:
        return
    updates = [column for column in columns[1:] if column != "created_at"]
    connection.executemany(
        f"INSERT INTO {table} ({','.join(columns)}) "
        f"VALUES ({','.join('?' for _ in columns)}) ON CONFLICT(entity_id) DO UPDATE SET "
        + ",".join(f"{column}=excluded.{column}" for column in updates),
        [tuple(projection.row[column] for column in columns) for projection in projections],
    )


def _query_value(field: str, value: Any) -> Any:
    if field != "timestamp" or not isinstance(value, str):
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return value
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _typed_query(
    connection: sqlite3.Connection,
    table: str,
    entity_type: str,
    columns: dict[str, str],
    filters: dict[str, Any] | None,
    sort: str | None,
    limit: int | None,
    skip: int,
) -> list[sqlite3.Row]:
    where = ["owner_id=?", "entity_type=?"]
    parameters: list[Any] = [DEPLOYMENT_OWNER_ID, entity_type]
    for key, value in (filters or {}).items():
        column = columns[key]
        if key == "source" and value is not None and not isinstance(value, dict):
            where.extend(("source_present=1", "provider=?"))
            parameters.append(value)
            continue
        if isinstance(value, dict) and any(str(operator).startswith("$") for operator in value):
            for operator, operand in value.items():
                if operator == "$in":
                    if not isinstance(operand, list) or not operand:
                        raise ValueError(f"$in requires a non-empty list for {key}")
                    where.append(f"{column} IN ({','.join('?' for _ in operand)})")
                    parameters.extend(_query_value(key, item) for item in operand)
                else:
                    sql_operator = {
                        "$gte": ">=",
                        "$gt": ">",
                        "$lte": "<=",
                        "$lt": "<",
                        "$ne": "!=",
                    }.get(operator)
                    if not sql_operator:
                        raise ValueError(f"Unsupported filter operator: {operator}")
                    where.append(f"{column} {sql_operator} ?")
                    parameters.append(_query_value(key, operand))
        elif value is None:
            where.append(f"{column} IS NULL")
        else:
            where.append(f"{column}=?")
            parameters.append(_query_value(key, value))
    selected_sort = (sort or "-created_date").strip()
    direction = "DESC" if selected_sort.startswith("-") else "ASC"
    sql = f"SELECT * FROM {table} WHERE {' AND '.join(where)}"
    sql += f" ORDER BY {columns[selected_sort.lstrip('-')]} {direction}, entity_id {direction}"
    sql += " LIMIT ? OFFSET ?"
    parameters.extend([int(limit) if limit else -1, int(skip or 0)])
    return connection.execute(sql, parameters).fetchall()


def _daily_shape(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    result = json.loads(row["compatibility_extra_json"])
    result.update(
        id=row["entity_id"],
        owner_email=row["owner_email"],
        date=row["observed_date"],
        created_date=row["received_at"],
        updated_date=row["recorded_at"],
    )
    if row["source_present"]:
        result["source"] = row["provider"]
    present = set(json.loads(row["present_fields_json"]))
    for field in DAILY_METRICS:
        if field in present:
            result[field] = row[field]
    return result


def _sample_shape(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    result = json.loads(row["compatibility_extra_json"])
    result.update(
        id=row["entity_id"],
        owner_email=row["owner_email"],
        timestamp=row["source_timestamp"],
        bpm=row["value"],
        created_date=row["received_at"],
        updated_date=row["recorded_at"],
    )
    if row["source_present"]:
        result["source"] = row["provider"]
    return result


class SqliteTypedWearableRepository:
    """One typed compatibility view bound to a wearable entity type."""

    def __init__(
        self,
        entity_type: str,
        connection: sqlite3.Connection | None = None,
        *,
        database: Path | None = None,
    ) -> None:
        if entity_type not in WEARABLE_TYPES:
            raise ValueError(f"unsupported wearable type: {entity_type}")
        self.entity_type = entity_type
        self.table = "wearable_daily" if entity_type in DAILY_TYPES else "wearable_samples"
        self._daily = entity_type in DAILY_TYPES
        self._connection = connection
        self._database = database
        self._columns = {
            "id": "entity_id",
            "owner_email": "owner_email",
            "source": "provider",
            "created_date": "received_at",
            "updated_date": "recorded_at",
            **({"date": "observed_date", **{field: field for field in DAILY_METRICS}}
               if self._daily else {"timestamp": "observed_at", "bpm": "value"}),
        }

    def supports_query(self, filters=None, sort=None) -> bool:
        source_filter = (filters or {}).get("source", "__absent__")
        if source_filter is None or isinstance(source_filter, dict):
            return False
        return set(filters or {}).issubset(self._columns) and (
            sort or "-created_date"
        ).lstrip("-") in self._columns

    def sync_entities(self, entities: list[dict[str, Any]]) -> list[TypedWearableProjection | None]:
        results: list[TypedWearableProjection | None] = []
        projections: list[TypedWearableProjection] = []
        invalid_ids: list[str] = []
        for entity in entities:
            try:
                projection = map_legacy(self.entity_type, entity)
            except WearableMappingError:
                if entity.get("id"):
                    invalid_ids.append(str(entity["id"]))
                results.append(None)
                continue
            projections.append(projection)
            results.append(projection)
        columns = _DAILY_COLUMNS if self._daily else _SAMPLE_COLUMNS
        with _scope(self._connection, self._database) as (connection, owns_connection):
            if invalid_ids:
                connection.executemany(
                    f"DELETE FROM {self.table} WHERE entity_id=?",
                    [(entity_id,) for entity_id in invalid_ids],
                )
            _upsert_many(connection, self.table, columns, projections)
            if owns_connection:
                connection.commit()
        return results

    def get(self, entity_id: str) -> dict[str, Any] | None:
        with _scope(self._connection, self._database) as (connection, _):
            row = connection.execute(
                f"SELECT * FROM {self.table} WHERE entity_id=? AND entity_type=?",
                (entity_id, self.entity_type),
            ).fetchone()
        if not row:
            return None
        return _daily_shape(row) if self._daily else _sample_shape(row)

    def query(self, filters=None, sort=None, limit=None, skip=0):
        if not self.supports_query(filters, sort):
            raise ValueError(f"typed {self.entity_type} query contains an unsupported field")
        with _scope(self._connection, self._database) as (connection, _):
            rows = _typed_query(
                connection,
                self.table,
                self.entity_type,
                self._columns,
                filters,
                sort,
                limit,
                skip,
            )
        shape = _daily_shape if self._daily else _sample_shape
        return [shape(row) for row in rows]


def _normalized(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalized(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalized(item) for item in value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return value


def _checksum(rows: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        _normalized(rows), sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def compare_query_results(
    legacy: list[dict[str, Any]], typed: list[dict[str, Any]]
) -> dict[str, Any]:
    legacy_checksum = _checksum(legacy)
    typed_checksum = _checksum(typed)
    legacy_sum = sum(
        float(row.get("bpm") or row.get("steps") or row.get("activity_steps") or 0)
        for row in legacy
    )
    typed_sum = sum(
        float(row.get("bpm") or row.get("steps") or row.get("activity_steps") or 0)
        for row in typed
    )
    return {
        "legacy_count": len(legacy),
        "typed_count": len(typed),
        "count_match": len(legacy) == len(typed),
        "legacy_checksum": legacy_checksum,
        "typed_checksum": typed_checksum,
        "checksum_match": legacy_checksum == typed_checksum,
        "ordering_match": [row.get("id") for row in legacy]
        == [row.get("id") for row in typed],
        "aggregate_match": abs(legacy_sum - typed_sum) < 0.001,
    }


class WearableCompatibilityRepository:
    def __init__(self, legacy: Any, typed: SqliteTypedWearableRepository):
        self.entity_type = typed.entity_type
        self._legacy = legacy
        self._typed = typed

    def query(self, filters=None, sort=None, limit=None, skip=0):
        if not self._typed.supports_query(filters, sort):
            return self._legacy.query(filters, sort, limit, skip)
        if not (typed_wearable_reads_enabled() or typed_wearable_shadow_reads_enabled()):
            return self._legacy.query(filters, sort, limit, skip)
        if typed_wearable_reads_enabled() and not typed_wearable_shadow_reads_enabled():
            return self._typed.query(filters, sort, limit, skip)
        started = time.perf_counter()
        legacy = self._legacy.query(filters, sort, limit, skip)
        legacy_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        typed = self._typed.query(filters, sort, limit, skip)
        typed_ms = (time.perf_counter() - started) * 1000
        comparison = compare_query_results(legacy, typed)
        log.info(
            "typed wearable shadow %s",
            json.dumps(
                {
                    "entity_type": self.entity_type,
                    **comparison,
                    "legacy_ms": round(legacy_ms, 3),
                    "typed_ms": round(typed_ms, 3),
                },
                sort_keys=True,
            ),
        )
        return typed if typed_wearable_reads_enabled() else legacy

    def get(self, entity_id: str):
        if typed_wearable_reads_enabled():
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


def record_typed_wearables(
    entity_type: str,
    entities: list[dict[str, Any]],
    *,
    connection: sqlite3.Connection,
) -> None:
    if typed_wearable_writes_enabled() and entity_type in WEARABLE_TYPES:
        SqliteTypedWearableRepository(entity_type, connection).sync_entities(entities)


def backfill_typed_wearables(
    database: Path = DB_PATH,
    *,
    batch_size: int = 1000,
) -> dict[str, dict[str, int]]:
    if not typed_wearable_writes_enabled():
        raise RuntimeError("TYPED_WEARABLE_WRITES_ENABLED must be true for backfill")
    from .migrations import run_migrations

    run_migrations(database)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    counts: dict[str, dict[str, int]] = {}
    try:
        for entity_type in WEARABLE_TYPES:
            repository = SqliteTypedWearableRepository(entity_type, connection)
            counts[entity_type] = {"legacy_scanned": 0, "typed_written": 0, "unmappable": 0}
            last_rowid = 0
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
                    entity.update(
                        id=row["id"],
                        created_date=row["created_date"],
                        updated_date=row["updated_date"],
                    )
                    entities.append(entity)
                    last_rowid = row["rowid"]
                results = repository.sync_entities(entities)
                counts[entity_type]["legacy_scanned"] += len(results)
                counts[entity_type]["typed_written"] += sum(item is not None for item in results)
                counts[entity_type]["unmappable"] += sum(item is None for item in results)
                connection.commit()
    finally:
        connection.close()
    return counts


def _domain_comparison(connection: sqlite3.Connection, entity_type: str) -> dict[str, Any]:
    daily = entity_type in DAILY_TYPES
    table = "wearable_daily" if daily else "wearable_samples"
    shape = _daily_shape if daily else _sample_shape
    sort_field = "date" if daily else "timestamp"
    expected: dict[str, str] = {}
    expected_rows: list[dict[str, Any]] = []
    unmappable = 0
    for row in connection.execute("SELECT * FROM entities WHERE type=?", (entity_type,)):
        entity = json.loads(row["data"])
        entity.update(id=row["id"], created_date=row["created_date"], updated_date=row["updated_date"])
        try:
            projection = map_legacy(entity_type, entity)
        except WearableMappingError:
            unmappable += 1
            continue
        expected[row["id"]] = projection.row["legacy_fingerprint"]
        expected_rows.append(entity)
    actual: dict[str, str] = {}
    stored: dict[str, str] = {}
    typed_rows: list[dict[str, Any]] = []
    for row in connection.execute(f"SELECT * FROM {table} WHERE entity_type=?", (entity_type,)):
        item = dict(row)
        actual[item["entity_id"]] = _fingerprint(item)
        stored[item["entity_id"]] = item["legacy_fingerprint"]
        typed_rows.append(shape(item))
    drift = {entity_id for entity_id in actual if actual[entity_id] != stored[entity_id]}
    mismatched = {
        entity_id
        for entity_id in set(expected) & set(actual)
        if expected[entity_id] != actual[entity_id]
    } | (drift & set(expected))
    ordered_legacy = sorted(expected_rows, key=lambda row: (str(row.get(sort_field)), row["id"]))
    ordered_typed = sorted(typed_rows, key=lambda row: (str(row.get(sort_field)), row["id"]))
    return {
        "legacy_total": len(expected) + unmappable,
        "mappable": len(expected),
        "unmappable": unmappable,
        "typed_total": len(actual),
        "matched": len(set(expected) & set(actual)) - len(mismatched),
        "missing": len(set(expected) - set(actual)),
        "mismatched": len(mismatched),
        "fingerprint_drift": len(drift),
        "extra": len(set(actual) - set(expected)),
        "query": compare_query_results(ordered_legacy, ordered_typed),
    }


def compare_wearable_stores(database: Path = DB_PATH) -> dict[str, Any]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        domains = {
            entity_type: _domain_comparison(connection, entity_type)
            for entity_type in WEARABLE_TYPES
        }
    finally:
        connection.close()
    return {"domains": domains, "mapping_version": MAPPING_VERSION}


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("backfill", "compare"))
    parser.add_argument("--database", type=Path, default=DB_PATH)
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()
    result = (
        backfill_typed_wearables(args.database, batch_size=max(1, args.batch_size))
        if args.command == "backfill"
        else compare_wearable_stores(args.database)
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    _main()
