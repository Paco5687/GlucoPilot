"""Read-only, value-free inventory and benchmark for a GlucoPilot SQLite store.

The report intentionally includes structural metadata only: entity counts,
field names and JSON types, index definitions, query plans, and timings. It
never emits entity values, source identifiers, owner identities, timestamps,
or application settings.

Run inside an installed container with::

    python -m server.data_audit /data/app.sqlite3
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote


BENCHMARKS = (
    ("GlucoseReading", "timestamp", 5_000),
    ("Treatment", "timestamp", 2_000),
    ("FitbitHeartRate", "timestamp", 400),
    ("OuraHeartRate", "timestamp", 5_000),
    ("LabResult", "collected_date", 5_000),
    ("OuraDaily", "date", 90),
    ("FitbitDaily", "date", 120),
)


def connect_read_only(path: Path) -> sqlite3.Connection:
    """Open an existing SQLite database without permitting writes."""
    resolved = path.expanduser().resolve(strict=True)
    uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _schema(connection: sqlite3.Connection) -> dict[str, Any]:
    tables = []
    for name, sql in connection.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        columns = [
            {
                "name": row[1],
                "type": row[2],
                "not_null": bool(row[3]),
                "primary_key": bool(row[5]),
            }
            for row in connection.execute(f'PRAGMA table_info("{name}")')
        ]
        tables.append({"name": name, "sql": sql, "columns": columns})

    indexes = [
        {"name": name, "table": table, "sql": sql}
        for name, table, sql in connection.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master "
            "WHERE type='index' ORDER BY tbl_name, name"
        )
    ]
    return {"tables": tables, "indexes": indexes}


def _entity_inventory(connection: sqlite3.Connection) -> tuple[dict[str, int], dict[str, Any]]:
    counts = dict(
        connection.execute("SELECT type, COUNT(*) FROM entities GROUP BY type ORDER BY type")
    )
    fields: dict[str, dict[str, Any]] = {}
    rows = connection.execute(
        """
        SELECT e.type, j.key, COUNT(*), GROUP_CONCAT(DISTINCT j.type)
        FROM entities AS e, json_each(e.data) AS j
        GROUP BY e.type, j.key
        ORDER BY e.type, j.key
        """
    )
    for entity_type, key, present, json_types in rows:
        fields.setdefault(entity_type, {})[key] = {
            "present": present,
            "types": sorted(json_types.split(",")),
        }
    return counts, fields


def _benchmarks(connection: sqlite3.Connection, repeat: int) -> list[dict[str, Any]]:
    results = []
    for entity_type, sort_field, limit in BENCHMARKS:
        sql = (
            "SELECT * FROM entities WHERE type=? "
            f"ORDER BY json_extract(data, '$.{sort_field}') DESC LIMIT ?"
        )
        samples = []
        row_count = 0
        for _ in range(repeat):
            started = time.perf_counter()
            rows = connection.execute(sql, (entity_type, limit)).fetchall()
            samples.append((time.perf_counter() - started) * 1_000)
            row_count = len(rows)
        plan = [
            row[3]
            for row in connection.execute(
                "EXPLAIN QUERY PLAN " + sql, (entity_type, limit)
            )
        ]
        results.append(
            {
                "entity": entity_type,
                "sort_field": sort_field,
                "limit": limit,
                "rows": row_count,
                "median_ms": round(statistics.median(samples), 3),
                "max_ms": round(max(samples), 3),
                "plan": plan,
            }
        )
    return results


def audit_database(path: Path, repeat: int = 5) -> dict[str, Any]:
    """Return a privacy-safe structural report for ``path``."""
    if repeat < 1:
        raise ValueError("repeat must be at least 1")

    started = time.perf_counter()
    with connect_read_only(path) as connection:
        required = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entities'"
        ).fetchone()
        if not required:
            raise ValueError("database does not contain an entities table")

        schema = _schema(connection)
        counts, fields = _entity_inventory(connection)
        page_count = connection.execute("PRAGMA page_count").fetchone()[0]
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        benchmarks = _benchmarks(connection, repeat)

    return {
        "privacy": (
            "Structural metadata only; no entity values, source identifiers, owner "
            "identities, timestamps, or settings."
        ),
        "sqlite_version": sqlite3.sqlite_version,
        "logical_database_bytes": page_count * page_size,
        "entity_total": sum(counts.values()),
        "entity_counts": counts,
        "fields": fields,
        "schema": schema,
        "benchmarks": benchmarks,
        "inventory_ms": round((time.perf_counter() - started) * 1_000, 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="path to an existing app.sqlite3")
    parser.add_argument(
        "--repeat",
        type=int,
        default=5,
        help="number of samples per representative query (default: 5)",
    )
    args = parser.parse_args()
    print(json.dumps(audit_database(args.database, args.repeat), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
