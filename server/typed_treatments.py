"""Strict typed projection of legacy Treatment entities.

Legacy JSON remains the write authority and generic API contract.  This module
provides a feature-gated, transaction-local sidecar, an explicit backfill, and
value-free parity reporting so a later issue can review a read cutover.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .canonical_time import normalize_entity_times
from .config import APP_TIMEZONE, DB_PATH, OWNER_EMAIL
from .data_contracts import (
    DEPLOYMENT_OWNER_ID,
    canonical_entity_id,
    canonical_source_record_id,
)


MAPPING_VERSION = "typed-treatment/1.0.0"
_TRUE = {"1", "true", "yes", "on"}
log = logging.getLogger("glucopilot.typed_treatments")
_TOTAL_RE = re.compile(r"(?:^|\|)\s*total:\s*([0-9]+(?:\.[0-9]+)?)\s*u?\b", re.I)
_BASAL_RE = re.compile(r"(?:^|\|)\s*basal:\s*([0-9]+(?:\.[0-9]+)?)\s*u?\b", re.I)
_BOLUS_RE = re.compile(r"(?:^|\|)\s*bolus:\s*([0-9]+(?:\.[0-9]+)?)\s*u?\b", re.I)

_COMMON_COLUMNS = (
    "entity_id",
    "canonical_id",
    "owner_id",
    "owner_email",
    "source",
    "source_record_id",
    "source_record_canonical_id",
    "occurred_at",
    "source_timestamp",
    "local_date",
    "kind",
    "legacy_type",
    "event_type",
    "amount_value",
    "amount_unit",
    "insulin_type",
    "glucose_mg_dl",
    "glucose_type",
    "notes",
    "reason",
    "pre_bolus_minutes",
    "legacy_fingerprint",
    "mapping_version",
    "received_at",
    "recorded_at",
    "created_at",
    "updated_at",
)
_BASAL_COLUMNS = (
    "treatment_entity_id",
    "owner_id",
    "source",
    "source_record_id",
    "segment_kind",
    "started_at",
    "ended_at",
    "duration_seconds",
    "rate_units_per_hour",
    "percent_of_profile",
    "mapping_version",
    "created_at",
    "updated_at",
)
_TOTAL_COLUMNS = (
    "treatment_entity_id",
    "owner_id",
    "source",
    "source_record_id",
    "occurred_at",
    "local_date",
    "total_units",
    "basal_units",
    "bolus_units",
    "completeness",
    "mapping_version",
    "created_at",
    "updated_at",
)


class TreatmentMappingError(ValueError):
    """A legacy Treatment cannot truthfully satisfy the strict schema."""


@dataclass(frozen=True)
class TypedTreatmentProjection:
    treatment: dict[str, Any]
    basal_segment: dict[str, Any] | None
    pump_daily_total: dict[str, Any] | None


def typed_treatment_writes_enabled() -> bool:
    return os.getenv("TYPED_TREATMENT_WRITES_ENABLED", "false").strip().lower() in _TRUE


def typed_treatment_shadow_reads_enabled() -> bool:
    return os.getenv("TYPED_TREATMENT_SHADOW_READS_ENABLED", "false").strip().lower() in _TRUE


def typed_treatment_reads_enabled() -> bool:
    return os.getenv("TYPED_TREATMENT_READS_ENABLED", "false").strip().lower() in _TRUE


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _float(value: Any, field: str, *, allow_negative: bool = False) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise TreatmentMappingError(f"{field} is not numeric") from error
    if not allow_negative and result < 0:
        raise TreatmentMappingError(f"{field} cannot be negative")
    return result


def _match_float(pattern: re.Pattern[str], value: Any) -> float | None:
    match = pattern.search(str(value or ""))
    return float(match.group(1)) if match else None


def parse_pump_daily_total(notes: Any) -> dict[str, float | str | None] | None:
    """Parse the established Tandem/Nightscout Daily Total note format."""
    total = _match_float(_TOTAL_RE, notes)
    if total is None:
        return None
    basal = _match_float(_BASAL_RE, notes)
    bolus = _match_float(_BOLUS_RE, notes)
    return {
        "total_units": total,
        "basal_units": basal,
        "bolus_units": bolus,
        "completeness": "complete" if basal is not None and bolus is not None else "partial",
    }


def _resolved_occurred_at(entity: dict[str, Any]) -> tuple[str, str]:
    rows = normalize_entity_times("Treatment", entity, default_timezone=APP_TIMEZONE)
    start = next((row for row in rows if row["role"] == "effective_start"), None)
    if not start or not start.get("canonical_at"):
        raise TreatmentMappingError("timestamp is not an unambiguous instant")
    local_date = start.get("local_date") or str(start["canonical_at"])[:10]
    return str(start["canonical_at"]), str(local_date)


def _utc_add_seconds(instant: str, seconds: float) -> str:
    parsed = datetime.fromisoformat(instant.replace("Z", "+00:00"))
    return (parsed + timedelta(seconds=seconds)).astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _query_value(field: str, value: Any) -> Any:
    """Normalize time bounds to the canonical storage form used by indexes."""
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


def _kind(legacy_type: str) -> str:
    return {
        "insulin": "insulin",
        "carb": "carbohydrate",
        "tempbasal": "basal",
        "suspension": "suspension",
        "bg": "blood_glucose",
        "note": "note",
        "other": "other",
    }.get(legacy_type.lower(), "other")


def _fingerprint_payload(
    treatment: dict[str, Any],
    basal: dict[str, Any] | None,
    daily_total: dict[str, Any] | None,
) -> dict[str, Any]:
    excluded = {"legacy_fingerprint", "created_at", "updated_at"}
    return {
        "treatment": {key: treatment[key] for key in sorted(treatment) if key not in excluded},
        "basal_segment": {
            key: basal[key] for key in sorted(basal) if key not in {"created_at", "updated_at"}
        }
        if basal
        else None,
        "pump_daily_total": {
            key: daily_total[key]
            for key in sorted(daily_total)
            if key not in {"created_at", "updated_at"}
        }
        if daily_total
        else None,
    }


def _fingerprint(
    treatment: dict[str, Any],
    basal: dict[str, Any] | None,
    daily_total: dict[str, Any] | None,
) -> str:
    payload = json.dumps(
        _fingerprint_payload(treatment, basal, daily_total),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def map_legacy_treatment(entity: dict[str, Any]) -> TypedTreatmentProjection:
    """Map one compatibility entity without inventing missing clinical facts."""
    entity_id = str(entity.get("id") or "").strip()
    if not entity_id:
        raise TreatmentMappingError("entity id is required")
    legacy_type = str(entity.get("type") or "other").strip().lower() or "other"
    kind = _kind(legacy_type)
    occurred_at, local_date = _resolved_occurred_at(entity)
    source = str(entity.get("source") or "legacy").strip().lower() or "legacy"
    source_record_id = str(entity.get("ns_id") or "").strip() or None
    owner_email = str(entity.get("owner_email") or OWNER_EMAIL).strip() or OWNER_EMAIL
    amount = _float(entity.get("amount"), "amount")
    glucose = _float(entity.get("glucose"), "glucose")
    if kind == "carbohydrate" and amount is None:
        raise TreatmentMappingError("carbohydrate amount is required")
    if kind == "blood_glucose" and glucose is None:
        raise TreatmentMappingError("blood glucose value is required")
    received_at = str(entity.get("created_date") or occurred_at)
    recorded_at = str(entity.get("updated_date") or received_at)
    if not received_at.endswith("Z"):
        received_at = occurred_at
    if not recorded_at.endswith("Z"):
        recorded_at = received_at
    now = _now_iso()
    treatment = {
        "entity_id": entity_id,
        "canonical_id": canonical_entity_id("Treatment", entity_id),
        "owner_id": DEPLOYMENT_OWNER_ID,
        "owner_email": owner_email,
        "source": source,
        "source_record_id": source_record_id,
        "source_record_canonical_id": canonical_source_record_id(source, source_record_id)
        if source_record_id
        else None,
        "occurred_at": occurred_at,
        "source_timestamp": str(entity.get("timestamp")),
        "local_date": local_date,
        "kind": kind,
        "legacy_type": legacy_type,
        "event_type": str(entity.get("event_type")) if entity.get("event_type") not in (None, "") else None,
        "amount_value": amount,
        "amount_unit": "U" if kind == "insulin" and amount is not None else "g"
        if kind == "carbohydrate" and amount is not None
        else None,
        "insulin_type": str(entity.get("insulin_type")) if entity.get("insulin_type") not in (None, "") else None,
        "glucose_mg_dl": glucose,
        "glucose_type": str(entity.get("glucose_type")) if entity.get("glucose_type") not in (None, "") else None,
        "notes": str(entity.get("notes")) if entity.get("notes") not in (None, "") else None,
        "reason": str(entity.get("reason")) if entity.get("reason") not in (None, "") else None,
        "pre_bolus_minutes": _float(entity.get("preBolus"), "preBolus", allow_negative=True),
        "legacy_fingerprint": "",
        "mapping_version": MAPPING_VERSION,
        "received_at": received_at,
        "recorded_at": recorded_at,
        "created_at": now,
        "updated_at": now,
    }

    basal = None
    if kind in {"basal", "suspension"}:
        duration_seconds = None
        duration_minutes = _float(entity.get("duration"), "duration")
        if duration_minutes is not None:
            duration_seconds = duration_minutes * 60
        rate = _float(entity.get("absolute"), "absolute")
        if kind == "suspension" and rate is None:
            rate = 0.0
        basal = {
            "treatment_entity_id": entity_id,
            "owner_id": DEPLOYMENT_OWNER_ID,
            "source": source,
            "source_record_id": source_record_id,
            "segment_kind": "temp_basal" if kind == "basal" else "suspension",
            "started_at": occurred_at,
            "ended_at": _utc_add_seconds(occurred_at, duration_seconds)
            if duration_seconds is not None
            else None,
            "duration_seconds": duration_seconds,
            "rate_units_per_hour": rate,
            "percent_of_profile": _float(entity.get("percent"), "percent", allow_negative=True),
            "mapping_version": MAPPING_VERSION,
            "created_at": now,
            "updated_at": now,
        }

    daily_total = None
    if legacy_type == "insulin" and str(entity.get("event_type") or "").lower() == "daily total":
        parsed = parse_pump_daily_total(entity.get("notes"))
        if parsed:
            daily_total = {
                "treatment_entity_id": entity_id,
                "owner_id": DEPLOYMENT_OWNER_ID,
                "source": source,
                "source_record_id": source_record_id,
                "occurred_at": occurred_at,
                "local_date": local_date,
                **parsed,
                "mapping_version": MAPPING_VERSION,
                "created_at": now,
                "updated_at": now,
            }

    treatment["legacy_fingerprint"] = _fingerprint(treatment, basal, daily_total)
    return TypedTreatmentProjection(treatment, basal, daily_total)


@contextmanager
def _scope(
    connection: sqlite3.Connection | None,
    database: Path | None = None,
) -> Iterator[tuple[sqlite3.Connection, bool]]:
    if connection is not None:
        yield connection, False
        return
    if database is None:
        from .db import connect

        opened = connect()
    else:
        opened = sqlite3.connect(database)
    opened.row_factory = sqlite3.Row
    opened.execute("PRAGMA foreign_keys=ON")
    try:
        yield opened, True
    finally:
        opened.close()


def _upsert(connection: sqlite3.Connection, table: str, columns: tuple[str, ...], row: dict[str, Any]) -> None:
    key = columns[0]
    updates = [column for column in columns[1:] if column != "created_at"]
    connection.execute(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
        f"ON CONFLICT({key}) DO UPDATE SET "
        + ",".join(f"{column}=excluded.{column}" for column in updates),
        tuple(row[column] for column in columns),
    )


class SqliteTypedTreatmentRepository:
    """Idempotent typed projection writes and compatibility-shaped reads."""

    entity_type = "Treatment"

    def __init__(
        self,
        connection: sqlite3.Connection | None = None,
        *,
        database: Path | None = None,
    ) -> None:
        self._connection = connection
        self._database = database

    def sync_entity(self, entity: dict[str, Any]) -> TypedTreatmentProjection | None:
        return self.sync_entities([entity])[0]

    def sync_entities(self, entities: list[dict[str, Any]]) -> list[TypedTreatmentProjection | None]:
        results: list[TypedTreatmentProjection | None] = []
        with _scope(self._connection, self._database) as (connection, owns_connection):
            for entity in entities:
                entity_id = str(entity.get("id") or "")
                try:
                    projection = map_legacy_treatment(entity)
                except TreatmentMappingError:
                    if entity_id:
                        connection.execute(
                            "DELETE FROM typed_treatments WHERE entity_id=?", (entity_id,)
                        )
                    results.append(None)
                    continue
                _upsert(connection, "typed_treatments", _COMMON_COLUMNS, projection.treatment)
                connection.execute(
                    "DELETE FROM basal_segments WHERE treatment_entity_id=?", (entity_id,)
                )
                connection.execute(
                    "DELETE FROM pump_daily_totals WHERE treatment_entity_id=?", (entity_id,)
                )
                if projection.basal_segment:
                    _upsert(connection, "basal_segments", _BASAL_COLUMNS, projection.basal_segment)
                if projection.pump_daily_total:
                    _upsert(
                        connection,
                        "pump_daily_totals",
                        _TOTAL_COLUMNS,
                        projection.pump_daily_total,
                    )
                results.append(projection)
            if owns_connection:
                connection.commit()
        return results

    def get(self, entity_id: str) -> dict[str, Any] | None:
        with _scope(self._connection, self._database) as (connection, _):
            row = connection.execute(
                "SELECT * FROM typed_treatments WHERE entity_id=?", (entity_id,)
            ).fetchone()
            basal = connection.execute(
                "SELECT * FROM basal_segments WHERE treatment_entity_id=?", (entity_id,)
            ).fetchone()
        return _legacy_shape(row, basal) if row else None

    def supports_query(self, filters: dict[str, Any] | None, sort: str | None) -> bool:
        allowed = {
            "id",
            "owner_email",
            "timestamp",
            "source",
            "ns_id",
            "type",
            "event_type",
            "amount",
            "insulin_type",
            "glucose",
            "glucose_type",
            "created_date",
            "updated_date",
        }
        sort_key = (sort or "-created_date").lstrip("-")
        return set(filters or {}).issubset(allowed) and sort_key in allowed

    def query(
        self,
        filters: dict[str, Any] | None = None,
        sort: str | None = None,
        limit: int | None = None,
        skip: int = 0,
    ) -> list[dict[str, Any]]:
        if not self.supports_query(filters, sort):
            raise ValueError("typed Treatment query contains an unsupported compatibility field")
        columns = {
            "id": "t.entity_id",
            "owner_email": "t.owner_email",
            "timestamp": "t.occurred_at",
            "source": "t.source",
            "ns_id": "t.source_record_id",
            "type": "t.legacy_type",
            "event_type": "t.event_type",
            "amount": "t.amount_value",
            "insulin_type": "t.insulin_type",
            "glucose": "t.glucose_mg_dl",
            "glucose_type": "t.glucose_type",
            "created_date": "t.received_at",
            "updated_date": "t.recorded_at",
        }
        where: list[str] = []
        parameters: list[Any] = []
        for key, value in (filters or {}).items():
            column = columns[key]
            if isinstance(value, dict) and any(str(op).startswith("$") for op in value):
                for operator, operand in value.items():
                    if operator == "$in":
                        if not isinstance(operand, list) or not operand:
                            raise ValueError(f"$in requires a non-empty list for {key}")
                        where.append(f"{column} IN ({','.join('?' for _ in operand)})")
                        parameters.extend(_query_value(key, item) for item in operand)
                    else:
                        sql_operator = {"$gte": ">=", "$gt": ">", "$lte": "<=", "$lt": "<", "$ne": "!="}.get(operator)
                        if not sql_operator:
                            raise ValueError(f"Unsupported filter operator: {operator}")
                        where.append(f"{column} {sql_operator} ?")
                        parameters.append(_query_value(key, operand))
            elif value is None:
                where.append(f"{column} IS NULL")
            else:
                where.append(f"{column} = ?")
                parameters.append(_query_value(key, value))
        sort = (sort or "-created_date").strip()
        direction = "DESC" if sort.startswith("-") else "ASC"
        sort_column = columns[sort.lstrip("-")]
        sql = (
            "SELECT t.*, b.segment_kind, b.duration_seconds, b.rate_units_per_hour, "
            "b.percent_of_profile "
            "FROM typed_treatments t LEFT JOIN basal_segments b "
            "ON b.treatment_entity_id=t.entity_id"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        # Legacy JSON ordering has no explicit tie-breaker and SQLite preserves
        # its directional insertion scan order for equal values. Typed rows are
        # inserted in that same legacy-row order, so rowid preserves pagination
        # boundaries where many treatment events share one provider timestamp.
        sql += (
            f" ORDER BY {sort_column} {direction}, t.rowid {direction} "
            "LIMIT ? OFFSET ?"
        )
        parameters.extend([int(limit) if limit else -1, int(skip or 0)])
        with _scope(self._connection, self._database) as (connection, _):
            rows = connection.execute(sql, parameters).fetchall()
        return [_legacy_shape(row, row) for row in rows]


class SqliteBasalSegmentRepository:
    def __init__(self, connection: sqlite3.Connection | None = None, *, database: Path | None = None):
        self._connection = connection
        self._database = database

    def for_treatment(self, entity_id: str) -> dict[str, Any] | None:
        with _scope(self._connection, self._database) as (connection, _):
            row = connection.execute(
                "SELECT * FROM basal_segments WHERE treatment_entity_id=?", (entity_id,)
            ).fetchone()
        return dict(row) if row else None


class SqlitePumpDailyTotalRepository:
    def __init__(self, connection: sqlite3.Connection | None = None, *, database: Path | None = None):
        self._connection = connection
        self._database = database

    def for_treatment(self, entity_id: str) -> dict[str, Any] | None:
        with _scope(self._connection, self._database) as (connection, _):
            row = connection.execute(
                "SELECT * FROM pump_daily_totals WHERE treatment_entity_id=?", (entity_id,)
            ).fetchone()
        return dict(row) if row else None

    def by_date(self, start: str, end: str) -> list[dict[str, Any]]:
        with _scope(self._connection, self._database) as (connection, _):
            rows = connection.execute(
                "SELECT * FROM pump_daily_totals WHERE owner_id=? AND local_date>=? "
                "AND local_date<=? ORDER BY local_date, treatment_entity_id",
                (DEPLOYMENT_OWNER_ID, start, end),
            ).fetchall()
        return [dict(row) for row in rows]


class TreatmentCompatibilityRepository:
    """Keep legacy mutations while allowing an explicitly gated typed read."""

    entity_type = "Treatment"

    def __init__(self, legacy: Any, typed: SqliteTypedTreatmentRepository) -> None:
        self._legacy = legacy
        self._typed = typed

    def query(self, filters=None, sort=None, limit=None, skip=0):
        if not self._typed.supports_query(filters, sort):
            return self._legacy.query(filters, sort, limit, skip)
        if not (
            typed_treatment_reads_enabled()
            or typed_treatment_shadow_reads_enabled()
        ):
            return self._legacy.query(filters, sort, limit, skip)
        if (
            typed_treatment_reads_enabled()
            and not typed_treatment_shadow_reads_enabled()
        ):
            return self._typed.query(filters, sort, limit, skip)
        started = time.perf_counter()
        legacy = self._legacy.query(filters, sort, limit, skip)
        legacy_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        typed = self._typed.query(filters, sort, limit, skip)
        typed_ms = (time.perf_counter() - started) * 1000
        comparison = compare_treatment_query_results(legacy, typed)
        log.info(
            "typed treatment shadow %s",
            json.dumps(
                {
                    **comparison,
                    "legacy_ms": round(legacy_ms, 3),
                    "typed_ms": round(typed_ms, 3),
                },
                sort_keys=True,
            ),
        )
        return typed if typed_treatment_reads_enabled() else legacy

    def get(self, entity_id: str):
        if typed_treatment_reads_enabled():
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


def _legacy_shape(row: sqlite3.Row, basal: sqlite3.Row | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": row["entity_id"],
        "owner_email": row["owner_email"],
        "timestamp": row["source_timestamp"],
        "source": row["source"],
        "type": row["legacy_type"],
        "created_date": row["received_at"],
        "updated_date": row["recorded_at"],
    }
    mappings = {
        "event_type": "event_type",
        "amount_value": "amount",
        "insulin_type": "insulin_type",
        "glucose_mg_dl": "glucose",
        "glucose_type": "glucose_type",
        "notes": "notes",
        "reason": "reason",
        "pre_bolus_minutes": "preBolus",
        "source_record_id": "ns_id",
    }
    for source, target in mappings.items():
        if row[source] is not None:
            result[target] = row[source]
    if basal:
        if basal["duration_seconds"] is not None:
            result["duration"] = basal["duration_seconds"] / 60
        if (
            basal["rate_units_per_hour"] is not None
            and basal["segment_kind"] != "suspension"
        ):
            result["absolute"] = basal["rate_units_per_hour"]
        if basal["percent_of_profile"] is not None:
            result["percent"] = basal["percent_of_profile"]
    return result


_QUERY_FIELDS = (
    "id",
    "owner_email",
    "timestamp",
    "source",
    "type",
    "event_type",
    "amount",
    "insulin_type",
    "glucose",
    "glucose_type",
    "notes",
    "reason",
    "preBolus",
    "ns_id",
    "duration",
    "absolute",
    "percent",
    "created_date",
    "updated_date",
)


def _query_shape(row: dict[str, Any]) -> dict[str, Any]:
    numeric_fields = {
        "amount",
        "glucose",
        "preBolus",
        "duration",
        "absolute",
        "percent",
    }
    return {
        key: float(row[key]) if key in numeric_fields and row[key] is not None else row[key]
        for key in _QUERY_FIELDS
        if key in row
    }


def _query_checksum(rows: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        [_query_shape(row) for row in rows],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def compare_treatment_query_results(
    legacy: list[dict[str, Any]],
    typed: list[dict[str, Any]],
) -> dict[str, Any]:
    legacy_checksum = _query_checksum(legacy)
    typed_checksum = _query_checksum(typed)
    legacy_sum = sum(float(row.get("amount") or 0) for row in legacy)
    typed_sum = sum(float(row.get("amount") or 0) for row in typed)
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


def _mapping_error_code(error: TreatmentMappingError) -> str:
    message = str(error).lower()
    if "timestamp" in message or "instant" in message:
        return "timestamp_not_unambiguous"
    if "entity id" in message:
        return "entity_id_missing"
    if "numeric" in message or "negative" in message:
        return "numeric_contract_invalid"
    return "typed_contract_invalid"


def record_typed_treatments(
    entity_type: str,
    entities: list[dict[str, Any]],
    *,
    connection: sqlite3.Connection,
) -> None:
    if entity_type != "Treatment" or not typed_treatment_writes_enabled():
        return
    SqliteTypedTreatmentRepository(connection).sync_entities(entities)


def backfill_typed_treatments(
    database: Path = DB_PATH,
    *,
    batch_size: int = 1000,
) -> dict[str, int]:
    if not typed_treatment_writes_enabled():
        raise RuntimeError("TYPED_TREATMENT_WRITES_ENABLED must be true for backfill")
    from .migrations import run_migrations

    run_migrations(database)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    repository = SqliteTypedTreatmentRepository(connection, database=database)
    scanned = written = unmappable = 0
    last_rowid = 0
    try:
        while True:
            rows = connection.execute(
                "SELECT rowid, * FROM entities WHERE type='Treatment' AND rowid>? "
                "ORDER BY rowid LIMIT ?",
                (last_rowid, max(1, batch_size)),
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
            scanned += len(results)
            written += sum(result is not None for result in results)
            unmappable += sum(result is None for result in results)
            connection.commit()
    finally:
        connection.close()
    return {"legacy_scanned": scanned, "typed_written": written, "unmappable": unmappable}


def compare_treatment_stores(database: Path = DB_PATH) -> dict[str, Any]:
    """Return deterministic, value-free parity counts for rollout review."""
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    expected: dict[str, str] = {}
    expected_rows: list[tuple[str, str, dict[str, Any]]] = []
    unmappable_by_reason: dict[str, int] = {}
    unmappable = 0
    try:
        for row in connection.execute("SELECT * FROM entities WHERE type='Treatment'"):
            entity = json.loads(row["data"])
            entity.update(
                id=row["id"],
                created_date=row["created_date"],
                updated_date=row["updated_date"],
            )
            try:
                projection = map_legacy_treatment(entity)
            except TreatmentMappingError as error:
                unmappable += 1
                code = _mapping_error_code(error)
                unmappable_by_reason[code] = unmappable_by_reason.get(code, 0) + 1
                continue
            expected[row["id"]] = projection.treatment["legacy_fingerprint"]
            expected_rows.append(
                (
                    projection.treatment["occurred_at"],
                    row["id"],
                    entity,
                )
            )
        basal_rows = {
            row["treatment_entity_id"]: dict(row)
            for row in connection.execute("SELECT * FROM basal_segments")
        }
        total_rows = {
            row["treatment_entity_id"]: dict(row)
            for row in connection.execute("SELECT * FROM pump_daily_totals")
        }
        stored_fingerprints: dict[str, str] = {}
        actual: dict[str, str] = {}
        actual_rows: list[tuple[str, str, dict[str, Any]]] = []
        for row in connection.execute("SELECT * FROM typed_treatments"):
            treatment = dict(row)
            entity_id = treatment["entity_id"]
            stored_fingerprints[entity_id] = treatment["legacy_fingerprint"]
            actual[entity_id] = _fingerprint(
                treatment,
                basal_rows.get(entity_id),
                total_rows.get(entity_id),
            )
            actual_rows.append(
                (
                    treatment["occurred_at"],
                    entity_id,
                    _legacy_shape(row, basal_rows.get(entity_id)),
                )
            )
        missing = set(expected) - set(actual)
        extra = set(actual) - set(expected)
        fingerprint_drift = {
            entity_id
            for entity_id in actual
            if actual[entity_id] != stored_fingerprints[entity_id]
        }
        mismatched = {
            entity_id
            for entity_id in set(expected) & set(actual)
            if expected[entity_id] != actual[entity_id]
        } | (fingerprint_drift & set(expected))
        matched = len(set(expected) & set(actual)) - len(mismatched)
        child_counts = {
            "basal_segments": connection.execute("SELECT COUNT(*) FROM basal_segments").fetchone()[0],
            "pump_daily_totals": connection.execute("SELECT COUNT(*) FROM pump_daily_totals").fetchone()[0],
        }
        duplicate_source_identities = connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT owner_id, source, source_record_id
                FROM typed_treatments
                WHERE source_record_id IS NOT NULL
                GROUP BY owner_id, source, source_record_id
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
    finally:
        connection.close()
    return {
        "legacy_total": len(expected) + unmappable,
        "mappable": len(expected),
        "unmappable": unmappable,
        "unmappable_by_reason": dict(sorted(unmappable_by_reason.items())),
        "typed_total": len(actual),
        "matched": matched,
        "missing": len(missing),
        "mismatched": len(mismatched),
        "fingerprint_drift": len(fingerprint_drift),
        "extra": len(extra),
        "duplicate_source_identities": duplicate_source_identities,
        "query": compare_treatment_query_results(
            [item for _, _, item in sorted(expected_rows)],
            [item for _, _, item in sorted(actual_rows)],
        ),
        **child_counts,
        "mapping_version": MAPPING_VERSION,
    }


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("backfill", "compare"))
    parser.add_argument("--database", type=Path, default=DB_PATH)
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()
    if args.command == "backfill":
        result = backfill_typed_treatments(args.database, batch_size=max(1, args.batch_size))
    else:
        result = compare_treatment_stores(args.database)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    _main()
