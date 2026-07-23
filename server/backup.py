"""Consistent, verifiable backup and clean-target restore for GlucoPilot data."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote


MANIFEST_NAME = "manifest.json"
MANIFEST_CHECKSUM_NAME = "manifest.sha256"
DATABASE_NAME = "app.sqlite3"
RECORDS_NAME = "records"
FORMAT_VERSION = 1
MINIMUM_MARGIN_BYTES = 16 * 1024 * 1024


class BackupError(RuntimeError):
    """Raised when backup preflight, creation, verification, or restore fails."""


def _read_only_connection(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve(strict=True)
    uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_record_files(records_dir: Path) -> list[tuple[Path, Path, os.stat_result]]:
    if not records_dir.exists():
        return []
    if not records_dir.is_dir() or records_dir.is_symlink():
        raise BackupError(f"records path is not a safe directory: {records_dir}")
    files = []
    for path in sorted(records_dir.rglob("*")):
        if path.is_symlink():
            raise BackupError(f"record symlinks are not supported: {path}")
        if path.is_file():
            relative = path.relative_to(records_dir)
            files.append((path, relative, path.stat()))
    return files


def _file_signature(files: list[tuple[Path, Path, os.stat_result]]) -> list[tuple[str, int, int]]:
    return [(str(relative), stat.st_size, stat.st_mtime_ns) for _, relative, stat in files]


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _database_metadata(path: Path, *, include_references: bool = False) -> dict[str, Any]:
    try:
        with _read_only_connection(path) as connection:
            integrity_rows = [row[0] for row in connection.execute("PRAGMA integrity_check")]
            if integrity_rows != ["ok"]:
                raise BackupError(f"SQLite integrity check failed: {integrity_rows[:3]}")
            page_count = connection.execute("PRAGMA page_count").fetchone()[0]
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            entity_counts: dict[str, int] = {}
            references: set[str] = set()
            if _table_exists(connection, "entities"):
                entity_counts = dict(
                    connection.execute(
                        "SELECT type, COUNT(*) FROM entities GROUP BY type ORDER BY type"
                    )
                )
                if include_references:
                    references = {
                        row[0]
                        for row in connection.execute(
                            """
                            SELECT json_extract(data, '$.stored_as')
                            FROM entities
                            WHERE type='MedicalRecord'
                              AND json_extract(data, '$.stored_as') IS NOT NULL
                              AND json_extract(data, '$.stored_as') != ''
                            """
                        )
                    }
            migrations = []
            if _table_exists(connection, "schema_migrations"):
                migrations = [
                    {"version": row[0], "name": row[1], "checksum": row[2]}
                    for row in connection.execute(
                        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
                    )
                ]
            source_archive = None
            if all(
                _table_exists(connection, table)
                for table in ("source_records", "source_files", "sync_runs")
            ):
                source_archive = {
                    "source_records": dict(
                        connection.execute(
                            """
                            SELECT COUNT(*) AS count,
                                   COALESCE(SUM(uncompressed_bytes), 0) AS uncompressed_bytes,
                                   COALESCE(SUM(stored_bytes), 0) AS stored_bytes
                            FROM source_records
                            """
                        ).fetchone()
                    ),
                    "source_files": dict(
                        connection.execute(
                            """
                            SELECT COUNT(*) AS count,
                                   COALESCE(SUM(byte_size), 0) AS referenced_bytes
                            FROM source_files
                            """
                        ).fetchone()
                    ),
                    "sync_runs": dict(
                        connection.execute(
                            "SELECT COUNT(*) AS count FROM sync_runs"
                        ).fetchone()
                    ),
                }
                if _table_exists(connection, "normalized_source_links"):
                    source_archive["normalized_source_links"] = dict(
                        connection.execute(
                            "SELECT COUNT(*) AS count FROM normalized_source_links"
                        ).fetchone()
                    )
            canonical_time = None
            if _table_exists(connection, "canonical_times"):
                canonical_time = dict(
                    connection.execute(
                        """
                        SELECT COUNT(*) AS count,
                               COALESCE(SUM(CASE WHEN timeline_at IS NOT NULL THEN 1 ELSE 0 END), 0)
                                   AS resolved_count,
                               COALESCE(SUM(CASE WHEN normalization_status IN
                                        ('ambiguous', 'nonexistent', 'invalid') THEN 1 ELSE 0 END), 0)
                                   AS unresolved_count
                        FROM canonical_times
                        """
                    ).fetchone()
                )
            typed_treatments = None
            if all(
                _table_exists(connection, table)
                for table in ("typed_treatments", "basal_segments", "pump_daily_totals")
            ):
                typed_treatments = {
                    table: dict(
                        connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
                    )
                    for table in ("typed_treatments", "basal_segments", "pump_daily_totals")
                }
            lab_audit = None
            if all(
                _table_exists(connection, table)
                for table in (
                    "lab_extraction_runs",
                    "lab_extraction_observations",
                    "lab_verification_events",
                )
            ):
                lab_audit = {
                    "runs": dict(
                        connection.execute(
                            "SELECT COUNT(*) AS count FROM lab_extraction_runs"
                        ).fetchone()
                    ),
                    "observations": dict(
                        connection.execute(
                            """
                            SELECT COUNT(*) AS count,
                                   COALESCE(SUM(CASE WHEN verification_status IN
                                       ('approved', 'edited') THEN 1 ELSE 0 END), 0)
                                       AS verified_count
                            FROM lab_extraction_observations
                            """
                        ).fetchone()
                    ),
                    "verification_events": dict(
                        connection.execute(
                            "SELECT COUNT(*) AS count FROM lab_verification_events"
                        ).fetchone()
                    ),
                }
            contradiction_ledger = None
            if all(
                _table_exists(connection, table)
                for table in ("contradiction_runs", "contradictions", "contradiction_events")
            ):
                contradiction_ledger = {
                    "runs": dict(
                        connection.execute(
                            "SELECT COUNT(*) AS count FROM contradiction_runs"
                        ).fetchone()
                    ),
                    "contradictions": dict(
                        connection.execute(
                            """
                            SELECT COUNT(*) AS count,
                                   COALESCE(SUM(CASE WHEN resolution_state='unresolved' THEN 1 ELSE 0 END), 0)
                                       AS unresolved_count,
                                   COALESCE(SUM(CASE WHEN resolution_state='unresolved' AND severity='blocking'
                                       THEN 1 ELSE 0 END), 0) AS unresolved_blocking_count
                            FROM contradictions
                            """
                        ).fetchone()
                    ),
                    "events": dict(
                        connection.execute(
                            "SELECT COUNT(*) AS count FROM contradiction_events"
                        ).fetchone()
                    ),
                }
            typed_glucose = None
            if all(
                _table_exists(connection, table)
                for table in ("glucose_readings", "fingerstick_readings")
            ):
                typed_glucose = {
                    table: dict(
                        connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
                    )
                    for table in ("glucose_readings", "fingerstick_readings")
                }
            typed_wearables = None
            if all(
                _table_exists(connection, table)
                for table in ("wearable_daily", "wearable_samples")
            ):
                typed_wearables = {
                    table: dict(
                        connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
                    )
                    for table in ("wearable_daily", "wearable_samples")
                }
            relationship_projection = None
            relationship_tables = (
                "entity_relationships",
                "relationship_predicate_registry",
                "assertion_status_registry",
                "evidence_level_registry",
                "relationship_algorithm_registry",
            )
            if all(_table_exists(connection, table) for table in relationship_tables):
                relationship_projection = {
                    table: dict(
                        connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
                    )
                    for table in relationship_tables
                }
            relationship_builds = None
            relationship_build_tables = (
                "relationship_projection_runs",
                "relationship_projection_run_edges",
                "relationship_projection_active_edges",
                "relationship_projection_state",
            )
            if all(_table_exists(connection, table) for table in relationship_build_tables):
                relationship_builds = {
                    table: dict(
                        connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
                    )
                    for table in relationship_build_tables
                }
            evidence_projection = None
            evidence_tables = ("observation_windows", "evidence_sets", "evidence_set_windows")
            if all(_table_exists(connection, table) for table in evidence_tables):
                evidence_projection = {
                    table: dict(
                        connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
                    )
                    for table in evidence_tables
                }
            claim_projection = None
            claim_tables = ("claim_algorithm_registry", "claim_versions")
            if all(_table_exists(connection, table) for table in claim_tables):
                claim_projection = {
                    table: dict(
                        connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
                    )
                    for table in claim_tables
                }
            hypothesis_ledger = None
            hypothesis_tables = (
                "health_hypotheses",
                "hypothesis_evidence",
                "hypothesis_events",
            )
            if all(_table_exists(connection, table) for table in hypothesis_tables):
                hypothesis_ledger = {
                    "hypotheses": dict(
                        connection.execute(
                            """
                            SELECT COUNT(*) AS count,
                                   COALESCE(SUM(CASE WHEN status='proposed' THEN 1 ELSE 0 END), 0)
                                       AS proposed_count,
                                   COALESCE(SUM(CASE WHEN status='under_review' THEN 1 ELSE 0 END), 0)
                                       AS under_review_count,
                                   COALESCE(SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END), 0)
                                       AS confirmed_count,
                                   COALESCE(SUM(CASE WHEN status='ruled_against' THEN 1 ELSE 0 END), 0)
                                       AS ruled_against_count
                            FROM health_hypotheses
                            """
                        ).fetchone()
                    ),
                    "evidence": dict(
                        connection.execute(
                            "SELECT COUNT(*) AS count FROM hypothesis_evidence"
                        ).fetchone()
                    ),
                    "events": dict(
                        connection.execute(
                            "SELECT COUNT(*) AS count FROM hypothesis_events"
                        ).fetchone()
                    ),
                }
            episode_ledger = None
            episode_tables = (
                "health_episodes",
                "episode_members",
                "episode_events",
                "medication_exposures",
                "medication_exposure_events",
            )
            if all(_table_exists(connection, table) for table in episode_tables):
                episode_ledger = {
                    "episodes": dict(
                        connection.execute(
                            """
                            SELECT COUNT(*) AS count,
                                   COALESCE(SUM(CASE WHEN status='proposed' THEN 1 ELSE 0 END), 0)
                                       AS proposed_count,
                                   COALESCE(SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END), 0)
                                       AS confirmed_count,
                                   COALESCE(SUM(CASE WHEN status='dismissed' THEN 1 ELSE 0 END), 0)
                                       AS dismissed_count
                            FROM health_episodes
                            """
                        ).fetchone()
                    ),
                    "members": dict(
                        connection.execute(
                            "SELECT COUNT(*) AS count FROM episode_members"
                        ).fetchone()
                    ),
                    "events": dict(
                        connection.execute(
                            "SELECT COUNT(*) AS count FROM episode_events"
                        ).fetchone()
                    ),
                    "medication_exposures": dict(
                        connection.execute(
                            """
                            SELECT COUNT(*) AS count,
                                   COALESCE(SUM(CASE WHEN end_time IS NULL THEN 1 ELSE 0 END), 0)
                                       AS open_ended_count
                            FROM medication_exposures
                            """
                        ).fetchone()
                    ),
                    "medication_exposure_events": dict(
                        connection.execute(
                            "SELECT COUNT(*) AS count FROM medication_exposure_events"
                        ).fetchone()
                    ),
                }
            activity_position_ledger = None
            activity_position_tables = (
                "activity_position_intervals",
                "activity_position_events",
            )
            if all(
                _table_exists(connection, table)
                for table in activity_position_tables
            ):
                activity_position_ledger = {
                    "intervals": dict(
                        connection.execute(
                            """
                            SELECT COUNT(*) AS count,
                                   COALESCE(SUM(CASE WHEN origin_kind='manual' THEN 1 ELSE 0 END), 0)
                                       AS manual_count,
                                   COALESCE(SUM(CASE WHEN origin_kind='wearable' THEN 1 ELSE 0 END), 0)
                                       AS wearable_count,
                                   COALESCE(SUM(CASE WHEN correction_of_id IS NOT NULL THEN 1 ELSE 0 END), 0)
                                       AS correction_count
                            FROM activity_position_intervals
                            """
                        ).fetchone()
                    ),
                    "events": dict(
                        connection.execute(
                            "SELECT COUNT(*) AS count FROM activity_position_events"
                        ).fetchone()
                    ),
                }
            management_burden_ledger = None
            management_burden_tables = (
                "management_burden_events",
                "management_burden_audit",
            )
            if all(
                _table_exists(connection, table)
                for table in management_burden_tables
            ):
                management_burden_ledger = {
                    "events": dict(
                        connection.execute(
                            """
                            SELECT COUNT(*) AS count,
                                   COALESCE(SUM(CASE WHEN origin_kind='observed' THEN 1 ELSE 0 END), 0)
                                       AS observed_count,
                                   COALESCE(SUM(CASE WHEN origin_kind='inferred' THEN 1 ELSE 0 END), 0)
                                       AS inferred_count,
                                   COALESCE(SUM(CASE WHEN origin_kind='manual' THEN 1 ELSE 0 END), 0)
                                       AS manual_count,
                                   COALESCE(SUM(CASE WHEN origin_kind='correction' THEN 1 ELSE 0 END), 0)
                                       AS correction_count,
                                   COALESCE(SUM(excluded), 0) AS excluded_count
                            FROM management_burden_events
                            """
                        ).fetchone()
                    ),
                    "audit": dict(
                        connection.execute(
                            "SELECT COUNT(*) AS count FROM management_burden_audit"
                        ).fetchone()
                    ),
                }
            clinical_review_ledger = None
            clinical_review_tables = (
                "clinical_review_threads",
                "clinical_review_events",
            )
            if all(
                _table_exists(connection, table)
                for table in clinical_review_tables
            ):
                clinical_review_ledger = {
                    "threads": dict(
                        connection.execute(
                            """
                            SELECT COUNT(*) AS count,
                                   COALESCE(SUM(CASE WHEN owner_status='pending' THEN 1 ELSE 0 END), 0)
                                       AS pending_count,
                                   COALESCE(SUM(CASE WHEN owner_status='accepted' THEN 1 ELSE 0 END), 0)
                                       AS accepted_count,
                                   COALESCE(SUM(CASE WHEN owner_status='disputed' THEN 1 ELSE 0 END), 0)
                                       AS disputed_count
                            FROM clinical_review_threads
                            """
                        ).fetchone()
                    ),
                    "events": dict(
                        connection.execute(
                            "SELECT COUNT(*) AS count FROM clinical_review_events"
                        ).fetchone()
                    ),
                }
    except BackupError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise BackupError(f"could not inspect SQLite database: {error}") from error

    metadata = {
        "integrity_check": "ok",
        "logical_bytes": page_count * page_size,
        "entity_total": sum(entity_counts.values()),
        "entity_counts": entity_counts,
        "migrations": migrations,
    }
    if source_archive is not None:
        metadata["source_archive"] = source_archive
    if canonical_time is not None:
        metadata["canonical_time"] = canonical_time
    if typed_treatments is not None:
        metadata["typed_treatments"] = typed_treatments
    if lab_audit is not None:
        metadata["lab_audit"] = lab_audit
    if contradiction_ledger is not None:
        metadata["contradiction_ledger"] = contradiction_ledger
    if typed_glucose is not None:
        metadata["typed_glucose"] = typed_glucose
    if typed_wearables is not None:
        metadata["typed_wearables"] = typed_wearables
    if relationship_projection is not None:
        metadata["relationship_projection"] = relationship_projection
    if relationship_builds is not None:
        metadata["relationship_builds"] = relationship_builds
    if evidence_projection is not None:
        metadata["evidence_projection"] = evidence_projection
    if claim_projection is not None:
        metadata["claim_projection"] = claim_projection
    if hypothesis_ledger is not None:
        metadata["hypothesis_ledger"] = hypothesis_ledger
    if episode_ledger is not None:
        metadata["episode_ledger"] = episode_ledger
    if activity_position_ledger is not None:
        metadata["activity_position_ledger"] = activity_position_ledger
    if management_burden_ledger is not None:
        metadata["management_burden_ledger"] = management_burden_ledger
    if clinical_review_ledger is not None:
        metadata["clinical_review_ledger"] = clinical_review_ledger
    if include_references:
        metadata["record_references"] = references
    return metadata


def required_backup_bytes(database_bytes: int, record_bytes: int) -> int:
    payload = database_bytes + record_bytes
    return payload + max(MINIMUM_MARGIN_BYTES, payload // 10)


def preflight_backup(
    data_dir: Path,
    backup_root: Path,
    *,
    available_bytes: int | None = None,
) -> dict[str, Any]:
    """Validate source integrity, paths, and destination capacity without writes."""
    data_dir = data_dir.expanduser().resolve(strict=True)
    database = data_dir / DATABASE_NAME
    if not database.is_file() or database.is_symlink():
        raise BackupError(f"database is missing or unsafe: {database}")

    records_dir = data_dir / RECORDS_NAME
    root_resolved = backup_root.expanduser().resolve()
    records_resolved = records_dir.resolve()
    if root_resolved == records_resolved or records_resolved in root_resolved.parents:
        raise BackupError("backup destination cannot be inside the records directory")

    metadata = _database_metadata(database)
    record_files = _safe_record_files(records_dir)
    record_bytes = sum(stat.st_size for _, _, stat in record_files)
    required = required_backup_bytes(metadata["logical_bytes"], record_bytes)

    if available_bytes is None:
        probe = root_resolved
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            available_bytes = shutil.disk_usage(probe).free
        except OSError as error:
            raise BackupError(f"could not inspect backup destination capacity: {error}") from error
    if available_bytes < required:
        raise BackupError(
            f"insufficient backup space: need {required} bytes, have {available_bytes} bytes"
        )

    return {
        "database_logical_bytes": metadata["logical_bytes"],
        "entity_total": metadata["entity_total"],
        "record_file_count": len(record_files),
        "record_bytes": record_bytes,
        "required_bytes": required,
        "available_bytes": available_bytes,
        "integrity_check": metadata["integrity_check"],
    }


def _copy_database(source_path: Path, destination_path: Path) -> None:
    try:
        with _read_only_connection(source_path) as source:
            destination = sqlite3.connect(destination_path)
            try:
                source.backup(destination)
            finally:
                destination.close()
    except (OSError, sqlite3.Error) as error:
        raise BackupError(f"SQLite online backup failed: {error}") from error
    destination_path.chmod(0o600)


def _copy_records(
    files: list[tuple[Path, Path, os.stat_result]], destination: Path
) -> list[dict[str, Any]]:
    manifest_files = []
    if files:
        destination.mkdir(parents=True, mode=0o700)
    for source, relative, _ in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(source, target)
        target.chmod(0o600)
        manifest_files.append(
            {"path": relative.as_posix(), "bytes": target.stat().st_size, "sha256": _sha256(target)}
        )
    return manifest_files


def _write_manifest(directory: Path, manifest: dict[str, Any]) -> None:
    manifest_path = directory / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest_path.chmod(0o600)
    checksum_path = directory / MANIFEST_CHECKSUM_NAME
    checksum_path.write_text(_sha256(manifest_path) + "\n", encoding="ascii")
    checksum_path.chmod(0o600)


def _load_manifest(backup_dir: Path) -> dict[str, Any]:
    manifest_path = backup_dir / MANIFEST_NAME
    checksum_path = backup_dir / MANIFEST_CHECKSUM_NAME
    if not manifest_path.is_file() or not checksum_path.is_file():
        raise BackupError("backup manifest or checksum is missing")
    expected = checksum_path.read_text(encoding="ascii").strip()
    if not expected or _sha256(manifest_path) != expected:
        raise BackupError("backup manifest checksum mismatch")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BackupError(f"backup manifest is unreadable: {error}") from error
    if manifest.get("format_version") != FORMAT_VERSION:
        raise BackupError(f"unsupported backup format: {manifest.get('format_version')!r}")
    return manifest


def _validate_backup_files(backup_dir: Path) -> dict[str, Any]:
    manifest = _load_manifest(backup_dir)
    database_path = backup_dir / DATABASE_NAME
    database = manifest.get("database") or {}
    if not database_path.is_file() or database_path.is_symlink():
        raise BackupError("backup database is missing or unsafe")
    if database_path.stat().st_size != database.get("bytes"):
        raise BackupError("backup database size mismatch")
    if _sha256(database_path) != database.get("sha256"):
        raise BackupError("backup database checksum mismatch")

    expected_files = {
        item["path"]: item for item in (manifest.get("records") or {}).get("files", [])
    }
    records_dir = backup_dir / RECORDS_NAME
    actual_files = _safe_record_files(records_dir)
    actual_names = {relative.as_posix() for _, relative, _ in actual_files}
    if actual_names != set(expected_files):
        raise BackupError("backup record-file inventory mismatch")
    for path, relative, stat in actual_files:
        expected = expected_files[relative.as_posix()]
        if stat.st_size != expected.get("bytes") or _sha256(path) != expected.get("sha256"):
            raise BackupError(f"backup record checksum mismatch: {relative.as_posix()}")
    return manifest


def restore_backup(backup_dir: Path, target_data_dir: Path) -> dict[str, Any]:
    """Restore into a new or empty data directory; never overwrite data."""
    backup_dir = backup_dir.expanduser().resolve(strict=True)
    target_data_dir = target_data_dir.expanduser().resolve()
    if target_data_dir.exists():
        if (
            not target_data_dir.is_dir()
            or target_data_dir.is_symlink()
            or any(target_data_dir.iterdir())
        ):
            raise BackupError(f"restore target must be a new or empty directory: {target_data_dir}")
    manifest = _validate_backup_files(backup_dir)
    required = required_backup_bytes(
        (manifest.get("database") or {}).get("bytes", 0),
        (manifest.get("records") or {}).get("total_bytes", 0),
    )
    probe = target_data_dir.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if shutil.disk_usage(probe).free < required:
        raise BackupError("insufficient free space for restore")

    created = not target_data_dir.exists()
    try:
        target_data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target_data_dir.chmod(0o700)
        database_target = target_data_dir / DATABASE_NAME
        shutil.copy2(backup_dir / DATABASE_NAME, database_target)
        database_target.chmod(0o600)
        source_records = backup_dir / RECORDS_NAME
        if source_records.exists():
            shutil.copytree(source_records, target_data_dir / RECORDS_NAME)
            for path in (target_data_dir / RECORDS_NAME).rglob("*"):
                path.chmod(0o700 if path.is_dir() else 0o600)
    except Exception as error:
        (target_data_dir / DATABASE_NAME).unlink(missing_ok=True)
        shutil.rmtree(target_data_dir / RECORDS_NAME, ignore_errors=True)
        if created:
            target_data_dir.rmdir()
        if isinstance(error, BackupError):
            raise
        raise BackupError(f"restore copy failed: {error}") from error
    return {
        "target": str(target_data_dir),
        "database_bytes": database_target.stat().st_size,
        "record_file_count": (manifest.get("records") or {}).get("file_count", 0),
    }


def _verify_restored_data(restored_data_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    metadata = _database_metadata(restored_data_dir / DATABASE_NAME, include_references=True)
    expected_database = manifest.get("database") or {}
    keys = ["entity_total", "entity_counts", "migrations"]
    if "source_archive" in expected_database:
        keys.append("source_archive")
    if "canonical_time" in expected_database:
        keys.append("canonical_time")
    if "typed_treatments" in expected_database:
        keys.append("typed_treatments")
    if "lab_audit" in expected_database:
        keys.append("lab_audit")
    if "contradiction_ledger" in expected_database:
        keys.append("contradiction_ledger")
    if "typed_glucose" in expected_database:
        keys.append("typed_glucose")
    if "typed_wearables" in expected_database:
        keys.append("typed_wearables")
    if "relationship_projection" in expected_database:
        keys.append("relationship_projection")
    if "relationship_builds" in expected_database:
        keys.append("relationship_builds")
    if "evidence_projection" in expected_database:
        keys.append("evidence_projection")
    if "claim_projection" in expected_database:
        keys.append("claim_projection")
    if "hypothesis_ledger" in expected_database:
        keys.append("hypothesis_ledger")
    if "episode_ledger" in expected_database:
        keys.append("episode_ledger")
    if "activity_position_ledger" in expected_database:
        keys.append("activity_position_ledger")
    if "management_burden_ledger" in expected_database:
        keys.append("management_burden_ledger")
    if "clinical_review_ledger" in expected_database:
        keys.append("clinical_review_ledger")
    for key in keys:
        if metadata[key] != expected_database.get(key):
            raise BackupError(f"restored database metadata mismatch: {key}")
    actual_records = {
        relative.as_posix()
        for _, relative, _ in _safe_record_files(restored_data_dir / RECORDS_NAME)
    }
    references = metadata.pop("record_references")
    missing = sorted(reference for reference in references if reference not in actual_records)
    if missing:
        raise BackupError(f"restored database references {len(missing)} missing record files")
    verification = {
        "integrity_check": metadata["integrity_check"],
        "entity_total": metadata["entity_total"],
        "record_file_count": len(actual_records),
        "referenced_record_count": len(references),
        "missing_record_count": 0,
    }
    if "source_archive" in expected_database:
        verification.update(
            {
                "source_record_count": metadata["source_archive"]["source_records"]["count"],
                "source_file_reference_count": metadata["source_archive"]["source_files"]["count"],
            }
        )
    if "activity_position_ledger" in expected_database:
        verification.update(
            {
                "activity_position_interval_count": metadata[
                    "activity_position_ledger"
                ]["intervals"]["count"],
                "manual_activity_position_interval_count": metadata[
                    "activity_position_ledger"
                ]["intervals"]["manual_count"],
                "wearable_activity_position_interval_count": metadata[
                    "activity_position_ledger"
                ]["intervals"]["wearable_count"],
                "activity_position_correction_count": metadata[
                    "activity_position_ledger"
                ]["intervals"]["correction_count"],
                "activity_position_event_count": metadata[
                    "activity_position_ledger"
                ]["events"]["count"],
            }
        )
    if "management_burden_ledger" in expected_database:
        burden_events = metadata["management_burden_ledger"]["events"]
        verification.update(
            {
                "management_burden_event_count": burden_events["count"],
                "observed_management_burden_event_count": burden_events[
                    "observed_count"
                ],
                "inferred_management_burden_event_count": burden_events[
                    "inferred_count"
                ],
                "manual_management_burden_event_count": burden_events[
                    "manual_count"
                ],
                "management_burden_correction_count": burden_events[
                    "correction_count"
                ],
                "excluded_management_burden_event_count": burden_events[
                    "excluded_count"
                ],
                "management_burden_audit_count": metadata[
                    "management_burden_ledger"
                ]["audit"]["count"],
            }
        )
    if "clinical_review_ledger" in expected_database:
        review_threads = metadata["clinical_review_ledger"]["threads"]
        verification.update(
            {
                "clinical_review_thread_count": review_threads["count"],
                "pending_clinical_review_count": review_threads["pending_count"],
                "accepted_clinical_review_count": review_threads["accepted_count"],
                "disputed_clinical_review_count": review_threads["disputed_count"],
                "clinical_review_event_count": metadata[
                    "clinical_review_ledger"
                ]["events"]["count"],
            }
        )
    if "canonical_time" in expected_database:
        verification["canonical_time_count"] = metadata["canonical_time"]["count"]
    if "typed_treatments" in expected_database:
        verification.update(
            {
                "typed_treatment_count": metadata["typed_treatments"]["typed_treatments"]["count"],
                "basal_segment_count": metadata["typed_treatments"]["basal_segments"]["count"],
                "pump_daily_total_count": metadata["typed_treatments"]["pump_daily_totals"]["count"],
            }
        )
    if "lab_audit" in expected_database:
        verification.update(
            {
                "lab_extraction_run_count": metadata["lab_audit"]["runs"]["count"],
                "lab_extraction_observation_count": metadata["lab_audit"]["observations"]["count"],
                "lab_verified_observation_count": metadata["lab_audit"]["observations"]["verified_count"],
                "lab_verification_event_count": metadata["lab_audit"]["verification_events"]["count"],
            }
        )
    if "contradiction_ledger" in expected_database:
        verification.update(
            {
                "contradiction_run_count": metadata["contradiction_ledger"]["runs"]["count"],
                "contradiction_count": metadata["contradiction_ledger"]["contradictions"]["count"],
                "unresolved_contradiction_count": metadata["contradiction_ledger"]["contradictions"]["unresolved_count"],
                "unresolved_blocking_contradiction_count": metadata["contradiction_ledger"]["contradictions"]["unresolved_blocking_count"],
                "contradiction_event_count": metadata["contradiction_ledger"]["events"]["count"],
            }
        )
    if "typed_glucose" in expected_database:
        verification.update(
            {
                "typed_glucose_reading_count": metadata["typed_glucose"]["glucose_readings"]["count"],
                "typed_fingerstick_reading_count": metadata["typed_glucose"]["fingerstick_readings"]["count"],
            }
        )
    if "typed_wearables" in expected_database:
        verification.update(
            {
                "typed_wearable_daily_count": metadata["typed_wearables"]["wearable_daily"]["count"],
                "typed_wearable_sample_count": metadata["typed_wearables"]["wearable_samples"]["count"],
            }
        )
    if "relationship_projection" in expected_database:
        verification.update(
            {
                "relationship_count": metadata["relationship_projection"]["entity_relationships"]["count"],
                "relationship_predicate_count": metadata["relationship_projection"]["relationship_predicate_registry"]["count"],
                "assertion_status_registry_count": metadata["relationship_projection"]["assertion_status_registry"]["count"],
                "evidence_level_registry_count": metadata["relationship_projection"]["evidence_level_registry"]["count"],
                "relationship_algorithm_registry_count": metadata["relationship_projection"]["relationship_algorithm_registry"]["count"],
            }
        )
    if "relationship_builds" in expected_database:
        verification.update(
            {
                "relationship_projection_run_count": metadata["relationship_builds"]["relationship_projection_runs"]["count"],
                "relationship_projection_run_edge_count": metadata["relationship_builds"]["relationship_projection_run_edges"]["count"],
                "relationship_projection_active_edge_count": metadata["relationship_builds"]["relationship_projection_active_edges"]["count"],
                "relationship_projection_state_count": metadata["relationship_builds"]["relationship_projection_state"]["count"],
            }
        )
    if "evidence_projection" in expected_database:
        verification.update(
            {
                "observation_window_count": metadata["evidence_projection"]["observation_windows"]["count"],
                "evidence_set_count": metadata["evidence_projection"]["evidence_sets"]["count"],
                "evidence_set_window_count": metadata["evidence_projection"]["evidence_set_windows"]["count"],
            }
        )
    if "claim_projection" in expected_database:
        verification.update(
            {
                "claim_algorithm_registry_count": metadata["claim_projection"]["claim_algorithm_registry"]["count"],
                "claim_version_count": metadata["claim_projection"]["claim_versions"]["count"],
            }
        )
    if "hypothesis_ledger" in expected_database:
        verification.update(
            {
                "health_hypothesis_count": metadata["hypothesis_ledger"]["hypotheses"]["count"],
                "proposed_health_hypothesis_count": metadata["hypothesis_ledger"]["hypotheses"]["proposed_count"],
                "under_review_health_hypothesis_count": metadata["hypothesis_ledger"]["hypotheses"]["under_review_count"],
                "confirmed_health_hypothesis_count": metadata["hypothesis_ledger"]["hypotheses"]["confirmed_count"],
                "ruled_against_health_hypothesis_count": metadata["hypothesis_ledger"]["hypotheses"]["ruled_against_count"],
                "hypothesis_evidence_count": metadata["hypothesis_ledger"]["evidence"]["count"],
                "hypothesis_event_count": metadata["hypothesis_ledger"]["events"]["count"],
            }
        )
    if "episode_ledger" in expected_database:
        verification.update(
            {
                "health_episode_count": metadata["episode_ledger"]["episodes"]["count"],
                "proposed_health_episode_count": metadata["episode_ledger"]["episodes"]["proposed_count"],
                "confirmed_health_episode_count": metadata["episode_ledger"]["episodes"]["confirmed_count"],
                "dismissed_health_episode_count": metadata["episode_ledger"]["episodes"]["dismissed_count"],
                "episode_member_count": metadata["episode_ledger"]["members"]["count"],
                "episode_event_count": metadata["episode_ledger"]["events"]["count"],
                "medication_exposure_count": metadata["episode_ledger"]["medication_exposures"]["count"],
                "open_ended_medication_exposure_count": metadata["episode_ledger"]["medication_exposures"]["open_ended_count"],
                "medication_exposure_event_count": metadata["episode_ledger"]["medication_exposure_events"]["count"],
            }
        )
    return verification


def verify_backup(backup_dir: Path) -> dict[str, Any]:
    """Restore the backup into a temporary clean directory and validate it."""
    backup_dir = backup_dir.expanduser().resolve(strict=True)
    manifest = _validate_backup_files(backup_dir)
    with tempfile.TemporaryDirectory(prefix="glucopilot-restore-verify-") as temp:
        restored = Path(temp) / "data"
        restore_backup(backup_dir, restored)
        return _verify_restored_data(restored, manifest)


def create_verified_backup(
    data_dir: Path,
    backup_root: Path,
    *,
    reason: str = "manual",
) -> tuple[Path, dict[str, Any]]:
    """Create an atomic backup and prove it restores before publishing it."""
    preflight = preflight_backup(data_dir, backup_root)
    data_dir = data_dir.expanduser().resolve(strict=True)
    backup_root = backup_root.expanduser().resolve()
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup_root.chmod(0o700)

    safe_reason = re.sub(r"[^A-Za-z0-9_.-]+", "-", reason).strip("-") or "backup"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{safe_reason}-{stamp}-{uuid.uuid4().hex[:8]}"
    partial = backup_root / f".{name}.partial"
    final = backup_root / name
    partial.mkdir(mode=0o700)
    try:
        source_records = _safe_record_files(data_dir / RECORDS_NAME)
        source_signature = _file_signature(source_records)
        database_target = partial / DATABASE_NAME
        _copy_database(data_dir / DATABASE_NAME, database_target)
        record_manifest = _copy_records(source_records, partial / RECORDS_NAME)
        if _file_signature(_safe_record_files(data_dir / RECORDS_NAME)) != source_signature:
            raise BackupError("record files changed while backup was being created; retry")

        database_metadata = _database_metadata(database_target, include_references=True)
        references = database_metadata.pop("record_references")
        copied_names = {item["path"] for item in record_manifest}
        missing = [reference for reference in references if reference not in copied_names]
        if missing:
            raise BackupError(f"database references {len(missing)} missing record files")
        database_metadata.update(
            {"file": DATABASE_NAME, "bytes": database_target.stat().st_size, "sha256": _sha256(database_target)}
        )
        manifest = {
            "format_version": FORMAT_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "reason": safe_reason,
            "privacy": "Backup contains private health data and credentials; manifest contains metadata only.",
            "preflight": preflight,
            "database": database_metadata,
            "records": {
                "directory": RECORDS_NAME,
                "file_count": len(record_manifest),
                "total_bytes": sum(item["bytes"] for item in record_manifest),
                "referenced_file_count": len(references),
                "files": record_manifest,
            },
        }
        _write_manifest(partial, manifest)
        verification = verify_backup(partial)
        partial.rename(final)
        return final, verification
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight_parser = subparsers.add_parser("preflight", help="check source integrity and free space")
    preflight_parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    preflight_parser.add_argument("--backup-root", type=Path, required=True)

    create_parser = subparsers.add_parser("create", help="create and restore-verify a backup")
    create_parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    create_parser.add_argument("--backup-root", type=Path, required=True)
    create_parser.add_argument("--reason", default="manual")

    verify_parser = subparsers.add_parser("verify", help="verify a backup via clean restore")
    verify_parser.add_argument("backup", type=Path)

    restore_parser = subparsers.add_parser("restore", help="restore into a new data directory")
    restore_parser.add_argument("backup", type=Path)
    restore_parser.add_argument("target", type=Path)

    args = parser.parse_args()
    if args.command == "preflight":
        _print_json(preflight_backup(args.data_dir, args.backup_root))
    elif args.command == "create":
        path, verification = create_verified_backup(
            args.data_dir, args.backup_root, reason=args.reason
        )
        _print_json({"backup": str(path), "verification": verification})
    elif args.command == "verify":
        _print_json(verify_backup(args.backup))
    elif args.command == "restore":
        _print_json(restore_backup(args.backup, args.target))


if __name__ == "__main__":
    main()
