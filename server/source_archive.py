"""Immutable, scrubbed raw-source archive backed by typed SQLite tables.

The archive is additive and disabled for source integrations by default. Raw
payloads are recursively scrubbed before canonical hashing and deterministic
gzip compression. File entries store references and hashes only; document bytes
remain in the existing records directory.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Iterator

from . import db
from .data_contracts import DEPLOYMENT_OWNER_ID

_REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = {
    "accesstoken",
    "apikey",
    "authorization",
    "clientsecret",
    "cookie",
    "credential",
    "credentials",
    "githubtoken",
    "idtoken",
    "password",
    "passwd",
    "privatekey",
    "refreshtoken",
    "secret",
    "setcookie",
    "token",
}
_AUTH_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:access_token|refresh_token|id_token|api_key|client_secret|password)=)[^&\s]+"
)
_ASSIGNMENT_SECRET_RE = re.compile(
    r"(?i)\b(access_token|refresh_token|id_token|api_key|client_secret|password)"
    r"\b[\"']?\s*[:=]\s*[\"']?[^\"'\s,;&}]+"
)
_VALID_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FINAL_STATUSES = {"succeeded", "failed", "partial"}
_RUN_KINDS = {"archive", "connector", "upload", "ingest", "reprocess"}
_TRIGGER_TYPES = {"unknown", "scheduled", "manual", "backfill", "upload", "ingest", "reprocess"}


class SourceArchiveError(RuntimeError):
    """Raised when archive input violates privacy, size, or lifecycle rules."""


@dataclass(frozen=True)
class ArchivePolicy:
    retention_days: int = 90
    max_payload_bytes: int = 2 * 1024 * 1024
    compression_level: int = 6

    def __post_init__(self) -> None:
        if self.retention_days < 1:
            raise ValueError("retention_days must be positive")
        if self.max_payload_bytes < 1:
            raise ValueError("max_payload_bytes must be positive")
        if not 0 <= self.compression_level <= 9:
            raise ValueError("compression_level must be between 0 and 9")

    @classmethod
    def from_environment(cls) -> ArchivePolicy:
        return cls(
            retention_days=_positive_env("SOURCE_ARCHIVE_RETENTION_DAYS", 90),
            max_payload_bytes=_positive_env("SOURCE_ARCHIVE_MAX_PAYLOAD_BYTES", 2 * 1024 * 1024),
        )


def source_archive_enabled() -> bool:
    return os.getenv("SOURCE_ARCHIVE_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _positive_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as error:
        raise SourceArchiveError(f"{name} must be an integer") from error
    if value < 1:
        raise SourceArchiveError(f"{name} must be positive")
    return value


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _timestamp(value: str | None, *, default_now: bool = False) -> str | None:
    if value is None and default_now:
        return _now_iso()
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise SourceArchiveError("archive timestamps must be ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SourceArchiveError("archive timestamps must include a UTC offset")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _label(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise SourceArchiveError(f"{field} is required")
    if len(normalized) > 200:
        raise SourceArchiveError(f"{field} is too long")
    if _scrub_text(normalized) != normalized:
        raise SourceArchiveError(f"{field} contains credential material")
    return normalized


def _sensitive_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return normalized in _SENSITIVE_KEYS or normalized.endswith(("password", "secret", "token", "apikey"))


def _scrub_text(value: str) -> str:
    value = _AUTH_RE.sub(lambda match: f"{match.group(1)} {_REDACTED}", value)
    value = _QUERY_SECRET_RE.sub(lambda match: f"{match.group(1)}{_REDACTED}", value)
    return _ASSIGNMENT_SECRET_RE.sub(lambda match: f"{match.group(1)}={_REDACTED}", value)


def scrub_payload(value: Any) -> Any:
    """Return JSON-compatible data with credential-bearing fields redacted."""
    if isinstance(value, dict):
        return {str(key): _REDACTED if _sensitive_key(key) else scrub_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_payload(item) for item in value]
    if isinstance(value, str):
        return _scrub_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise SourceArchiveError(f"payload value type is not JSON-compatible: {type(value).__name__}")


def _canonical_payload(payload: Any, policy: ArchivePolicy) -> tuple[bytes, bytes, str]:
    try:
        canonical = json.dumps(
            scrub_payload(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise SourceArchiveError(f"payload is not valid JSON: {error}") from error
    if len(canonical) > policy.max_payload_bytes:
        raise SourceArchiveError(f"scrubbed payload exceeds {policy.max_payload_bytes} byte archive limit")
    payload_hash = "sha256:" + hashlib.sha256(canonical).hexdigest()
    compressed = gzip.compress(canonical, compresslevel=policy.compression_level, mtime=0)
    return canonical, compressed, payload_hash


def _validate_hash(value: str, field: str) -> str:
    normalized = str(value or "").lower()
    if not _VALID_HASH_RE.fullmatch(normalized):
        raise SourceArchiveError(f"{field} must be a sha256: digest")
    return normalized


def _safe_relative_path(value: str) -> str:
    normalized = str(value or "").replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if not normalized or path.as_posix() == "." or path.is_absolute() or ".." in path.parts:
        raise SourceArchiveError("source file path must be a safe relative path")
    return path.as_posix()


class SqliteSourceArchiveRepository:
    """Typed archive repository; caller-owned connections participate in a UoW."""

    def __init__(
        self,
        connection: sqlite3.Connection | None = None,
        policy: ArchivePolicy | None = None,
    ) -> None:
        self._connection = connection
        self.policy = policy or ArchivePolicy.from_environment()

    @contextmanager
    def _scope(self) -> Iterator[sqlite3.Connection]:
        if self._connection is not None:
            yield self._connection
            return
        connection = db.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def start_sync_run(
        self,
        source_type: str,
        parser_version: str,
        *,
        started_at: str | None = None,
        run_kind: str = "archive",
        trigger_type: str = "unknown",
        connector_version: str = "legacy",
    ) -> dict[str, Any]:
        source_type = _label(source_type, "source_type")
        parser_version = _label(parser_version, "parser_version")
        connector_version = _label(connector_version, "connector_version")
        if run_kind not in _RUN_KINDS:
            raise SourceArchiveError(f"invalid run kind: {run_kind}")
        if trigger_type not in _TRIGGER_TYPES:
            raise SourceArchiveError(f"invalid trigger type: {trigger_type}")
        started_at = _timestamp(started_at, default_now=True)
        run_id = "sync_" + uuid.uuid4().hex
        with self._scope() as connection:
            connection.execute(
                """
                INSERT INTO sync_runs (
                    id, owner_id, source_type, parser_version, status, started_at,
                    created_at, run_kind, trigger_type, connector_version
                ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    DEPLOYMENT_OWNER_ID,
                    source_type,
                    parser_version,
                    started_at,
                    _now_iso(),
                    run_kind,
                    trigger_type,
                    connector_version,
                ),
            )
            return self._sync_run(connection, run_id)

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
    ) -> dict[str, Any]:
        if status not in _FINAL_STATUSES:
            raise SourceArchiveError(f"invalid final sync status: {status}")
        safe_error = _scrub_text(str(error_summary))[:1000] if error_summary else None
        counters = {
            "fetched_count": fetched_count,
            "created_count": created_count,
            "updated_count": updated_count,
            "skipped_count": skipped_count,
            "failed_count": failed_count,
            "stale_count": stale_count,
        }
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in counters.values()):
            raise SourceArchiveError("sync counters must be non-negative integers")
        successful_data = _timestamp(last_successful_data_at) if status == "succeeded" else None
        with self._scope() as connection:
            cursor = connection.execute(
                """
                UPDATE sync_runs
                SET status=?, completed_at=?, error_summary=?,
                    fetched_count=?, created_count=?, updated_count=?, skipped_count=?,
                    failed_count=?, stale_count=?, last_successful_data_at=?
                WHERE id=? AND owner_id=? AND status='running'
                """,
                (
                    status,
                    _timestamp(completed_at, default_now=True),
                    safe_error,
                    fetched_count,
                    created_count,
                    updated_count,
                    skipped_count,
                    failed_count,
                    stale_count,
                    successful_data,
                    run_id,
                    DEPLOYMENT_OWNER_ID,
                ),
            )
            if cursor.rowcount != 1:
                raise SourceArchiveError("sync run does not exist or is already complete")
            return self._sync_run(connection, run_id)

    def link_entity(
        self,
        entity_type: str,
        entity_id: str,
        sync_run_id: str,
        parser_version: str,
        *,
        source_record_id: str | None = None,
        source_file_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        entity_type = _label(entity_type, "entity_type")
        entity_id = _label(entity_id, "entity_id")
        parser_version = _label(parser_version, "parser_version")
        if bool(source_record_id) == bool(source_file_id):
            raise SourceArchiveError("exactly one source record or source file is required")
        evidence_kind = "record" if source_record_id else "file"
        evidence_id = source_record_id or source_file_id
        link_id = "link_" + hashlib.sha256(
            f"{DEPLOYMENT_OWNER_ID}\0{entity_type}\0{entity_id}\0{evidence_kind}\0{evidence_id}\0{sync_run_id}".encode()
        ).hexdigest()
        with self._scope() as connection:
            self._require_sync_run_exists(connection, sync_run_id)
            evidence_table = "source_records" if source_record_id else "source_files"
            if not connection.execute(
                f"SELECT 1 FROM {evidence_table} WHERE id=? AND owner_id=?",
                (evidence_id, DEPLOYMENT_OWNER_ID),
            ).fetchone():
                raise SourceArchiveError("source evidence does not exist")
            cursor = connection.execute(
                """
                INSERT INTO normalized_source_links (
                    id, owner_id, entity_type, entity_id, source_record_id,
                    source_file_id, sync_run_id, parser_version, linked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    link_id,
                    DEPLOYMENT_OWNER_ID,
                    entity_type,
                    entity_id,
                    source_record_id,
                    source_file_id,
                    sync_run_id,
                    parser_version,
                    _now_iso(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM normalized_source_links WHERE id=? AND owner_id=?",
                (link_id, DEPLOYMENT_OWNER_ID),
            ).fetchone()
            return dict(row), cursor.rowcount == 1

    def archive_payload(
        self,
        source_type: str,
        payload: Any,
        parser_version: str,
        *,
        external_id: str | None = None,
        observed_at: str | None = None,
        received_at: str | None = None,
        sync_run_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        source_type = _label(source_type, "source_type")
        parser_version = _label(parser_version, "parser_version")
        canonical, compressed, payload_hash = _canonical_payload(payload, self.policy)
        record_id = (
            "src_" + hashlib.sha256(f"{DEPLOYMENT_OWNER_ID}\0{source_type}\0{payload_hash}".encode()).hexdigest()
        )
        received = _timestamp(received_at, default_now=True)
        observed = _timestamp(observed_at)
        with self._scope() as connection:
            self._require_sync_run(connection, sync_run_id)
            cursor = connection.execute(
                """
                INSERT INTO source_records (
                    id, owner_id, source_type, external_id, observed_at, received_at,
                    payload_hash, parser_version, sync_run_id, content_encoding,
                    payload, uncompressed_bytes, stored_bytes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'json+gzip', ?, ?, ?, ?)
                ON CONFLICT(owner_id, source_type, payload_hash) DO NOTHING
                """,
                (
                    record_id,
                    DEPLOYMENT_OWNER_ID,
                    source_type,
                    _scrub_text(str(external_id))[:500] if external_id is not None else None,
                    observed,
                    received,
                    payload_hash,
                    parser_version,
                    sync_run_id,
                    compressed,
                    len(canonical),
                    len(compressed),
                    _now_iso(),
                ),
            )
            created = cursor.rowcount == 1
            if sync_run_id:
                connection.execute(
                    """
                    UPDATE sync_runs SET
                        records_seen=records_seen + 1,
                        records_archived=records_archived + ?,
                        records_deduplicated=records_deduplicated + ?,
                        bytes_received=bytes_received + ?
                    WHERE id=? AND owner_id=?
                    """,
                    (
                        int(created),
                        int(not created),
                        len(canonical),
                        sync_run_id,
                        DEPLOYMENT_OWNER_ID,
                    ),
                )
            return self._source_record(connection, source_type, payload_hash), created

    def register_file(
        self,
        source_type: str,
        relative_path: str,
        file_hash: str,
        byte_size: int,
        parser_version: str,
        *,
        external_id: str | None = None,
        observed_at: str | None = None,
        received_at: str | None = None,
        mime_type: str | None = None,
        sync_run_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        source_type = _label(source_type, "source_type")
        parser_version = _label(parser_version, "parser_version")
        relative_path = _safe_relative_path(relative_path)
        file_hash = _validate_hash(file_hash, "file_hash")
        if int(byte_size) < 0:
            raise SourceArchiveError("byte_size cannot be negative")
        file_id = "file_" + hashlib.sha256(f"{DEPLOYMENT_OWNER_ID}\0{source_type}\0{file_hash}".encode()).hexdigest()
        with self._scope() as connection:
            self._require_sync_run(connection, sync_run_id)
            cursor = connection.execute(
                """
                INSERT INTO source_files (
                    id, owner_id, source_type, external_id, observed_at, received_at,
                    file_hash, relative_path, byte_size, mime_type,
                    parser_version, sync_run_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_id, source_type, file_hash) DO NOTHING
                """,
                (
                    file_id,
                    DEPLOYMENT_OWNER_ID,
                    source_type,
                    _scrub_text(str(external_id))[:500] if external_id is not None else None,
                    _timestamp(observed_at),
                    _timestamp(received_at, default_now=True),
                    file_hash,
                    relative_path,
                    int(byte_size),
                    _scrub_text(str(mime_type))[:200] if mime_type else None,
                    parser_version,
                    sync_run_id,
                    _now_iso(),
                ),
            )
            created = cursor.rowcount == 1
            if sync_run_id:
                connection.execute(
                    """
                    UPDATE sync_runs SET
                        files_seen=files_seen + 1,
                        files_archived=files_archived + ?,
                        files_deduplicated=files_deduplicated + ?
                    WHERE id=? AND owner_id=?
                    """,
                    (int(created), int(not created), sync_run_id, DEPLOYMENT_OWNER_ID),
                )
            row = connection.execute(
                "SELECT * FROM source_files WHERE owner_id=? AND source_type=? AND file_hash=?",
                (DEPLOYMENT_OWNER_ID, source_type, file_hash),
            ).fetchone()
            return dict(row), created

    def read_payload(self, record_id: str) -> Any:
        with self._scope() as connection:
            row = connection.execute(
                "SELECT payload, content_encoding FROM source_records WHERE id=? AND owner_id=?",
                (record_id, DEPLOYMENT_OWNER_ID),
            ).fetchone()
            if not row:
                raise SourceArchiveError("source record does not exist")
            if row["content_encoding"] != "json+gzip":
                raise SourceArchiveError("unsupported source payload encoding")
            return json.loads(gzip.decompress(row["payload"]))

    def links_for_entity(self, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
        entity_type = _label(entity_type, "entity_type")
        entity_id = _label(entity_id, "entity_id")
        with self._scope() as connection:
            rows = connection.execute(
                """
                SELECT * FROM normalized_source_links
                WHERE owner_id=? AND entity_type=? AND entity_id=?
                ORDER BY linked_at, id
                """,
                (DEPLOYMENT_OWNER_ID, entity_type, entity_id),
            ).fetchall()
            return [dict(row) for row in rows]

    def recent_sync_runs(
        self,
        source_type: str | None = None,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise SourceArchiveError("sync run limit must be between 1 and 500")
        with self._scope() as connection:
            if source_type is None:
                rows = connection.execute(
                    "SELECT * FROM sync_runs WHERE owner_id=? ORDER BY started_at DESC, id DESC LIMIT ?",
                    (DEPLOYMENT_OWNER_ID, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM sync_runs
                    WHERE owner_id=? AND source_type=?
                    ORDER BY started_at DESC, id DESC LIMIT ?
                    """,
                    (DEPLOYMENT_OWNER_ID, _label(source_type, "source_type"), limit),
                ).fetchall()
            return [dict(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        with self._scope() as connection:
            records = connection.execute(
                """
                SELECT COUNT(*) AS count,
                       COALESCE(SUM(uncompressed_bytes), 0) AS uncompressed_bytes,
                       COALESCE(SUM(stored_bytes), 0) AS stored_bytes,
                       MIN(received_at) AS oldest_received_at,
                       MAX(received_at) AS newest_received_at
                FROM source_records
                WHERE owner_id=?
                """,
                (DEPLOYMENT_OWNER_ID,),
            ).fetchone()
            files = connection.execute(
                """
                SELECT COUNT(*) AS count, COALESCE(SUM(byte_size), 0) AS referenced_bytes
                FROM source_files WHERE owner_id=?
                """,
                (DEPLOYMENT_OWNER_ID,),
            ).fetchone()
            runs = connection.execute(
                """
                SELECT COUNT(*) AS count,
                       COALESCE(SUM(records_deduplicated), 0) AS records_deduplicated,
                       COALESCE(SUM(files_deduplicated), 0) AS files_deduplicated,
                       COALESCE(SUM(status = 'succeeded'), 0) AS succeeded,
                       COALESCE(SUM(status = 'partial'), 0) AS partial,
                       COALESCE(SUM(status = 'failed'), 0) AS failed,
                       MAX(last_successful_data_at) AS last_successful_data_at,
                       MAX(completed_at) AS last_completed_at
                FROM sync_runs
                WHERE owner_id=?
                """,
                (DEPLOYMENT_OWNER_ID,),
            ).fetchone()
            links = connection.execute(
                "SELECT COUNT(*) AS count FROM normalized_source_links WHERE owner_id=?",
                (DEPLOYMENT_OWNER_ID,),
            ).fetchone()
        return {
            "enabled": source_archive_enabled(),
            "policy": {
                "retention_days": self.policy.retention_days,
                "max_payload_bytes": self.policy.max_payload_bytes,
                "compression": "gzip",
            },
            "records": dict(records),
            "files": dict(files),
            "sync_runs": dict(runs),
            "normalized_links": dict(links),
        }

    def prune_before(self, cutoff: str) -> dict[str, int]:
        cutoff = _timestamp(cutoff)
        with self._scope() as connection:
            records = connection.execute(
                "DELETE FROM source_records WHERE owner_id=? AND received_at < ?",
                (DEPLOYMENT_OWNER_ID, cutoff),
            ).rowcount
            files = connection.execute(
                "DELETE FROM source_files WHERE owner_id=? AND received_at < ?",
                (DEPLOYMENT_OWNER_ID, cutoff),
            ).rowcount
        return {"source_records": records, "source_files": files}

    @staticmethod
    def _require_sync_run(connection: sqlite3.Connection, run_id: str | None) -> None:
        if (
            run_id
            and not connection.execute(
                "SELECT 1 FROM sync_runs WHERE id=? AND owner_id=? AND status='running'",
                (run_id, DEPLOYMENT_OWNER_ID),
            ).fetchone()
        ):
            raise SourceArchiveError("sync run does not exist or is not running")

    @staticmethod
    def _require_sync_run_exists(connection: sqlite3.Connection, run_id: str) -> None:
        if not connection.execute(
            "SELECT 1 FROM sync_runs WHERE id=? AND owner_id=?",
            (run_id, DEPLOYMENT_OWNER_ID),
        ).fetchone():
            raise SourceArchiveError("sync run does not exist")

    @staticmethod
    def _sync_run(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM sync_runs WHERE id=? AND owner_id=?",
            (run_id, DEPLOYMENT_OWNER_ID),
        ).fetchone()
        if not row:
            raise SourceArchiveError("sync run does not exist")
        return dict(row)

    @staticmethod
    def _source_record(
        connection: sqlite3.Connection,
        source_type: str,
        payload_hash: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT id, owner_id, source_type, external_id, observed_at, received_at,
                   payload_hash, parser_version, sync_run_id, content_encoding,
                   uncompressed_bytes, stored_bytes, created_at
            FROM source_records WHERE owner_id=? AND source_type=? AND payload_hash=?
            """,
            (DEPLOYMENT_OWNER_ID, source_type, payload_hash),
        ).fetchone()
        return dict(row)
