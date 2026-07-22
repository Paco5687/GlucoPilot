"""Auditable medical-document extraction and lab verification.

``LabResult`` remains the compatibility projection consumed by existing pages.
This module preserves the richer extraction, validation, verification, and
correction history in additive relational tables introduced by migration 7.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

from . import db
from .config import OWNER_EMAIL
from .data_contracts import DEPLOYMENT_OWNER_ID

PARSER_VERSION = "medical-record-parser-2.0.0"
SCHEMA_VERSION = "lab-extraction/1.0.0"
INPUT_DATA_VERSION = "source-document-sha256/1"

_TRUE = {"1", "true", "yes", "on"}
_VALID_FLAGS = {"", "normal", "high", "low", "critical", "abnormal", "reported"}
_SPECIMENS = {
    "serum": {"serum"},
    "plasma": {"plasma"},
    "urine": {"urine", "urinary"},
    "blood": {"blood", "whole blood"},
    "saliva": {"saliva", "salivary"},
    "csf": {"csf", "cerebrospinal"},
}


class LabAuditError(ValueError):
    """A review action or audited extraction is not valid."""


class LabAuditRepository(Protocol):
    """Compatibility boundary for the additive audit sidecar and JSON view."""

    def start_run(
        self,
        record_id: str,
        source_hash: str,
        page_count: int,
        source_file_id: str | None = None,
    ) -> str: ...

    def fail_run(self, run_id: str, error: Exception) -> None: ...

    def replace_unverified_with_run(
        self,
        run_id: str,
        record_id: str,
        observations: list[dict[str, Any]],
        *,
        failed_batches: int = 0,
    ) -> tuple[list[dict[str, Any]], int]: ...

    def record_extractions(self, record_id: str) -> dict[str, Any]: ...

    def review_observation(
        self,
        record_id: str,
        observation_id: str,
        action: str,
        patch: dict[str, Any] | None = None,
        reason: str = "",
    ) -> dict[str, Any]: ...


def enabled() -> bool:
    return os.getenv("LAB_EXTRACTION_AUDIT_ENABLED", "true").strip().lower() in _TRUE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _finite(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _source_key(record_id: str, row: dict[str, Any]) -> str:
    location = row.get("extraction_location") or row.get("location") or ""
    if isinstance(location, dict):
        location = json.dumps(location, sort_keys=True, separators=(",", ":"))
    payload = "\0".join(
        (
            record_id,
            _text(row.get("source_page") or row.get("page")),
            _text(location).lower(),
            _text(row.get("original_test_name") or row.get("test_name") or row.get("name")).lower(),
            _text(row.get("specimen")).lower(),
            _text(row.get("original_collected_date") or row.get("collected_date")),
            _text(row.get("category")).lower(),
        )
    )
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def _issue(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def _value_parts(row: dict[str, Any]) -> tuple[str, float | None, str]:
    raw = row.get("original_value")
    if raw is None:
        raw = row.get("value_text")
    if raw is None:
        raw = row.get("value")
    original = _text(raw)
    explicit = _text(row.get("value_kind")).lower()
    if explicit == "titer" or re.fullmatch(r"\s*1\s*:\s*\d+(?:\.\d+)?\s*", original):
        denominator = _finite(original.split(":", 1)[1]) if ":" in original else _finite(row.get("value"))
        return original, denominator, "titer"
    numeric = _finite(row.get("normalized_value"))
    if numeric is None:
        numeric = _finite(row.get("value"))
    if explicit == "numeric":
        return original, numeric, "numeric"
    if explicit == "qualitative" or numeric is None:
        return original, None, "qualitative"
    return original, numeric, "numeric"


def _specimen_conflict(name: str, specimen: str) -> bool:
    if not specimen:
        return False
    lowered_name = name.lower()
    lowered_specimen = specimen.lower()
    claimed = {
        canonical
        for canonical, aliases in _SPECIMENS.items()
        if any(re.search(rf"\b{re.escape(alias)}\b", lowered_name) for alias in aliases)
    }
    supplied = {
        canonical
        for canonical, aliases in _SPECIMENS.items()
        if any(alias in lowered_specimen for alias in aliases)
    }
    return bool(claimed and supplied and claimed.isdisjoint(supplied))


def _normalized_row(row: dict[str, Any], record_id: str, *, measurement: bool = False) -> dict[str, Any] | None:
    original_name = _text(row.get("original_test_name") or row.get("original_name") or row.get("name") or row.get("test_name"))
    normalized_name = _text(row.get("test_name") or row.get("normalized_name") or row.get("name"))
    if not original_name or not normalized_name:
        return None
    original_value, value, value_kind = _value_parts(row)
    if not original_value:
        return None

    original_unit = _text(row.get("original_unit") if row.get("original_unit") is not None else row.get("unit"))
    normalized_unit = _text(row.get("unit") or row.get("normalized_unit"))
    original_low = row.get("reference_low")
    original_high = row.get("reference_high")
    low, high = _finite(original_low), _finite(original_high)
    range_text = _text(row.get("original_reference_range") or row.get("reference_range"))
    if not range_text and (original_low is not None or original_high is not None):
        range_text = f"{_text(original_low)}–{_text(original_high)}"
    original_flag = _text(row.get("original_flag") if row.get("original_flag") is not None else row.get("flag"))
    normalized_flag = _text(row.get("flag") or row.get("normalized_flag")).lower()
    if measurement:
        normalized_flag = {
            "enlarged": "high", "elevated": "high", "large": "high",
            "small": "low", "decreased": "low",
        }.get(normalized_flag, normalized_flag)
    original_date = _text(row.get("original_collected_date") or row.get("collected_date"))
    normalized_date = _text(row.get("collected_date") or row.get("normalized_collected_date"))
    specimen = _text(row.get("specimen"))
    issues: list[dict[str, str]] = []

    if value_kind == "qualitative":
        issues.append(_issue("qualitative_value", "warning", "Qualitative result is preserved for review and is not projected into numeric trends."))
    elif value_kind == "titer":
        issues.append(_issue("titer_value", "warning", "Titer is preserved in its reported form and is not projected as an ordinary numeric result."))
    if value_kind == "numeric" and value is None:
        issues.append(_issue("invalid_numeric_value", "invalid", "The reported value is not a finite number."))
    if low is not None and high is not None and low > high:
        issues.append(_issue("impossible_reference_range", "invalid", "Reference range lower bound exceeds its upper bound."))
        low = high = None
    if normalized_flag not in _VALID_FLAGS:
        issues.append(_issue("unknown_flag", "warning", "The reported flag is not in the normalized flag vocabulary."))
    if _specimen_conflict(original_name, specimen):
        issues.append(_issue("specimen_mismatch", "invalid", "The specimen conflicts with the specimen named in the result."))

    source_page = row.get("source_page") or row.get("page")
    try:
        source_page = int(source_page) if source_page not in (None, "") else None
    except (TypeError, ValueError):
        source_page = None
        issues.append(_issue("invalid_source_page", "warning", "Source page could not be normalized."))
    if source_page is not None and source_page < 1:
        source_page = None
        issues.append(_issue("invalid_source_page", "warning", "Source page must be one or greater."))
    location = row.get("extraction_location") or row.get("location") or {}
    if isinstance(location, str):
        location = {"description": location}
    if not isinstance(location, dict):
        location = {"description": _text(location)}
    confidence = _finite(row.get("parser_confidence"))
    if confidence is not None and not 0 <= confidence <= 1:
        confidence = None
        issues.append(_issue("invalid_parser_confidence", "warning", "Parser confidence was outside the 0–1 range."))

    status = "invalid" if any(i["severity"] == "invalid" for i in issues) else "warning" if issues else "valid"
    return {
        "stable_source_key": _source_key(record_id, row),
        "original_name": original_name,
        "normalized_name": normalized_name,
        "original_value": original_value,
        "normalized_value": value,
        "value_kind": value_kind,
        "original_unit": original_unit,
        "normalized_unit": normalized_unit,
        "original_reference_range": range_text,
        "reference_low": low,
        "reference_high": high,
        "original_flag": original_flag,
        "normalized_flag": normalized_flag,
        "specimen": specimen,
        "original_collected_date": original_date,
        "normalized_collected_date": normalized_date,
        "category": _text(row.get("category") or ("Imaging" if measurement else "")),
        "source_page": source_page,
        "extraction_location": location,
        "parser_confidence": confidence,
        "validation_status": status,
        "validation_issues": issues,
    }


def normalize_and_validate(extracted: dict[str, Any], record_id: str) -> list[dict[str, Any]]:
    """Preserve and validate all extracted result types without writing storage."""
    rows: list[dict[str, Any]] = []
    for raw in extracted.get("lab_results") or []:
        if isinstance(raw, dict) and (row := _normalized_row(raw, record_id)):
            if not row["normalized_collected_date"]:
                row["normalized_collected_date"] = _text(extracted.get("record_date"))
            if not row["original_collected_date"]:
                row["original_collected_date"] = row["normalized_collected_date"]
            rows.append(row)
    for raw in extracted.get("measurements") or []:
        if isinstance(raw, dict) and (row := _normalized_row(raw, record_id, measurement=True)):
            row["normalized_collected_date"] = row["normalized_collected_date"] or _text(extracted.get("record_date"))
            row["original_collected_date"] = row["original_collected_date"] or row["normalized_collected_date"]
            rows.append(row)

    seen: dict[tuple[Any, ...], int] = {}
    identity_units: dict[tuple[str, str, str], set[str]] = {}
    for index, row in enumerate(rows):
        duplicate_key = (
            row["normalized_name"].lower(),
            row["normalized_collected_date"],
            row["specimen"].lower(),
            row["original_value"].lower(),
            row["normalized_unit"].lower(),
        )
        if duplicate_key in seen:
            row["validation_issues"].append(
                _issue("duplicate_result", "warning", "This result duplicates another extraction at the same source location.")
            )
            row["duplicate_of_index"] = seen[duplicate_key]
        else:
            seen[duplicate_key] = index
        identity = (
            row["normalized_name"].lower(), row["normalized_collected_date"], row["specimen"].lower()
        )
        if row["normalized_unit"]:
            identity_units.setdefault(identity, set()).add(row["normalized_unit"].lower())

    for row in rows:
        identity = (
            row["normalized_name"].lower(), row["normalized_collected_date"], row["specimen"].lower()
        )
        if len(identity_units.get(identity, set())) > 1:
            row["validation_issues"].append(
                _issue("unit_conflict", "warning", "The same test, date, and specimen was extracted with conflicting units.")
            )
        if row["validation_status"] != "invalid" and row["validation_issues"]:
            row["validation_status"] = "warning"
    return rows


def start_run(
    record_id: str,
    source_hash: str,
    page_count: int,
    source_file_id: str | None = None,
) -> str:
    run_id = "labrun_" + uuid.uuid4().hex
    now = _now()
    with db.connect() as connection:
        connection.execute(
            """
            INSERT INTO lab_extraction_runs (
                id, owner_id, record_entity_id, source_file_id, source_hash, parser_version,
                schema_version, input_data_version, status, page_count,
                started_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
            """,
            (
                run_id, DEPLOYMENT_OWNER_ID, record_id, source_file_id, source_hash, PARSER_VERSION,
                SCHEMA_VERSION, INPUT_DATA_VERSION, page_count, now, now,
            ),
        )
    return run_id


def fail_run(run_id: str, error: Exception) -> None:
    # Exception strings from model providers can echo source text or credentials.
    # Persist only the error class in the audit ledger; detailed logs remain local.
    safe_error = f"{type(error).__name__}: extraction failed"
    with db.connect() as connection:
        connection.execute(
            """
            UPDATE lab_extraction_runs
            SET status='failed', completed_at=?, error_summary=?
            WHERE id=? AND owner_id=? AND status='running'
            """,
            (_now(), safe_error, run_id, DEPLOYMENT_OWNER_ID),
        )


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["extraction_location"] = json.loads(result.pop("extraction_location_json") or "{}")
    result["validation_issues"] = json.loads(result.pop("validation_issues_json") or "[]")
    return result


def _next_version(connection: sqlite3.Connection, record_id: str, stable_key: str) -> int:
    row = connection.execute(
        """
        SELECT COALESCE(MAX(version), 0) AS version
        FROM lab_extraction_observations
        WHERE owner_id=? AND record_entity_id=? AND stable_source_key=?
        """,
        (DEPLOYMENT_OWNER_ID, record_id, stable_key),
    ).fetchone()
    return int(row["version"]) + 1


def _insert_observation(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    record_id: str,
    observation: dict[str, Any],
    legacy_entity_id: str | None,
    verification_status: str = "unverified",
    supersedes_id: str | None = None,
    superseded_by_id: str | None = None,
) -> dict[str, Any]:
    observation_id = "labobs_" + uuid.uuid4().hex
    now = _now()
    version = _next_version(connection, record_id, observation["stable_source_key"])
    connection.execute(
        """
        INSERT INTO lab_extraction_observations (
            id, owner_id, record_entity_id, extraction_run_id, legacy_entity_id,
            stable_source_key, version, original_name, normalized_name,
            original_value, normalized_value, value_kind, original_unit,
            normalized_unit, original_reference_range, reference_low,
            reference_high, original_flag, normalized_flag, specimen,
            original_collected_date, normalized_collected_date, category,
            source_page, extraction_location_json, parser_confidence,
            validation_status, validation_issues_json, verification_status,
            supersedes_observation_id, superseded_by_observation_id,
            superseded_at, created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            observation_id, DEPLOYMENT_OWNER_ID, record_id, run_id, legacy_entity_id,
            observation["stable_source_key"], version, observation["original_name"],
            observation["normalized_name"], observation["original_value"],
            observation["normalized_value"], observation["value_kind"],
            observation["original_unit"], observation["normalized_unit"],
            observation["original_reference_range"], observation["reference_low"],
            observation["reference_high"], observation["original_flag"],
            observation["normalized_flag"], observation["specimen"],
            observation["original_collected_date"], observation["normalized_collected_date"],
            observation["category"], observation["source_page"],
            json.dumps(observation["extraction_location"], sort_keys=True),
            observation["parser_confidence"], observation["validation_status"],
            json.dumps(observation["validation_issues"], sort_keys=True),
            verification_status, supersedes_id, superseded_by_id,
            now if verification_status == "superseded" else None, now, now,
        ),
    )
    row = connection.execute(
        "SELECT * FROM lab_extraction_observations WHERE id=?", (observation_id,)
    ).fetchone()
    return _row_dict(row)


def _projection(observation: dict[str, Any], record_id: str) -> dict[str, Any] | None:
    if observation["value_kind"] != "numeric" or observation["normalized_value"] is None:
        return None
    duplicate = any(issue["code"] == "duplicate_result" for issue in observation["validation_issues"])
    if duplicate:
        return None
    return {
        "test_name": observation["normalized_name"],
        "value": observation["normalized_value"],
        "unit": observation["normalized_unit"],
        "reference_low": observation["reference_low"],
        "reference_high": observation["reference_high"],
        "flag": observation["normalized_flag"],
        "collected_date": observation["normalized_collected_date"],
        "category": observation["category"],
        "specimen": observation["specimen"],
        "record_id": record_id,
        "owner_email": OWNER_EMAIL,
        "stable_source_key": observation["stable_source_key"],
        "source_page": observation["source_page"],
        "extraction_location": observation["extraction_location"],
        "parser_confidence": observation["parser_confidence"],
        "validation_status": observation["validation_status"],
        "validation_issues": observation["validation_issues"],
        "verification_status": "unverified",
        "original_test_name": observation["original_name"],
        "original_value": observation["original_value"],
        "original_unit": observation["original_unit"],
        "original_reference_range": observation["original_reference_range"],
        "original_flag": observation["original_flag"],
        "original_collected_date": observation["original_collected_date"],
        "extraction_parser_version": PARSER_VERSION,
        "extraction_schema_version": SCHEMA_VERSION,
    }


def replace_unverified_with_run(
    run_id: str,
    record_id: str,
    observations: list[dict[str, Any]],
    *,
    failed_batches: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Project a run while retaining any approved/edited compatibility rows."""
    created: list[dict[str, Any]] = []
    preserved_keys: set[str] = set()
    with db.connect() as connection:
        old_labs = db.query_entities(
            "LabResult",
            {"record_id": record_id, "owner_email": OWNER_EMAIL},
            connection=connection,
        )
        preserved = {
            lab.get("stable_source_key"): lab
            for lab in old_labs
            if lab.get("verification_status") in {"approved", "edited"}
            and lab.get("stable_source_key")
        }
        preserved_observations = {
            row["stable_source_key"]: row
            for row in connection.execute(
                """
                SELECT * FROM lab_extraction_observations
                WHERE owner_id=? AND record_entity_id=?
                  AND verification_status IN ('approved', 'edited')
                  AND superseded_at IS NULL
                ORDER BY version DESC
                """,
                (DEPLOYMENT_OWNER_ID, record_id),
            ).fetchall()
        }
        for lab in old_labs:
            if lab.get("verification_status") not in {"approved", "edited"}:
                db.delete_entity("LabResult", lab["id"], connection=connection)

        active_old = connection.execute(
            """
            SELECT * FROM lab_extraction_observations
            WHERE owner_id=? AND record_entity_id=?
              AND verification_status='unverified' AND superseded_at IS NULL
            """,
            (DEPLOYMENT_OWNER_ID, record_id),
        ).fetchall()

        for observation in observations:
            retained = preserved.get(observation["stable_source_key"])
            retained_observation = preserved_observations.get(observation["stable_source_key"])
            if retained or retained_observation:
                preserved_keys.add(observation["stable_source_key"])
                retained_observation_id = (
                    retained.get("extraction_observation_id")
                    if retained
                    else retained_observation["id"]
                )
                _insert_observation(
                    connection,
                    run_id=run_id,
                    record_id=record_id,
                    observation=observation,
                    legacy_entity_id=(
                        retained["id"] if retained else retained_observation["legacy_entity_id"]
                    ),
                    verification_status="superseded",
                    superseded_by_id=retained_observation_id,
                )
                continue
            projection = _projection(observation, record_id)
            legacy = db.create_entity("LabResult", projection, connection=connection) if projection else None
            audited = _insert_observation(
                connection,
                run_id=run_id,
                record_id=record_id,
                observation=observation,
                legacy_entity_id=legacy["id"] if legacy else None,
            )
            if legacy:
                legacy = db.update_entity(
                    "LabResult",
                    legacy["id"],
                    {"extraction_observation_id": audited["id"]},
                    connection=connection,
                )
                created.append(legacy)

        for old in active_old:
            replacement = connection.execute(
                """
                SELECT id FROM lab_extraction_observations
                WHERE owner_id=? AND record_entity_id=? AND stable_source_key=?
                  AND extraction_run_id=? AND verification_status!='superseded'
                ORDER BY version DESC LIMIT 1
                """,
                (DEPLOYMENT_OWNER_ID, record_id, old["stable_source_key"], run_id),
            ).fetchone()
            connection.execute(
                """
                UPDATE lab_extraction_observations
                SET verification_status='superseded', superseded_by_observation_id=?,
                    superseded_at=?, updated_at=?
                WHERE id=? AND verification_status='unverified'
                """,
                (replacement["id"] if replacement else None, _now(), _now(), old["id"]),
            )

        connection.execute(
            """
            UPDATE lab_extraction_runs
            SET status=?, failed_batch_count=?, completed_at=?
            WHERE id=? AND owner_id=? AND status='running'
            """,
            ("partial" if failed_batches else "succeeded", failed_batches, _now(), run_id, DEPLOYMENT_OWNER_ID),
        )
    return created, len(preserved_keys)


def _event_payload(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "normalized_name", "normalized_value", "normalized_unit", "reference_low",
        "reference_high", "normalized_flag", "specimen", "normalized_collected_date",
        "category", "validation_status", "validation_issues", "verification_status",
    )
    return {key: row.get(key) for key in keys}


def _record_event(
    connection: sqlite3.Connection,
    row: dict[str, Any],
    action: str,
    before: dict[str, Any],
    after: dict[str, Any],
    reason: str,
) -> None:
    connection.execute(
        """
        INSERT INTO lab_verification_events (
            id, owner_id, record_entity_id, observation_id, action, actor,
            reason, before_json, after_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "labevent_" + uuid.uuid4().hex, DEPLOYMENT_OWNER_ID,
            row["record_entity_id"], row["id"], action, "deployment_admin",
            reason[:500], json.dumps(before, sort_keys=True),
            json.dumps(after, sort_keys=True), _now(),
        ),
    )


def review(record_id: str, lab_id: str, action: str, patch: dict[str, Any] | None = None, reason: str = "") -> dict[str, Any]:
    if action not in {"approve", "edit", "reject"}:
        raise LabAuditError("action must be approve, edit, or reject")
    patch = patch or {}
    with db.connect() as connection:
        lab_rows = db.query_entities(
            "LabResult", {"id": lab_id, "record_id": record_id, "owner_email": OWNER_EMAIL},
            limit=1, connection=connection,
        )
        if not lab_rows:
            raise LabAuditError("lab result not found")
        lab = lab_rows[0]
        row = connection.execute(
            """
            SELECT * FROM lab_extraction_observations
            WHERE id=? AND owner_id=? AND record_entity_id=? AND legacy_entity_id=?
            """,
            (lab.get("extraction_observation_id"), DEPLOYMENT_OWNER_ID, record_id, lab_id),
        ).fetchone()
        if not row:
            raise LabAuditError("lab result predates the audited extraction format; reprocess it before review")
        current = _row_dict(row)
        if current["verification_status"] == "superseded":
            raise LabAuditError("lab result has already been superseded")
        before = _event_payload(current)

        if action == "approve":
            if current["verification_status"] in {"approved", "edited"}:
                return {"lab": lab, "observation": current}
            if current["validation_status"] == "invalid":
                raise LabAuditError("invalid extraction must be corrected or rejected before approval")
            now = _now()
            connection.execute(
                "UPDATE lab_extraction_observations SET verification_status='approved', updated_at=? WHERE id=?",
                (now, current["id"]),
            )
            lab = db.update_entity(
                "LabResult", lab_id,
                {"verification_status": "approved", "verified_at": now},
                connection=connection,
            )
            after = {**before, "verification_status": "approved"}
            _record_event(connection, current, action, before, after, reason)
            return {"lab": lab, "observation": {**current, **after}}

        if action == "reject":
            now = _now()
            connection.execute(
                "UPDATE lab_extraction_observations SET verification_status='rejected', updated_at=? WHERE id=?",
                (now, current["id"]),
            )
            lab = db.update_entity(
                "LabResult", lab_id,
                {"verification_status": "rejected", "verified_at": now},
                connection=connection,
            )
            after = {**before, "verification_status": "rejected"}
            _record_event(connection, current, action, before, after, reason)
            return {"lab": lab, "observation": {**current, **after}}

        editable = {
            "test_name", "value", "unit", "reference_low", "reference_high",
            "flag", "specimen", "collected_date", "category",
        }
        corrected_input = {
            "original_test_name": current["original_name"],
            "test_name": patch.get("test_name", current["normalized_name"]),
            "original_value": current["original_value"],
            "normalized_value": patch.get("value", current["normalized_value"]),
            "value": patch.get("value", current["normalized_value"]),
            "original_unit": current["original_unit"],
            "unit": patch.get("unit", current["normalized_unit"]),
            "original_reference_range": current["original_reference_range"],
            "reference_low": patch.get("reference_low", current["reference_low"]),
            "reference_high": patch.get("reference_high", current["reference_high"]),
            "original_flag": current["original_flag"],
            "flag": patch.get("flag", current["normalized_flag"]),
            "specimen": patch.get("specimen", current["specimen"]),
            "original_collected_date": current["original_collected_date"],
            "collected_date": patch.get("collected_date", current["normalized_collected_date"]),
            "category": patch.get("category", current["category"]),
            "source_page": current["source_page"],
            "extraction_location": current["extraction_location"],
            "parser_confidence": current["parser_confidence"],
            "value_kind": current["value_kind"],
        }
        unknown = set(patch) - editable
        if unknown:
            raise LabAuditError(f"unsupported edit fields: {', '.join(sorted(unknown))}")
        corrected = _normalized_row(corrected_input, record_id)
        if not corrected or corrected["value_kind"] != "numeric" or corrected["normalized_value"] is None:
            raise LabAuditError("edited compatibility lab must have a finite numeric value")
        if corrected["validation_status"] == "invalid":
            raise LabAuditError("edited result still fails validation")
        corrected["stable_source_key"] = current["stable_source_key"]
        new_row = _insert_observation(
            connection,
            run_id=current["extraction_run_id"],
            record_id=record_id,
            observation=corrected,
            legacy_entity_id=lab_id,
            verification_status="edited",
            supersedes_id=current["id"],
        )
        now = _now()
        connection.execute(
            """
            UPDATE lab_extraction_observations
            SET verification_status='superseded', superseded_by_observation_id=?,
                superseded_at=?, updated_at=? WHERE id=?
            """,
            (new_row["id"], now, now, current["id"]),
        )
        lab = db.update_entity(
            "LabResult", lab_id,
            {
                "test_name": corrected["normalized_name"],
                "value": corrected["normalized_value"],
                "unit": corrected["normalized_unit"],
                "reference_low": corrected["reference_low"],
                "reference_high": corrected["reference_high"],
                "flag": corrected["normalized_flag"],
                "specimen": corrected["specimen"],
                "collected_date": corrected["normalized_collected_date"],
                "category": corrected["category"],
                "validation_status": corrected["validation_status"],
                "validation_issues": corrected["validation_issues"],
                "verification_status": "edited",
                "verified_at": now,
                "extraction_observation_id": new_row["id"],
            },
            connection=connection,
        )
        after = _event_payload(new_row)
        _record_event(connection, new_row, action, before, after, reason)
        return {"lab": lab, "observation": new_row}


def review_observation(
    record_id: str,
    observation_id: str,
    action: str,
    patch: dict[str, Any] | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """Review any current extraction, including qualitative/titer-only rows."""
    with db.connect() as connection:
        raw = connection.execute(
            """
            SELECT * FROM lab_extraction_observations
            WHERE id=? AND owner_id=? AND record_entity_id=?
            """,
            (observation_id, DEPLOYMENT_OWNER_ID, record_id),
        ).fetchone()
    if not raw:
        raise LabAuditError("extraction not found")
    current = _row_dict(raw)
    if current["verification_status"] == "superseded":
        raise LabAuditError("extraction has already been superseded")
    if current.get("legacy_entity_id"):
        return review(record_id, current["legacy_entity_id"], action, patch, reason)
    if action not in {"approve", "edit", "reject"}:
        raise LabAuditError("action must be approve, edit, or reject")

    with db.connect() as connection:
        before = _event_payload(current)
        if action in {"approve", "reject"}:
            if action == "approve" and current["verification_status"] in {"approved", "edited"}:
                return {"lab": None, "observation": current}
            if action == "approve" and current["validation_status"] == "invalid":
                raise LabAuditError("invalid extraction must be corrected or rejected before approval")
            status = "approved" if action == "approve" else "rejected"
            connection.execute(
                "UPDATE lab_extraction_observations SET verification_status=?, updated_at=? WHERE id=?",
                (status, _now(), observation_id),
            )
            after = {**before, "verification_status": status}
            _record_event(connection, current, action, before, after, reason)
            return {"lab": None, "observation": {**current, **after}}

        patch = patch or {}
        editable = {
            "test_name", "value", "value_kind", "unit", "reference_low",
            "reference_high", "flag", "specimen", "collected_date", "category",
        }
        unknown = set(patch) - editable
        if unknown:
            raise LabAuditError(f"unsupported edit fields: {', '.join(sorted(unknown))}")
        corrected_input = {
            "original_test_name": current["original_name"],
            "test_name": patch.get("test_name", current["normalized_name"]),
            "original_value": current["original_value"],
            "value": patch.get("value", current["normalized_value"] or current["original_value"]),
            "value_kind": patch.get("value_kind", current["value_kind"]),
            "original_unit": current["original_unit"],
            "unit": patch.get("unit", current["normalized_unit"]),
            "original_reference_range": current["original_reference_range"],
            "reference_low": patch.get("reference_low", current["reference_low"]),
            "reference_high": patch.get("reference_high", current["reference_high"]),
            "original_flag": current["original_flag"],
            "flag": patch.get("flag", current["normalized_flag"]),
            "specimen": patch.get("specimen", current["specimen"]),
            "original_collected_date": current["original_collected_date"],
            "collected_date": patch.get("collected_date", current["normalized_collected_date"]),
            "category": patch.get("category", current["category"]),
            "source_page": current["source_page"],
            "extraction_location": current["extraction_location"],
            "parser_confidence": current["parser_confidence"],
        }
        corrected = _normalized_row(corrected_input, record_id)
        if not corrected or corrected["validation_status"] == "invalid":
            raise LabAuditError("edited result still fails validation")
        corrected["stable_source_key"] = current["stable_source_key"]
        projection = _projection(corrected, record_id)
        legacy = db.create_entity("LabResult", projection, connection=connection) if projection else None
        new_row = _insert_observation(
            connection,
            run_id=current["extraction_run_id"],
            record_id=record_id,
            observation=corrected,
            legacy_entity_id=legacy["id"] if legacy else None,
            verification_status="edited",
            supersedes_id=current["id"],
        )
        if legacy:
            legacy = db.update_entity(
                "LabResult",
                legacy["id"],
                {
                    "verification_status": "edited",
                    "verified_at": _now(),
                    "extraction_observation_id": new_row["id"],
                },
                connection=connection,
            )
        now = _now()
        connection.execute(
            """
            UPDATE lab_extraction_observations
            SET verification_status='superseded', superseded_by_observation_id=?,
                superseded_at=?, updated_at=? WHERE id=?
            """,
            (new_row["id"], now, now, current["id"]),
        )
        _record_event(connection, new_row, action, before, _event_payload(new_row), reason)
        return {"lab": legacy, "observation": new_row}


def record_extractions(record_id: str) -> dict[str, Any]:
    with db.connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM lab_extraction_observations
            WHERE owner_id=? AND record_entity_id=? AND verification_status!='superseded'
            ORDER BY COALESCE(source_page, 2147483647), normalized_name, version DESC
            """,
            (DEPLOYMENT_OWNER_ID, record_id),
        ).fetchall()
        runs = connection.execute(
            """
            SELECT * FROM lab_extraction_runs
            WHERE owner_id=? AND record_entity_id=? ORDER BY started_at DESC
            """,
            (DEPLOYMENT_OWNER_ID, record_id),
        ).fetchall()
        observations = [_row_dict(row) for row in rows]
        for observation in observations:
            events = connection.execute(
                """
                SELECT event.action, event.actor, event.reason, event.before_json,
                       event.after_json, event.created_at
                FROM lab_verification_events AS event
                JOIN lab_extraction_observations AS version
                  ON version.id=event.observation_id
                WHERE event.owner_id=? AND version.record_entity_id=?
                  AND version.stable_source_key=?
                ORDER BY event.created_at
                """,
                (
                    DEPLOYMENT_OWNER_ID,
                    observation["record_entity_id"],
                    observation["stable_source_key"],
                ),
            ).fetchall()
            observation["history"] = [
                {
                    **dict(event),
                    "before": json.loads(event["before_json"]),
                    "after": json.loads(event["after_json"]),
                }
                for event in events
            ]
            for event in observation["history"]:
                event.pop("before_json")
                event.pop("after_json")
        return {
            "enabled": enabled(),
            "parser_version": PARSER_VERSION,
            "schema_version": SCHEMA_VERSION,
            "observations": observations,
            "runs": [dict(row) for row in runs],
        }


def qualification(lab: dict[str, Any]) -> dict[str, Any]:
    verification = _text(lab.get("verification_status") or "unverified").lower()
    validation = _text(lab.get("validation_status") or "unknown").lower()
    issues = lab.get("validation_issues") if isinstance(lab.get("validation_issues"), list) else []
    eligible = verification != "rejected" and validation != "invalid"
    return {
        "verification_status": verification,
        "validation_status": validation,
        "qualified": verification not in {"approved", "edited"} or validation not in {"valid", "unknown"},
        "summary_eligible": eligible,
        "limitations": [issue.get("message") for issue in issues if isinstance(issue, dict) and issue.get("message")],
    }


def summary_eligible(lab: dict[str, Any]) -> bool:
    return qualification(lab)["summary_eligible"]


class SqliteLabAuditRepository:
    """Production compatibility repository backed by migration-7 tables."""

    def start_run(
        self,
        record_id: str,
        source_hash: str,
        page_count: int,
        source_file_id: str | None = None,
    ) -> str:
        return start_run(record_id, source_hash, page_count, source_file_id)

    def fail_run(self, run_id: str, error: Exception) -> None:
        fail_run(run_id, error)

    def replace_unverified_with_run(
        self,
        run_id: str,
        record_id: str,
        observations: list[dict[str, Any]],
        *,
        failed_batches: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        return replace_unverified_with_run(
            run_id,
            record_id,
            observations,
            failed_batches=failed_batches,
        )

    def record_extractions(self, record_id: str) -> dict[str, Any]:
        return record_extractions(record_id)

    def review_observation(
        self,
        record_id: str,
        observation_id: str,
        action: str,
        patch: dict[str, Any] | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        return review_observation(record_id, observation_id, action, patch, reason)
