"""SQLite-backed generic entity store replicating Base44 entity semantics.

Every record lives in one `entities` table as a JSON document. This mirrors the
flexible filter/sort/limit/skip API the Base44 SDK exposes, which keeps the
ported frontend and function code close to the original.
"""

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .config import DATA_DIR, DB_PATH

ENTITY_TYPES = {
    "GlucoseReading",
    "Treatment",
    "DailySummary",
    "WeeklySummary",
    "Pattern",
    "Insight",
    "AIConversation",
    "PeriodLog",
    "NightscoutProfile",
    "OuraConnection",
    "OuraDaily",
    "OuraHeartRate",
    "UserSettings",
    "DexcomConnection",
    "MedicalRecord",
    "LabResult",
    "FitbitConnection",
    "FitbitDaily",
    "FitbitHeartRate",
}

_COLUMN_FIELDS = {"id", "created_date", "updated_date"}


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    return db


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                data TEXT NOT NULL,
                created_date TEXT NOT NULL,
                updated_date TEXT NOT NULL
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type)")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_entities_type_ts ON entities(type, json_extract(data, '$.timestamp'))"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_entities_type_date ON entities(type, json_extract(data, '$.date'))"
        )
        db.commit()
    _migrate_legacy_dexcom_tokens()


def _migrate_legacy_dexcom_tokens() -> None:
    """Carry Dexcom tokens forward from the pre-rewrite `dexcom_tokens` table.

    Preserves an existing production Dexcom authorization across the rewrite so
    the single user-license slot never needs a fresh consent flow.
    """
    from .config import OWNER_EMAIL  # local import to avoid a config<->db cycle at import time

    with connect() as db:
        table = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='dexcom_tokens'"
        ).fetchone()
        if not table:
            return
        row = db.execute(
            "SELECT access_token, refresh_token, token_type, expires_at FROM dexcom_tokens WHERE id=1"
        ).fetchone()
    if not row:
        return
    if query_entities("DexcomConnection", {"owner_email": OWNER_EMAIL}, limit=1):
        return
    create_entity(
        "DexcomConnection",
        {
            "owner_email": OWNER_EMAIL,
            "access_token": row["access_token"],
            "refresh_token": row["refresh_token"],
            "token_type": row["token_type"] or "Bearer",
            "expires_at": row["expires_at"],
            "connected": True,
        },
    )


def get_setting(key: str) -> str:
    with connect() as db:
        row = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""


def config_value(name: str, default: str = "") -> str:
    """Resolve a config value: DB-stored setting (from the in-app Settings page)
    wins over the environment variable of the same (uppercased) name."""
    import os

    stored = get_setting(f"cfg_{name.lower()}")
    if stored:
        return stored
    return os.getenv(name.upper(), default)


def set_config_value(name: str, value: str) -> None:
    set_setting(f"cfg_{name.lower()}", value)


def set_setting(key: str, value: str) -> None:
    with connect() as db:
        db.execute(
            """
            INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, int(time.time())),
        )
        db.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _row_to_record(row: sqlite3.Row) -> dict[str, Any]:
    record = json.loads(row["data"])
    record["id"] = row["id"]
    record["created_date"] = row["created_date"]
    record["updated_date"] = row["updated_date"]
    return record


def _filter_value(value: Any) -> Any:
    # json_extract() surfaces JSON booleans as 0/1 integers.
    if isinstance(value, bool):
        return int(value)
    return value


def _order_clause(sort: str | None) -> str:
    sort = (sort or "-created_date").strip()
    direction = "ASC"
    if sort.startswith("-"):
        direction = "DESC"
        sort = sort[1:]
    if not sort.replace("_", "").isalnum():
        raise ValueError(f"Invalid sort field: {sort}")
    if sort in _COLUMN_FIELDS:
        return f"ORDER BY {sort} {direction}"
    return f"ORDER BY json_extract(data, '$.{sort}') {direction}"


_FILTER_OPS = {"$gte": ">=", "$gt": ">", "$lte": "<=", "$lt": "<", "$ne": "!="}


def query_entities(
    etype: str,
    filters: dict[str, Any] | None = None,
    sort: str | None = None,
    limit: int | None = None,
    skip: int = 0,
) -> list[dict[str, Any]]:
    where = ["type = ?"]
    params: list[Any] = [etype]
    for key, value in (filters or {}).items():
        if not str(key).replace("_", "").isalnum():
            raise ValueError(f"Invalid filter field: {key}")
        lhs = key if key in _COLUMN_FIELDS else f"json_extract(data, '$.{key}')"
        if isinstance(value, dict) and any(str(k).startswith("$") for k in value):
            # Mongo-style operators, as the Base44 SDK supported
            for op, op_value in value.items():
                if op == "$in":
                    if not isinstance(op_value, list) or not op_value:
                        raise ValueError(f"$in requires a non-empty list for {key}")
                    where.append(f"{lhs} IN ({','.join('?' * len(op_value))})")
                    params.extend(_filter_value(v) for v in op_value)
                elif op in _FILTER_OPS:
                    where.append(f"{lhs} {_FILTER_OPS[op]} ?")
                    params.append(_filter_value(op_value))
                else:
                    raise ValueError(f"Unsupported filter operator: {op}")
        elif value is None:
            where.append(f"{lhs} IS NULL")
        else:
            where.append(f"{lhs} = ?")
            params.append(_filter_value(value))
    sql = f"SELECT * FROM entities WHERE {' AND '.join(where)} {_order_clause(sort)}"
    sql += " LIMIT ? OFFSET ?"
    params.extend([int(limit) if limit else -1, int(skip or 0)])
    with connect() as db:
        rows = db.execute(sql, params).fetchall()
    return [_row_to_record(r) for r in rows]


def create_entity(etype: str, data: dict[str, Any]) -> dict[str, Any]:
    return bulk_create_entities(etype, [data])[0]


def bulk_create_entities(etype: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = _now_iso()
    created = []
    rows = []
    for data in records:
        data = {k: v for k, v in dict(data).items() if k not in _COLUMN_FIELDS}
        rid = uuid.uuid4().hex
        rows.append((rid, etype, json.dumps(data), now, now))
        created.append({**data, "id": rid, "created_date": now, "updated_date": now})
    with connect() as db:
        db.executemany(
            "INSERT INTO entities (id, type, data, created_date, updated_date) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        db.commit()
    return created


def update_entity(etype: str, rid: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM entities WHERE id=? AND type=?", (rid, etype)).fetchone()
        if not row:
            return None
        data = json.loads(row["data"])
        data.update({k: v for k, v in patch.items() if k not in _COLUMN_FIELDS})
        now = _now_iso()
        db.execute(
            "UPDATE entities SET data=?, updated_date=? WHERE id=?",
            (json.dumps(data), now, rid),
        )
        db.commit()
    return {**data, "id": rid, "created_date": row["created_date"], "updated_date": now}


def delete_entity(etype: str, rid: str) -> bool:
    with connect() as db:
        cur = db.execute("DELETE FROM entities WHERE id=? AND type=?", (rid, etype))
        db.commit()
    return cur.rowcount > 0


def delete_entities_where(etype: str, filters: dict[str, Any]) -> int:
    """Bulk delete helper for source-scoped backfills (Base44 had to loop)."""
    where = ["type = ?"]
    params: list[Any] = [etype]
    for key, value in filters.items():
        if not str(key).replace("_", "").isalnum():
            raise ValueError(f"Invalid filter field: {key}")
        where.append(f"json_extract(data, '$.{key}') = ?")
        params.append(_filter_value(value))
    with connect() as db:
        cur = db.execute(f"DELETE FROM entities WHERE {' AND '.join(where)}", params)
        db.commit()
    return cur.rowcount
