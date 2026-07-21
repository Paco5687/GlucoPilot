import json
import sqlite3

import pytest

from server.data_audit import audit_database, connect_read_only


def _sample_database(path):
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE entities (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                data TEXT NOT NULL,
                created_date TEXT NOT NULL,
                updated_date TEXT NOT NULL
            )
            """
        )
        connection.execute("CREATE INDEX idx_entities_type ON entities(type)")
        connection.execute(
            "CREATE INDEX idx_entities_type_ts "
            "ON entities(type, json_extract(data, '$.timestamp'))"
        )
        connection.executemany(
            "INSERT INTO entities VALUES (?, ?, ?, ?, ?)",
            [
                (
                    "a",
                    "GlucoseReading",
                    json.dumps(
                        {
                            "timestamp": "2026-07-21T12:00:00Z",
                            "value": 123,
                            "owner_email": "private@example.com",
                        }
                    ),
                    "2026-07-21T12:00:00Z",
                    "2026-07-21T12:00:00Z",
                ),
                (
                    "b",
                    "GlucoseReading",
                    json.dumps(
                        {
                            "timestamp": "2026-07-21T12:05:00Z",
                            "value": 125,
                            "owner_email": "private@example.com",
                        }
                    ),
                    "2026-07-21T12:05:00Z",
                    "2026-07-21T12:05:00Z",
                ),
            ],
        )


def test_audit_reports_structure_without_values(tmp_path):
    database = tmp_path / "app.sqlite3"
    _sample_database(database)

    report = audit_database(database, repeat=1)

    assert report["entity_counts"] == {"GlucoseReading": 2}
    assert report["fields"]["GlucoseReading"]["timestamp"] == {
        "present": 2,
        "types": ["text"],
    }
    serialized = json.dumps(report)
    assert "private@example.com" not in serialized
    assert "2026-07-21T12:00:00Z" not in serialized
    assert '"value": 123' not in serialized


def test_audit_connection_is_read_only(tmp_path):
    database = tmp_path / "app.sqlite3"
    _sample_database(database)

    with connect_read_only(database) as connection:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM entities")


def test_audit_rejects_non_entity_database(tmp_path):
    database = tmp_path / "other.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE something_else (id INTEGER)")

    with pytest.raises(ValueError, match="entities table"):
        audit_database(database, repeat=1)
