"""Ordered, checksummed, transactional SQLite schema migrations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from .config import DB_PATH
from .schema_registry import BASELINE_ENTITY_SCHEMAS, ENTITY_SCHEMAS


TRACKING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0)
)
"""


@dataclass(frozen=True)
class Statement:
    sql: str
    parameters: tuple[Any, ...] = ()


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[Statement, ...]

    @property
    def checksum(self) -> str:
        payload = {
            "version": self.version,
            "name": self.name,
            "statements": [
                {"sql": statement.sql.strip(), "parameters": statement.parameters}
                for statement in self.statements
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        return hashlib.sha256(encoded).hexdigest()


class MigrationError(RuntimeError):
    """Raised when the database cannot be safely brought to the expected schema."""


def _registry_statements() -> tuple[Statement, ...]:
    insert = """
        INSERT INTO entity_schema_registry (
            entity_type, schema_version, storage_kind, domain, owner_scope,
            api_exposure, lifecycle, description
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """
    return tuple(
        Statement(
            insert,
            (
                schema.name,
                schema.schema_version,
                schema.storage_kind,
                schema.domain,
                schema.owner_scope,
                schema.api_exposure,
                schema.lifecycle,
                schema.description,
            ),
        )
        for schema in BASELINE_ENTITY_SCHEMAS
    )


MIGRATIONS = (
    Migration(
        1,
        "legacy_json_store_baseline",
        (
            Statement(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            ),
            Statement(
                """
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_date TEXT NOT NULL,
                    updated_date TEXT NOT NULL
                )
                """
            ),
            Statement("CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type)"),
            Statement(
                "CREATE INDEX IF NOT EXISTS idx_entities_type_ts "
                "ON entities(type, json_extract(data, '$.timestamp'))"
            ),
            Statement(
                "CREATE INDEX IF NOT EXISTS idx_entities_type_date "
                "ON entities(type, json_extract(data, '$.date'))"
            ),
        ),
    ),
    Migration(
        2,
        "entity_schema_registry",
        (
            Statement(
                """
                CREATE TABLE entity_schema_registry (
                    entity_type TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL CHECK(schema_version > 0),
                    storage_kind TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    owner_scope TEXT NOT NULL,
                    api_exposure TEXT NOT NULL,
                    lifecycle TEXT NOT NULL,
                    description TEXT NOT NULL
                )
                """
            ),
            *_registry_statements(),
        ),
    ),
    Migration(
        3,
        "immutable_source_archive",
        (
            Statement(
                """
                CREATE TABLE sync_runs (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    parser_version TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('running', 'succeeded', 'failed', 'partial')),
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    records_seen INTEGER NOT NULL DEFAULT 0 CHECK(records_seen >= 0),
                    records_archived INTEGER NOT NULL DEFAULT 0 CHECK(records_archived >= 0),
                    records_deduplicated INTEGER NOT NULL DEFAULT 0 CHECK(records_deduplicated >= 0),
                    files_seen INTEGER NOT NULL DEFAULT 0 CHECK(files_seen >= 0),
                    files_archived INTEGER NOT NULL DEFAULT 0 CHECK(files_archived >= 0),
                    files_deduplicated INTEGER NOT NULL DEFAULT 0 CHECK(files_deduplicated >= 0),
                    bytes_received INTEGER NOT NULL DEFAULT 0 CHECK(bytes_received >= 0),
                    error_summary TEXT,
                    created_at TEXT NOT NULL
                )
                """
            ),
            Statement(
                """
                CREATE TABLE source_records (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT,
                    observed_at TEXT,
                    received_at TEXT NOT NULL,
                    payload_hash TEXT NOT NULL CHECK(length(payload_hash) = 71 AND payload_hash LIKE 'sha256:%'),
                    parser_version TEXT NOT NULL,
                    sync_run_id TEXT REFERENCES sync_runs(id) ON DELETE RESTRICT,
                    content_encoding TEXT NOT NULL CHECK(content_encoding = 'json+gzip'),
                    payload BLOB NOT NULL,
                    uncompressed_bytes INTEGER NOT NULL CHECK(uncompressed_bytes >= 0),
                    stored_bytes INTEGER NOT NULL CHECK(stored_bytes >= 0),
                    created_at TEXT NOT NULL,
                    UNIQUE(owner_id, source_type, payload_hash)
                )
                """
            ),
            Statement(
                """
                CREATE TABLE source_files (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT,
                    observed_at TEXT,
                    received_at TEXT NOT NULL,
                    file_hash TEXT NOT NULL CHECK(length(file_hash) = 71 AND file_hash LIKE 'sha256:%'),
                    relative_path TEXT NOT NULL CHECK(
                        relative_path != '' AND
                        substr(relative_path, 1, 1) != '/' AND
                        relative_path != '..' AND
                        relative_path NOT LIKE '../%' AND
                        relative_path NOT LIKE '%/../%' AND
                        relative_path NOT LIKE '%/..'
                    ),
                    byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
                    mime_type TEXT,
                    parser_version TEXT NOT NULL,
                    sync_run_id TEXT REFERENCES sync_runs(id) ON DELETE RESTRICT,
                    created_at TEXT NOT NULL,
                    UNIQUE(owner_id, source_type, file_hash)
                )
                """
            ),
            Statement(
                "CREATE INDEX idx_source_records_received "
                "ON source_records(owner_id, source_type, received_at)"
            ),
            Statement(
                "CREATE INDEX idx_source_records_sync_run ON source_records(sync_run_id)"
            ),
            Statement(
                "CREATE INDEX idx_source_files_received "
                "ON source_files(owner_id, source_type, received_at)"
            ),
            Statement("CREATE INDEX idx_source_files_sync_run ON source_files(sync_run_id)"),
            Statement(
                "CREATE INDEX idx_sync_runs_source_started "
                "ON sync_runs(owner_id, source_type, started_at)"
            ),
            Statement(
                """
                CREATE TRIGGER source_records_immutable
                BEFORE UPDATE ON source_records
                BEGIN
                    SELECT RAISE(ABORT, 'source_records are immutable');
                END
                """
            ),
            Statement(
                """
                CREATE TRIGGER source_files_immutable
                BEFORE UPDATE ON source_files
                BEGIN
                    SELECT RAISE(ABORT, 'source_files are immutable');
                END
                """
            ),
        ),
    ),
    Migration(
        4,
        "connector_provenance_runs",
        (
            Statement(
                "ALTER TABLE sync_runs ADD COLUMN run_kind TEXT NOT NULL DEFAULT 'archive' "
                "CHECK(run_kind IN ('archive', 'connector', 'upload', 'ingest', 'reprocess'))"
            ),
            Statement(
                "ALTER TABLE sync_runs ADD COLUMN trigger_type TEXT NOT NULL DEFAULT 'unknown' "
                "CHECK(trigger_type IN ('unknown', 'scheduled', 'manual', 'backfill', 'upload', 'ingest', 'reprocess'))"
            ),
            Statement(
                "ALTER TABLE sync_runs ADD COLUMN connector_version TEXT NOT NULL DEFAULT 'legacy'"
            ),
            Statement(
                "ALTER TABLE sync_runs ADD COLUMN fetched_count INTEGER NOT NULL DEFAULT 0 "
                "CHECK(fetched_count >= 0)"
            ),
            Statement(
                "ALTER TABLE sync_runs ADD COLUMN created_count INTEGER NOT NULL DEFAULT 0 "
                "CHECK(created_count >= 0)"
            ),
            Statement(
                "ALTER TABLE sync_runs ADD COLUMN updated_count INTEGER NOT NULL DEFAULT 0 "
                "CHECK(updated_count >= 0)"
            ),
            Statement(
                "ALTER TABLE sync_runs ADD COLUMN skipped_count INTEGER NOT NULL DEFAULT 0 "
                "CHECK(skipped_count >= 0)"
            ),
            Statement(
                "ALTER TABLE sync_runs ADD COLUMN failed_count INTEGER NOT NULL DEFAULT 0 "
                "CHECK(failed_count >= 0)"
            ),
            Statement(
                "ALTER TABLE sync_runs ADD COLUMN stale_count INTEGER NOT NULL DEFAULT 0 "
                "CHECK(stale_count >= 0)"
            ),
            Statement("ALTER TABLE sync_runs ADD COLUMN last_successful_data_at TEXT"),
            Statement(
                """
                CREATE TABLE normalized_source_links (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
                    source_record_id TEXT REFERENCES source_records(id) ON DELETE CASCADE,
                    source_file_id TEXT REFERENCES source_files(id) ON DELETE CASCADE,
                    sync_run_id TEXT NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
                    parser_version TEXT NOT NULL,
                    linked_at TEXT NOT NULL,
                    CHECK(
                        (source_record_id IS NOT NULL AND source_file_id IS NULL) OR
                        (source_record_id IS NULL AND source_file_id IS NOT NULL)
                    )
                )
                """
            ),
            Statement(
                "CREATE INDEX idx_normalized_links_entity "
                "ON normalized_source_links(owner_id, entity_type, entity_id)"
            ),
            Statement(
                "CREATE INDEX idx_normalized_links_run ON normalized_source_links(sync_run_id)"
            ),
            Statement(
                "CREATE INDEX idx_sync_runs_freshness "
                "ON sync_runs(owner_id, source_type, status, last_successful_data_at)"
            ),
            Statement(
                """
                CREATE TRIGGER normalized_source_links_immutable
                BEFORE UPDATE ON normalized_source_links
                BEGIN
                    SELECT RAISE(ABORT, 'normalized_source_links are immutable');
                END
                """
            ),
        ),
    ),
    Migration(
        5,
        "canonical_clinical_time",
        (
            Statement(
                """
                CREATE TABLE canonical_times (
                    owner_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
                    timeline_role TEXT CHECK(timeline_role IN ('observed', 'effective_start')),
                    timeline_at TEXT,
                    observed_at TEXT,
                    recorded_at TEXT,
                    received_at TEXT,
                    effective_start TEXT,
                    effective_end TEXT,
                    event_field TEXT,
                    source_text TEXT,
                    recorded_source_text TEXT,
                    received_source_text TEXT,
                    normalized_value TEXT,
                    local_date TEXT,
                    timezone TEXT,
                    utc_offset TEXT,
                    precision TEXT NOT NULL CHECK(precision IN (
                        'second', 'minute', 'hour', 'day', 'month', 'year', 'unknown'
                    )),
                    basis TEXT NOT NULL CHECK(basis IN (
                        'exact', 'patient_reported', 'source_reported', 'inferred', 'unknown'
                    )),
                    dst_resolution TEXT NOT NULL CHECK(dst_resolution IN (
                        'not_applicable', 'unambiguous',
                        'ambiguous_earlier_offset', 'ambiguous_later_offset',
                        'nonexistent_local_time', 'unresolved'
                    )),
                    normalization_status TEXT NOT NULL CHECK(normalization_status IN (
                        'resolved', 'partial', 'ambiguous', 'nonexistent', 'invalid',
                        'not_applicable'
                    )),
                    inferred INTEGER NOT NULL CHECK(inferred IN (0, 1)),
                    duration_seconds REAL CHECK(duration_seconds IS NULL OR duration_seconds >= 0),
                    additional_times_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(additional_times_json)),
                    normalizer_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK(timeline_at IS NULL OR timeline_at LIKE '%Z'),
                    CHECK(observed_at IS NULL OR observed_at LIKE '%Z'),
                    CHECK(recorded_at IS NULL OR recorded_at LIKE '%Z'),
                    CHECK(received_at IS NULL OR received_at LIKE '%Z'),
                    CHECK(effective_start IS NULL OR effective_start LIKE '%Z'),
                    CHECK(effective_end IS NULL OR effective_end LIKE '%Z'),
                    CHECK(local_date IS NULL OR length(local_date) = 10),
                    CHECK((basis = 'inferred') = inferred)
                )
                """
            ),
            Statement(
                "CREATE INDEX idx_canonical_times_timeline "
                "ON canonical_times(owner_id, timeline_at, entity_type, timeline_role)"
            ),
            Statement(
                "CREATE INDEX idx_canonical_times_local_date "
                "ON canonical_times(owner_id, local_date, entity_type, timeline_role)"
            ),
            Statement("CREATE INDEX idx_canonical_times_type ON canonical_times(owner_id, entity_type)"),
        ),
    ),
    Migration(
        6,
        "typed_treatment_domain",
        (
            Statement(
                """
                CREATE TABLE typed_treatments (
                    entity_id TEXT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
                    canonical_id TEXT NOT NULL UNIQUE,
                    owner_id TEXT NOT NULL CHECK(owner_id = 'urn:glucopilot:owner:self'),
                    owner_email TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(source != ''),
                    source_record_id TEXT,
                    source_record_canonical_id TEXT,
                    occurred_at TEXT NOT NULL CHECK(occurred_at LIKE '%Z'),
                    source_timestamp TEXT NOT NULL,
                    local_date TEXT NOT NULL CHECK(length(local_date) = 10),
                    kind TEXT NOT NULL CHECK(kind IN (
                        'insulin', 'carbohydrate', 'blood_glucose', 'note',
                        'basal', 'suspension', 'other'
                    )),
                    legacy_type TEXT NOT NULL CHECK(legacy_type != ''),
                    event_type TEXT,
                    amount_value REAL CHECK(amount_value IS NULL OR amount_value >= 0),
                    amount_unit TEXT CHECK(amount_unit IS NULL OR amount_unit IN ('U', 'g')),
                    insulin_type TEXT,
                    glucose_mg_dl REAL CHECK(glucose_mg_dl IS NULL OR glucose_mg_dl >= 0),
                    glucose_type TEXT,
                    notes TEXT,
                    reason TEXT,
                    pre_bolus_minutes REAL,
                    legacy_fingerprint TEXT NOT NULL CHECK(
                        length(legacy_fingerprint) = 71 AND legacy_fingerprint LIKE 'sha256:%'
                    ),
                    mapping_version TEXT NOT NULL,
                    received_at TEXT NOT NULL CHECK(received_at LIKE '%Z'),
                    recorded_at TEXT NOT NULL CHECK(recorded_at LIKE '%Z'),
                    created_at TEXT NOT NULL CHECK(created_at LIKE '%Z'),
                    updated_at TEXT NOT NULL CHECK(updated_at LIKE '%Z'),
                    CHECK(kind != 'carbohydrate' OR (amount_value IS NOT NULL AND amount_unit = 'g')),
                    CHECK(kind != 'blood_glucose' OR glucose_mg_dl IS NOT NULL)
                )
                """
            ),
            Statement(
                """
                CREATE TABLE basal_segments (
                    treatment_entity_id TEXT PRIMARY KEY
                        REFERENCES typed_treatments(entity_id) ON DELETE CASCADE,
                    owner_id TEXT NOT NULL CHECK(owner_id = 'urn:glucopilot:owner:self'),
                    source TEXT NOT NULL CHECK(source != ''),
                    source_record_id TEXT,
                    segment_kind TEXT NOT NULL CHECK(segment_kind IN ('temp_basal', 'suspension')),
                    started_at TEXT NOT NULL CHECK(started_at LIKE '%Z'),
                    ended_at TEXT CHECK(ended_at IS NULL OR ended_at LIKE '%Z'),
                    duration_seconds REAL CHECK(duration_seconds IS NULL OR duration_seconds >= 0),
                    rate_units_per_hour REAL CHECK(
                        rate_units_per_hour IS NULL OR rate_units_per_hour >= 0
                    ),
                    percent_of_profile REAL,
                    mapping_version TEXT NOT NULL,
                    created_at TEXT NOT NULL CHECK(created_at LIKE '%Z'),
                    updated_at TEXT NOT NULL CHECK(updated_at LIKE '%Z'),
                    CHECK(
                        (duration_seconds IS NULL AND ended_at IS NULL) OR
                        (duration_seconds IS NOT NULL AND ended_at IS NOT NULL)
                    )
                )
                """
            ),
            Statement(
                """
                CREATE TABLE pump_daily_totals (
                    treatment_entity_id TEXT PRIMARY KEY
                        REFERENCES typed_treatments(entity_id) ON DELETE CASCADE,
                    owner_id TEXT NOT NULL CHECK(owner_id = 'urn:glucopilot:owner:self'),
                    source TEXT NOT NULL CHECK(source != ''),
                    source_record_id TEXT,
                    occurred_at TEXT NOT NULL CHECK(occurred_at LIKE '%Z'),
                    local_date TEXT NOT NULL CHECK(length(local_date) = 10),
                    total_units REAL NOT NULL CHECK(total_units >= 0),
                    basal_units REAL CHECK(basal_units IS NULL OR basal_units >= 0),
                    bolus_units REAL CHECK(bolus_units IS NULL OR bolus_units >= 0),
                    completeness TEXT NOT NULL CHECK(completeness IN ('complete', 'partial')),
                    mapping_version TEXT NOT NULL,
                    created_at TEXT NOT NULL CHECK(created_at LIKE '%Z'),
                    updated_at TEXT NOT NULL CHECK(updated_at LIKE '%Z')
                )
                """
            ),
            Statement(
                "CREATE INDEX idx_typed_treatments_owner_time "
                "ON typed_treatments(owner_id, occurred_at, entity_id)"
            ),
            Statement(
                "CREATE INDEX idx_typed_treatments_owner_source_time "
                "ON typed_treatments(owner_id, source, occurred_at, entity_id)"
            ),
            Statement(
                "CREATE INDEX idx_typed_treatments_source_record "
                "ON typed_treatments(owner_id, source, source_record_id) "
                "WHERE source_record_id IS NOT NULL"
            ),
            Statement(
                "CREATE INDEX idx_typed_treatments_kind_time "
                "ON typed_treatments(owner_id, kind, occurred_at, entity_id)"
            ),
            Statement(
                "CREATE INDEX idx_basal_segments_owner_time "
                "ON basal_segments(owner_id, started_at, treatment_entity_id)"
            ),
            Statement(
                "CREATE INDEX idx_basal_segments_source_time "
                "ON basal_segments(owner_id, source, started_at, treatment_entity_id)"
            ),
            Statement(
                "CREATE INDEX idx_pump_daily_totals_owner_date "
                "ON pump_daily_totals(owner_id, local_date, treatment_entity_id)"
            ),
            Statement(
                "CREATE INDEX idx_pump_daily_totals_source_date "
                "ON pump_daily_totals(owner_id, source, local_date, treatment_entity_id)"
            ),
        ),
    ),
    Migration(
        7,
        "auditable_medical_record_extraction",
        (
            Statement(
                """
                CREATE TABLE lab_extraction_runs (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    record_entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
                    source_file_id TEXT REFERENCES source_files(id) ON DELETE SET NULL,
                    source_hash TEXT NOT NULL CHECK(
                        length(source_hash) = 71 AND source_hash LIKE 'sha256:%'
                    ),
                    parser_version TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    input_data_version TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('running', 'succeeded', 'partial', 'failed')),
                    page_count INTEGER NOT NULL DEFAULT 0 CHECK(page_count >= 0),
                    failed_batch_count INTEGER NOT NULL DEFAULT 0 CHECK(failed_batch_count >= 0),
                    started_at TEXT NOT NULL CHECK(started_at LIKE '%Z'),
                    completed_at TEXT CHECK(completed_at IS NULL OR completed_at LIKE '%Z'),
                    error_summary TEXT,
                    created_at TEXT NOT NULL CHECK(created_at LIKE '%Z')
                )
                """
            ),
            Statement(
                """
                CREATE TABLE lab_extraction_observations (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    record_entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
                    extraction_run_id TEXT NOT NULL REFERENCES lab_extraction_runs(id) ON DELETE CASCADE,
                    legacy_entity_id TEXT,
                    stable_source_key TEXT NOT NULL CHECK(
                        length(stable_source_key) = 71 AND stable_source_key LIKE 'sha256:%'
                    ),
                    version INTEGER NOT NULL CHECK(version > 0),
                    original_name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    original_value TEXT NOT NULL,
                    normalized_value REAL,
                    value_kind TEXT NOT NULL CHECK(value_kind IN ('numeric', 'qualitative', 'titer')),
                    original_unit TEXT NOT NULL DEFAULT '',
                    normalized_unit TEXT NOT NULL DEFAULT '',
                    original_reference_range TEXT NOT NULL DEFAULT '',
                    reference_low REAL,
                    reference_high REAL,
                    original_flag TEXT NOT NULL DEFAULT '',
                    normalized_flag TEXT NOT NULL DEFAULT '',
                    specimen TEXT NOT NULL DEFAULT '',
                    original_collected_date TEXT NOT NULL DEFAULT '',
                    normalized_collected_date TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    source_page INTEGER CHECK(source_page IS NULL OR source_page > 0),
                    extraction_location_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(extraction_location_json)),
                    parser_confidence REAL CHECK(
                        parser_confidence IS NULL OR
                        (parser_confidence >= 0 AND parser_confidence <= 1)
                    ),
                    validation_status TEXT NOT NULL CHECK(
                        validation_status IN ('valid', 'warning', 'invalid')
                    ),
                    validation_issues_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(validation_issues_json)),
                    verification_status TEXT NOT NULL CHECK(
                        verification_status IN ('unverified', 'approved', 'edited', 'rejected', 'superseded')
                    ),
                    supersedes_observation_id TEXT REFERENCES lab_extraction_observations(id) ON DELETE SET NULL,
                    superseded_by_observation_id TEXT REFERENCES lab_extraction_observations(id) ON DELETE SET NULL,
                    superseded_at TEXT CHECK(superseded_at IS NULL OR superseded_at LIKE '%Z'),
                    created_at TEXT NOT NULL CHECK(created_at LIKE '%Z'),
                    updated_at TEXT NOT NULL CHECK(updated_at LIKE '%Z'),
                    UNIQUE(record_entity_id, stable_source_key, version)
                )
                """
            ),
            Statement(
                """
                CREATE TABLE lab_verification_events (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    record_entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
                    observation_id TEXT NOT NULL REFERENCES lab_extraction_observations(id) ON DELETE RESTRICT,
                    action TEXT NOT NULL CHECK(action IN ('approve', 'edit', 'reject', 'supersede')),
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    before_json TEXT NOT NULL CHECK(json_valid(before_json)),
                    after_json TEXT NOT NULL CHECK(json_valid(after_json)),
                    created_at TEXT NOT NULL CHECK(created_at LIKE '%Z')
                )
                """
            ),
            Statement(
                "CREATE INDEX idx_lab_extraction_runs_record "
                "ON lab_extraction_runs(owner_id, record_entity_id, started_at)"
            ),
            Statement(
                "CREATE INDEX idx_lab_observations_record_status "
                "ON lab_extraction_observations(owner_id, record_entity_id, verification_status)"
            ),
            Statement(
                "CREATE INDEX idx_lab_observations_legacy "
                "ON lab_extraction_observations(owner_id, legacy_entity_id)"
            ),
            Statement(
                "CREATE INDEX idx_lab_observations_source_key "
                "ON lab_extraction_observations(owner_id, record_entity_id, stable_source_key, version)"
            ),
            Statement(
                "CREATE INDEX idx_lab_verification_events_observation "
                "ON lab_verification_events(owner_id, observation_id, created_at)"
            ),
            Statement(
                """
                CREATE TRIGGER lab_extraction_observations_immutable_delete
                BEFORE DELETE ON lab_extraction_observations
                WHEN EXISTS (
                    SELECT 1 FROM entities WHERE id=OLD.record_entity_id
                )
                BEGIN
                    SELECT RAISE(ABORT, 'lab_extraction_observations are immutable');
                END
                """
            ),
            Statement(
                """
                CREATE TRIGGER lab_verification_events_immutable_update
                BEFORE UPDATE ON lab_verification_events
                BEGIN
                    SELECT RAISE(ABORT, 'lab_verification_events are immutable');
                END
                """
            ),
            Statement(
                """
                CREATE TRIGGER lab_verification_events_immutable_delete
                BEFORE DELETE ON lab_verification_events
                WHEN EXISTS (
                    SELECT 1 FROM entities WHERE id=OLD.record_entity_id
                )
                BEGIN
                    SELECT RAISE(ABORT, 'lab_verification_events are immutable');
                END
                """
            ),
        ),
    ),
    Migration(
        8,
        "clinical_contradiction_ledger",
        (
            Statement(
                """
                CREATE TABLE contradiction_runs (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    rules_version TEXT NOT NULL,
                    input_data_version TEXT NOT NULL CHECK(
                        length(input_data_version) = 71 AND input_data_version LIKE 'sha256:%'
                    ),
                    status TEXT NOT NULL CHECK(status IN ('running', 'succeeded', 'failed')),
                    detection_count INTEGER NOT NULL DEFAULT 0 CHECK(detection_count >= 0),
                    started_at TEXT NOT NULL CHECK(started_at LIKE '%Z'),
                    completed_at TEXT CHECK(completed_at IS NULL OR completed_at LIKE '%Z'),
                    error_summary TEXT,
                    created_at TEXT NOT NULL CHECK(created_at LIKE '%Z')
                )
                """
            ),
            Statement(
                """
                CREATE TABLE contradictions (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    contradiction_run_id TEXT NOT NULL REFERENCES contradiction_runs(id) ON DELETE RESTRICT,
                    detection_key TEXT NOT NULL CHECK(
                        length(detection_key) = 71 AND detection_key LIKE 'sha256:%'
                    ),
                    rule_id TEXT NOT NULL,
                    rule_version TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    subject_type TEXT NOT NULL,
                    subject_key TEXT NOT NULL,
                    severity TEXT NOT NULL CHECK(severity IN ('info', 'warning', 'blocking')),
                    explanation TEXT NOT NULL,
                    left_json TEXT NOT NULL CHECK(json_valid(left_json)),
                    right_json TEXT NOT NULL CHECK(json_valid(right_json)),
                    context_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(context_json)),
                    detection_state TEXT NOT NULL CHECK(detection_state IN ('active', 'not_current')),
                    resolution_state TEXT NOT NULL CHECK(resolution_state IN ('unresolved', 'resolved')),
                    resolution_kind TEXT CHECK(
                        resolution_kind IS NULL OR resolution_kind IN (
                            'accepted_left', 'accepted_right', 'both_valid',
                            'data_corrected', 'not_applicable'
                        )
                    ),
                    resolution_note TEXT NOT NULL DEFAULT '',
                    resolved_by TEXT,
                    resolved_at TEXT CHECK(resolved_at IS NULL OR resolved_at LIKE '%Z'),
                    first_detected_at TEXT NOT NULL CHECK(first_detected_at LIKE '%Z'),
                    last_detected_at TEXT NOT NULL CHECK(last_detected_at LIKE '%Z'),
                    no_longer_detected_at TEXT CHECK(
                        no_longer_detected_at IS NULL OR no_longer_detected_at LIKE '%Z'
                    ),
                    created_at TEXT NOT NULL CHECK(created_at LIKE '%Z'),
                    updated_at TEXT NOT NULL CHECK(updated_at LIKE '%Z'),
                    UNIQUE(owner_id, detection_key),
                    CHECK(
                        (resolution_state = 'unresolved' AND resolved_by IS NULL AND resolved_at IS NULL) OR
                        (resolution_state = 'resolved' AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)
                    )
                )
                """
            ),
            Statement(
                """
                CREATE TABLE contradiction_events (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    contradiction_id TEXT NOT NULL REFERENCES contradictions(id) ON DELETE RESTRICT,
                    action TEXT NOT NULL CHECK(action IN ('detected', 'not_current', 'resolved', 'reopened')),
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    actor_name TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    before_json TEXT NOT NULL CHECK(json_valid(before_json)),
                    after_json TEXT NOT NULL CHECK(json_valid(after_json)),
                    created_at TEXT NOT NULL CHECK(created_at LIKE '%Z')
                )
                """
            ),
            Statement(
                "CREATE INDEX idx_contradictions_resolution "
                "ON contradictions(owner_id, resolution_state, detection_state, severity, domain)"
            ),
            Statement(
                "CREATE INDEX idx_contradictions_subject "
                "ON contradictions(owner_id, subject_type, subject_key, rule_id)"
            ),
            Statement(
                "CREATE INDEX idx_contradiction_events_record "
                "ON contradiction_events(owner_id, contradiction_id, created_at)"
            ),
            Statement(
                """
                CREATE TRIGGER contradictions_immutable_delete
                BEFORE DELETE ON contradictions
                BEGIN
                    SELECT RAISE(ABORT, 'contradictions are immutable');
                END
                """
            ),
            Statement(
                """
                CREATE TRIGGER contradiction_events_immutable_update
                BEFORE UPDATE ON contradiction_events
                BEGIN
                    SELECT RAISE(ABORT, 'contradiction_events are immutable');
                END
                """
            ),
            Statement(
                """
                CREATE TRIGGER contradiction_events_immutable_delete
                BEFORE DELETE ON contradiction_events
                BEGIN
                    SELECT RAISE(ABORT, 'contradiction_events are immutable');
                END
                """
            ),
        ),
    ),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate_definitions(migrations: tuple[Migration, ...]) -> None:
    versions = [migration.version for migration in migrations]
    if versions != list(range(1, len(migrations) + 1)):
        raise MigrationError("migration versions must be contiguous, ordered, and start at 1")
    names = [migration.name for migration in migrations]
    if len(set(names)) != len(names):
        raise MigrationError("migration names must be unique")
    if any(not migration.statements for migration in migrations):
        raise MigrationError("every migration must contain at least one statement")


def _connect(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    uri = f"file:{quote(str(resolved), safe='/')}?mode=rwc"
    connection = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    deadline = time.monotonic() + 30
    while True:
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).lower() or time.monotonic() >= deadline:
                connection.close()
                raise MigrationError(f"could not configure SQLite WAL mode: {error}") from error
            time.sleep(0.05)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _validate_applied(
    applied: list[sqlite3.Row], migrations: tuple[Migration, ...]
) -> int:
    expected_versions = list(range(1, len(applied) + 1))
    actual_versions = [row["version"] for row in applied]
    if actual_versions != expected_versions:
        raise MigrationError(
            f"applied migration versions are not a contiguous prefix: {actual_versions}"
        )
    if len(applied) > len(migrations):
        raise MigrationError(
            "database schema is newer than this application; use a compatible release"
        )
    for row, migration in zip(applied, migrations):
        if row["name"] != migration.name:
            raise MigrationError(
                f"migration {migration.version} name drift: database={row['name']!r}, "
                f"application={migration.name!r}"
            )
        if row["checksum"] != migration.checksum:
            raise MigrationError(
                f"migration {migration.version} checksum drift for {migration.name!r}"
            )
    return len(applied)


def _validate_schema_registry(connection: sqlite3.Connection) -> None:
    actual = connection.execute(
        """
        SELECT entity_type, schema_version, storage_kind, domain, owner_scope,
               api_exposure, lifecycle, description
        FROM entity_schema_registry
        ORDER BY entity_type
        """
    ).fetchall()
    expected = sorted(
        (
            schema.name,
            schema.schema_version,
            schema.storage_kind,
            schema.domain,
            schema.owner_scope,
            schema.api_exposure,
            schema.lifecycle,
            schema.description,
        )
        for schema in ENTITY_SCHEMAS
    )
    if [tuple(row) for row in actual] != expected:
        raise MigrationError(
            "entity schema registry drift; add an ordered migration for registry changes"
        )


def run_migrations(
    path: Path = DB_PATH, migrations: Iterable[Migration] = MIGRATIONS
) -> list[int]:
    """Apply all pending migrations under one exclusive writer transaction.

    Returns the versions applied by this invocation. Any error rolls back all
    migrations attempted by this invocation and prevents application startup.
    """
    ordered = tuple(migrations)
    _validate_definitions(ordered)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = _connect(path)
    current: Migration | None = None
    applied_now: list[int] = []
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as error:
            raise MigrationError(f"could not acquire schema migration lock: {error}") from error
        connection.execute(TRACKING_TABLE_SQL)
        applied = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        applied_count = _validate_applied(applied, ordered)

        for current in ordered[applied_count:]:
            started = time.perf_counter()
            for statement in current.statements:
                connection.execute(statement.sql, statement.parameters)
            duration_ms = max(0, round((time.perf_counter() - started) * 1_000))
            connection.execute(
                """
                INSERT INTO schema_migrations (version, name, checksum, applied_at, duration_ms)
                VALUES (?, ?, ?, ?, ?)
                """,
                (current.version, current.name, current.checksum, _now_iso(), duration_ms),
            )
            applied_now.append(current.version)
        _validate_schema_registry(connection)
        connection.commit()
        return applied_now
    except MigrationError:
        connection.rollback()
        raise
    except sqlite3.Error as error:
        connection.rollback()
        label = f"migration {current.version} ({current.name})" if current else "migration bootstrap"
        raise MigrationError(f"{label} failed: {error}") from error
    finally:
        connection.close()


def pending_migration_versions(
    path: Path = DB_PATH, migrations: Iterable[Migration] = MIGRATIONS
) -> list[int]:
    """Inspect an existing database without modifying it."""
    ordered = tuple(migrations)
    _validate_definitions(ordered)
    if not path.exists() or path.stat().st_size == 0:
        return [migration.version for migration in ordered]
    try:
        resolved = path.expanduser().resolve(strict=True)
        uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=30) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if not table:
                return [migration.version for migration in ordered]
            applied = connection.execute(
                "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
            applied_count = _validate_applied(applied, ordered)
            return [migration.version for migration in ordered[applied_count:]]
    except MigrationError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise MigrationError(f"could not inspect migration state: {error}") from error
