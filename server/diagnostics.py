"""Privacy-safe operational diagnostics for data freshness and platform health."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

from . import db
from .auth import require_login
from .backup import BackupError, _load_manifest
from .config import MIGRATION_BACKUP_DIR, OWNER_EMAIL
from .data_contracts import DEPLOYMENT_OWNER_ID
from .relationship_projection import (
    RelationshipProjectionRepository,
    relationship_projection_writes_enabled,
)
from .relationships import relationship_reads_enabled


router = APIRouter(prefix="/api/diagnostics", dependencies=[Depends(require_login)])
CONTRACT_VERSION = "platform-diagnostics/1.0.0"
BACKUP_MAX_AGE_DAYS = 30
GRAPH_MAX_AGE_DAYS = 7
ANALYTICS_MAX_AGE_DAYS = 14


def _semantics() -> dict[str, Any]:
    return {
        "category": "operational_diagnostics",
        "not_health_findings": True,
        "message": (
            "These diagnostics describe data pipelines and platform state. "
            "They are not medical findings and do not assess the user's health."
        ),
    }


@dataclass(frozen=True)
class Observation:
    entity_type: str
    field: str
    source: str | None = None
    source_missing: bool = False


@dataclass(frozen=True)
class SourceSpec:
    key: str
    label: str
    observations: tuple[Observation, ...]
    max_age_days: int | None
    setting_key: str | None = None
    connection_type: str | None = None


SOURCE_SPECS = (
    SourceSpec(
        "dexcom",
        "Dexcom",
        (Observation("GlucoseReading", "timestamp", "dexcom"),),
        2,
        connection_type="DexcomConnection",
    ),
    SourceSpec(
        "dexcom_share",
        "Dexcom Share",
        (Observation("GlucoseReading", "timestamp", "dexcom_share"),),
        2,
        setting_key="dexcom_share_verified",
    ),
    SourceSpec(
        "nightscout",
        "Nightscout",
        (
            Observation("GlucoseReading", "timestamp", "nightscout"),
            Observation("Treatment", "timestamp", "nightscout"),
        ),
        2,
    ),
    SourceSpec(
        "tandem",
        "Tandem Source",
        (Observation("Treatment", "timestamp", "tandem"),),
        14,
        setting_key="tandem_verified",
    ),
    SourceSpec(
        "glooko",
        "Glooko",
        (
            Observation("GlucoseReading", "timestamp", "glooko"),
            Observation("Treatment", "timestamp", "glooko"),
        ),
        14,
        setting_key="glooko_verified",
    ),
    SourceSpec(
        "oura",
        "Oura",
        (
            Observation("OuraDaily", "date"),
            Observation("OuraHeartRate", "timestamp", "oura"),
        ),
        7,
        connection_type="OuraConnection",
    ),
    SourceSpec(
        "fitbit",
        "Fitbit",
        (Observation("FitbitDaily", "date", source_missing=True),),
        7,
        connection_type="FitbitConnection",
    ),
    SourceSpec(
        "google_health",
        "Google Health",
        (
            Observation("FitbitDaily", "date", "google_health"),
            Observation("FitbitHeartRate", "timestamp", "google_health"),
        ),
        7,
        connection_type="GoogleHealthConnection",
    ),
    SourceSpec(
        "medical_record_upload",
        "Medical records",
        (Observation("MedicalRecord", "record_date"),),
        None,
    ),
    SourceSpec(
        "cycle_ingest",
        "Cycle records",
        (Observation("PeriodLog", "date"),),
        45,
    ),
)


def _instant(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value)
    if len(text) == 10:
        text += "T00:00:00Z"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00",
        "Z",
    )


def _age_days(value: datetime | None, as_of: datetime) -> float | None:
    if value is None:
        return None
    return round(max(0.0, (as_of - value).total_seconds()) / 86_400, 2)


def _issue(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def _table_exists(connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _latest_observation(connection, spec: SourceSpec) -> datetime | None:
    latest = None
    for observation in spec.observations:
        clauses = [
            "type=?",
            "json_extract(data, '$.owner_email')=?",
        ]
        parameters: list[Any] = [observation.entity_type, OWNER_EMAIL]
        if observation.source:
            clauses.append("json_extract(data, '$.source')=?")
            parameters.append(observation.source)
        elif observation.source_missing:
            clauses.append("json_extract(data, '$.source') IS NULL")
        row = connection.execute(
            f"""
            SELECT MAX(json_extract(data, '$.{observation.field}'))
            FROM entities WHERE {' AND '.join(clauses)}
            """,
            parameters,
        ).fetchone()
        candidate = _instant(row[0] if row else None)
        if candidate and (latest is None or candidate > latest):
            latest = candidate
    return latest


def _truth(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _settings(connection) -> dict[str, str]:
    return {
        str(row["key"]).removeprefix("cfg_"): str(row["value"])
        for row in connection.execute(
            "SELECT key,value FROM app_settings WHERE key LIKE 'cfg_%'"
        ).fetchall()
    }


def _connected(connection, spec: SourceSpec, settings: dict[str, str]) -> bool:
    if spec.connection_type:
        row = connection.execute(
            """
            SELECT 1 FROM entities
            WHERE type=? AND json_extract(data, '$.owner_email')=?
              AND json_extract(data, '$.connected')=1
            LIMIT 1
            """,
            (spec.connection_type, OWNER_EMAIL),
        ).fetchone()
        return row is not None
    if spec.setting_key:
        return _truth(settings.get(spec.setting_key))
    if spec.key == "nightscout":
        row = connection.execute(
            """
            SELECT json_extract(data, '$.nightscout_connected')
            FROM entities
            WHERE type='UserSettings' AND json_extract(data, '$.owner_email')=?
            ORDER BY created_date DESC LIMIT 1
            """,
            (OWNER_EMAIL,),
        ).fetchone()
        return bool(row and _truth(row[0]))
    return False


def _legacy_sync_time(connection, spec: SourceSpec, settings: dict[str, str]) -> datetime | None:
    setting_keys = {
        "dexcom_share": "dexcom_share_last_sync",
        "tandem": "tandem_last_sync",
        "glooko": "glooko_last_sync",
        "fitbit": "fitbit_last_sync",
        "google_health": "google_health_last_sync",
    }
    if key := setting_keys.get(spec.key):
        return _instant(settings.get(key))
    if spec.key == "dexcom":
        row = connection.execute(
            """
            SELECT json_extract(data, '$.last_sync') FROM entities
            WHERE type='DexcomConnection' AND json_extract(data, '$.owner_email')=?
            ORDER BY created_date DESC LIMIT 1
            """,
            (OWNER_EMAIL,),
        ).fetchone()
        return _instant(row[0] if row else None)
    if spec.key == "nightscout":
        row = connection.execute(
            """
            SELECT json_extract(data, '$.last_nightscout_sync') FROM entities
            WHERE type='UserSettings' AND json_extract(data, '$.owner_email')=?
            ORDER BY created_date DESC LIMIT 1
            """,
            (OWNER_EMAIL,),
        ).fetchone()
        return _instant(row[0] if row else None)
    return None


def _sync_runs(connection) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    latest: dict[str, dict[str, Any]] = {}
    success: dict[str, dict[str, Any]] = {}
    if not _table_exists(connection, "sync_runs"):
        return latest, success
    rows = connection.execute(
        """
        SELECT source_type,status,started_at,completed_at,fetched_count,
               created_count,updated_count,skipped_count,failed_count,
               stale_count,last_successful_data_at
        FROM sync_runs WHERE owner_id=?
        ORDER BY started_at DESC, id DESC
        """,
        (DEPLOYMENT_OWNER_ID,),
    ).fetchall()
    for raw in rows:
        row = dict(raw)
        source = row["source_type"]
        latest.setdefault(source, row)
        if row["status"] == "succeeded":
            success.setdefault(source, row)
    return latest, success


def _source_diagnostics(connection, as_of: datetime) -> list[dict[str, Any]]:
    settings = _settings(connection)
    latest_runs, successful_runs = _sync_runs(connection)
    output = []
    for spec in SOURCE_SPECS:
        data_through = _latest_observation(connection, spec)
        latest = latest_runs.get(spec.key)
        successful = successful_runs.get(spec.key)
        legacy_sync = _legacy_sync_time(connection, spec, settings)
        successful_at = _instant(
            successful.get("completed_at") if successful else None
        ) or legacy_sync
        governed_data_through = _instant(
            successful.get("last_successful_data_at") if successful else None
        )
        if governed_data_through and (
            data_through is None or governed_data_through > data_through
        ):
            data_through = governed_data_through
        configured = _connected(connection, spec, settings)
        tracking = "governed" if latest else "legacy"
        issues: list[dict[str, str]] = []
        status = "inactive"
        latest_status = latest.get("status") if latest else None
        age = _age_days(data_through, as_of)
        if latest_status == "failed":
            status = "error"
            issues.append(_issue(
                "latest_sync_failed",
                "critical",
                f"{spec.label} most recently failed to synchronize.",
            ))
        elif latest_status == "partial":
            status = "warning"
            issues.append(_issue(
                "latest_sync_partial",
                "warning",
                f"{spec.label} most recently completed only partially.",
            ))
        elif configured and data_through is None:
            status = "error"
            issues.append(_issue(
                "configured_source_has_no_data",
                "critical",
                f"{spec.label} is configured but has no data-through time.",
            ))
        elif data_through is not None:
            status = "current"
            if spec.max_age_days is not None and age is not None and age > spec.max_age_days:
                status = "stale"
                issues.append(_issue(
                    "source_data_stale",
                    "warning",
                    f"{spec.label} data are older than the {spec.max_age_days}-day freshness limit.",
                ))
        elif configured or latest:
            status = "warning"
            issues.append(_issue(
                "source_freshness_unknown",
                "warning",
                f"{spec.label} has no interpretable freshness timestamp.",
            ))
        latest_counters = {
            key: int(latest.get(key) or 0) if latest else 0
            for key in (
                "fetched_count",
                "created_count",
                "updated_count",
                "skipped_count",
                "failed_count",
                "stale_count",
            )
        }
        if latest_counters["failed_count"] and not any(
            item["code"] == "latest_sync_failed" for item in issues
        ):
            issues.append(_issue(
                "sync_items_failed",
                "warning",
                f"{spec.label} reported failed items in its latest run.",
            ))
            if status == "current":
                status = "warning"
        lag_seconds = None
        if successful_at and data_through:
            lag_seconds = round(max(
                0.0,
                (successful_at - data_through).total_seconds(),
            ))
        output.append({
            "source": spec.key,
            "label": spec.label,
            "configured": configured,
            "tracking": tracking,
            "status": status,
            "last_successful_sync_at": _utc(successful_at),
            "data_through": _utc(data_through),
            "freshness_days": age,
            "freshness_limit_days": spec.max_age_days,
            "import_lag_seconds": lag_seconds,
            "latest_run_status": latest_status,
            "latest_run_started_at": (
                _utc(_instant(latest.get("started_at"))) if latest else None
            ),
            "latest_run_completed_at": (
                _utc(_instant(latest.get("completed_at"))) if latest else None
            ),
            "latest_run_counts": latest_counters,
            "issues": issues,
        })
    return output


def _quality(connection) -> dict[str, Any]:
    counters = {
        "sync_failed_runs": 0,
        "sync_partial_runs": 0,
        "sync_failed_items": 0,
        "sync_duplicate_or_skipped_items": 0,
        "parser_failed_runs": 0,
        "parser_failed_batches": 0,
        "unverified_records": 0,
        "invalid_records": 0,
        "unresolved_canonical_times": 0,
    }
    if _table_exists(connection, "sync_runs"):
        row = connection.execute(
            """
            SELECT COALESCE(SUM(status='failed'),0),
                   COALESCE(SUM(status='partial'),0),
                   COALESCE(SUM(failed_count),0),
                   COALESCE(SUM(skipped_count + records_deduplicated + files_deduplicated),0)
            FROM sync_runs WHERE owner_id=?
            """,
            (DEPLOYMENT_OWNER_ID,),
        ).fetchone()
        (
            counters["sync_failed_runs"],
            counters["sync_partial_runs"],
            counters["sync_failed_items"],
            counters["sync_duplicate_or_skipped_items"],
        ) = map(int, row)
    if _table_exists(connection, "lab_extraction_runs"):
        row = connection.execute(
            """
            SELECT COALESCE(SUM(status='failed'),0),
                   COALESCE(SUM(failed_batch_count),0)
            FROM lab_extraction_runs WHERE owner_id=?
            """,
            (DEPLOYMENT_OWNER_ID,),
        ).fetchone()
        counters["parser_failed_runs"], counters["parser_failed_batches"] = map(int, row)
    row = connection.execute(
        """
        SELECT COALESCE(SUM(
                   COALESCE(json_extract(data, '$.verification_status'), 'unverified')
                   NOT IN ('approved','edited','rejected','superseded')
               ),0),
               COALESCE(SUM(json_extract(data, '$.validation_status')='invalid'),0)
        FROM entities
        WHERE type='LabResult' AND json_extract(data, '$.owner_email')=?
        """,
        (OWNER_EMAIL,),
    ).fetchone()
    counters["unverified_records"], counters["invalid_records"] = map(int, row)
    if _table_exists(connection, "canonical_times"):
        counters["unresolved_canonical_times"] = int(connection.execute(
            """
            SELECT COUNT(*) FROM canonical_times
            WHERE owner_id=? AND normalization_status IN ('ambiguous','nonexistent','invalid')
            """,
            (DEPLOYMENT_OWNER_ID,),
        ).fetchone()[0])
    review_required = (
        counters["parser_failed_runs"]
        + counters["parser_failed_batches"]
        + counters["unverified_records"]
        + counters["invalid_records"]
        + counters["unresolved_canonical_times"]
    )
    issues = []
    if review_required:
        issues.append(_issue(
            "data_quality_review_required",
            "warning",
            "Some imported or machine-parsed records require operational review.",
        ))
    return {
        "status": "warning" if review_required else "healthy",
        "counters": counters,
        "issues": issues,
    }


def _graph(as_of: datetime) -> dict[str, Any]:
    reads_enabled = relationship_reads_enabled()
    writes_enabled = relationship_projection_writes_enabled()
    enabled = reads_enabled or writes_enabled
    freshness = RelationshipProjectionRepository().freshness(OWNER_EMAIL)
    published = _instant(freshness.published_at)
    age = _age_days(published, as_of)
    issues = []
    status = "inactive"
    if enabled and freshness.latest_run_status == "failed":
        status = "error"
        issues.append(_issue(
            "graph_projection_failed",
            "critical",
            "The latest relationship projection failed; the prior published graph remains active.",
        ))
    elif enabled and published is None:
        status = "error"
        issues.append(_issue(
            "graph_not_published",
            "critical",
            "Relationship reads or writes are enabled without a published graph.",
        ))
    elif enabled and age is not None and age > GRAPH_MAX_AGE_DAYS:
        status = "stale"
        issues.append(_issue(
            "graph_projection_stale",
            "warning",
            f"The relationship projection is older than {GRAPH_MAX_AGE_DAYS} days.",
        ))
    elif enabled:
        status = "current"
    return {
        "status": status,
        "reads_enabled": reads_enabled,
        "writes_enabled": writes_enabled,
        "published_at": _utc(published),
        "freshness_days": age,
        "freshness_limit_days": GRAPH_MAX_AGE_DAYS,
        "latest_run_status": freshness.latest_run_status,
        "latest_run_started_at": _utc(_instant(freshness.latest_run_started_at)),
        "latest_run_completed_at": _utc(_instant(freshness.latest_run_completed_at)),
        "issues": issues,
    }


def _analytics(connection, as_of: datetime) -> dict[str, Any]:
    generated: dict[str, str | None] = {}
    missing_confidence = 0
    for entity_type in ("Pattern", "Insight"):
        row = connection.execute(
            """
            SELECT MAX(json_extract(data, '$.date_generated')),
                   COALESCE(SUM(
                       json_extract(data, '$.is_active')=1
                       AND json_type(data, '$.analytics_confidence') IS NULL
                   ),0)
            FROM entities
            WHERE type=? AND json_extract(data, '$.owner_email')=?
            """,
            (entity_type, OWNER_EMAIL),
        ).fetchone()
        generated[entity_type] = _utc(_instant(row[0] if row else None))
        missing_confidence += int(row[1] if row else 0)
    latest = max(
        (_instant(value) for value in generated.values() if value),
        default=None,
    )
    age = _age_days(latest, as_of)
    issues = []
    status = "inactive" if latest is None else "current"
    if age is not None and age > ANALYTICS_MAX_AGE_DAYS:
        status = "stale"
        issues.append(_issue(
            "analytics_stale",
            "warning",
            f"Pattern and Insight analytics are older than {ANALYTICS_MAX_AGE_DAYS} days.",
        ))
    if missing_confidence:
        if status == "current":
            status = "warning"
        issues.append(_issue(
            "analytics_confidence_missing",
            "warning",
            "Some current analytics lack the shared confidence contract.",
        ))
    return {
        "status": status,
        "latest_generated_at": _utc(latest),
        "freshness_days": age,
        "freshness_limit_days": ANALYTICS_MAX_AGE_DAYS,
        "by_type": generated,
        "missing_confidence_count": missing_confidence,
        "issues": issues,
    }


def _backups(as_of: datetime, backup_root: Path) -> dict[str, Any]:
    latest = None
    invalid = 0
    unreadable = False
    try:
        if backup_root.is_dir() and not backup_root.is_symlink():
            for candidate in backup_root.iterdir():
                if not candidate.is_dir() or candidate.is_symlink():
                    continue
                try:
                    manifest = _load_manifest(candidate)
                except (BackupError, OSError):
                    invalid += 1
                    continue
                created = _instant(manifest.get("created_at"))
                if created and (latest is None or created > latest):
                    latest = created
    except OSError:
        unreadable = True
    age = _age_days(latest, as_of)
    issues = []
    status = "current"
    if latest is None:
        status = "unavailable"
        issues.append(_issue(
            "verified_backup_unavailable",
            "warning",
            "No checksummed backup manifest is visible to the application.",
        ))
    elif age is not None and age > BACKUP_MAX_AGE_DAYS:
        status = "stale"
        issues.append(_issue(
            "verified_backup_stale",
            "warning",
            f"The newest visible backup is older than {BACKUP_MAX_AGE_DAYS} days.",
        ))
    if invalid:
        if status == "current":
            status = "warning"
        issues.append(_issue(
            "invalid_backup_manifests",
            "warning",
            "One or more backup directories have an invalid or unreadable manifest.",
        ))
    if unreadable:
        if status == "current":
            status = "warning"
        issues.append(_issue(
            "backup_location_unreadable",
            "warning",
            "The configured backup location is not readable by the application.",
        ))
    return {
        "status": status,
        "latest_created_at": _utc(latest),
        "age_days": age,
        "freshness_limit_days": BACKUP_MAX_AGE_DAYS,
        "invalid_manifest_count": invalid,
        "issues": issues,
    }


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def _storage(as_of: datetime, backup_root: Path) -> dict[str, Any]:
    database = db.DB_PATH
    wal = Path(str(database) + "-wal")
    return {
        "database_bytes": _file_size(database),
        "wal_bytes": _file_size(wal),
        "backup": _backups(as_of, backup_root),
    }


def _caveats(
    sources: list[dict[str, Any]],
    quality: dict[str, Any],
    graph: dict[str, Any],
    analytics: dict[str, Any],
    backup: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    caveats = []
    for source in sources:
        for issue in source["issues"]:
            caveats.append({
                **issue,
                "category": "source_health",
                "scope": source["source"],
                "domain": f"source:{source['source']}",
            })
    blocks = [
        ("data_quality", quality),
        ("graph_freshness", graph),
        ("analytics_freshness", analytics),
    ]
    if backup is not None:
        blocks.append(("backup_freshness", backup))
    for category, block in blocks:
        for issue in block["issues"]:
            caveats.append({
                **issue,
                "category": category,
                "scope": category,
                "domain": category,
            })
    return sorted(caveats, key=lambda item: (
        item["severity"],
        item["category"],
        item["scope"],
        item["code"],
    ))


def build_diagnostics(
    *,
    as_of: datetime | None = None,
    backup_root: Path | None = None,
) -> dict[str, Any]:
    as_of = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    backup_root = Path(backup_root or MIGRATION_BACKUP_DIR)
    with db.connect() as connection:
        sources = _source_diagnostics(connection, as_of)
        quality = _quality(connection)
        analytics = _analytics(connection, as_of)
    graph = _graph(as_of)
    storage = _storage(as_of, backup_root)
    caveats = _caveats(sources, quality, graph, analytics, storage["backup"])
    if any(item["severity"] == "critical" for item in caveats):
        status = "critical"
    elif caveats:
        status = "warning"
    else:
        status = "healthy"
    return {
        "contract_version": CONTRACT_VERSION,
        "generated_at": _utc(as_of),
        "status": status,
        "semantics": _semantics(),
        "sources": sources,
        "quality": quality,
        "graph": graph,
        "analytics": analytics,
        "storage": storage,
        "caveats": caveats,
    }


def reasoning_context(
    *,
    as_of: date | datetime | None = None,
) -> dict[str, Any]:
    if isinstance(as_of, date) and not isinstance(as_of, datetime):
        as_of = datetime.combine(as_of, time.max, tzinfo=timezone.utc)
    as_of = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with db.connect() as connection:
        sources = _source_diagnostics(connection, as_of)
        quality = _quality(connection)
        analytics = _analytics(connection, as_of)
    graph = _graph(as_of)
    caveats = _caveats(sources, quality, graph, analytics)
    return {
        "contract_version": CONTRACT_VERSION,
        "generated_at": _utc(as_of),
        "semantics": _semantics(),
        "sources": [
            {
                key: source[key]
                for key in (
                    "source",
                    "label",
                    "status",
                    "last_successful_sync_at",
                    "data_through",
                    "freshness_days",
                    "issues",
                )
            }
            for source in sources
            if source["status"] != "inactive"
        ],
        "caveats": caveats,
    }


@router.get("")
def platform_diagnostics():
    return build_diagnostics()
