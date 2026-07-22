"""Deterministic clinical contradiction detection and auditable resolution.

Rules compare existing authoritative/compatibility data without replacing it.
Detections are materialized in additive typed tables.  Re-evaluation may mark
evidence as no longer current, but only an attributable user action can change
the resolution state.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from . import db
from .auth import current_user, require_admin, require_login, session_role
from .config import APP_TIMEZONE, OWNER_EMAIL
from .data_contracts import DEPLOYMENT_OWNER_ID
from .db import config_value
from .insulin_reconciliation import reconcile_treatments


RULES_VERSION = "clinical-contradictions/1.0.0"
REFRESH_SECONDS = 60
WINDOW_DAYS = 120
_TRUE = {"1", "true", "yes", "on"}
_RESOLUTION_KINDS = {
    "accepted_left",
    "accepted_right",
    "both_valid",
    "data_corrected",
    "not_applicable",
}
_PHASES = {"menstrual", "follicular", "ovulation", "luteal"}


class ContradictionError(RuntimeError):
    """Raised when contradiction lifecycle input is invalid."""


@runtime_checkable
class ContradictionRepository(Protocol):
    def reconcile(
        self,
        detections: list[dict[str, Any]],
        input_data_version: str,
    ) -> dict[str, Any]: ...

    def list(
        self,
        *,
        domains: tuple[str, ...] = (),
        include_resolved: bool = False,
        limit: int = 250,
    ) -> list[dict[str, Any]]: ...

    def resolve(
        self,
        contradiction_id: str,
        resolution_kind: str,
        note: str,
        actor: dict[str, Any],
    ) -> dict[str, Any]: ...

    def reopen(
        self,
        contradiction_id: str,
        note: str,
        actor: dict[str, Any],
    ) -> dict[str, Any]: ...


def contradiction_engine_enabled() -> bool:
    return os.getenv("CONTRADICTION_ENGINE_ENABLED", "true").strip().lower() in _TRUE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _detection(
    rule_id: str,
    domain: str,
    severity: str,
    subject_type: str,
    subject_key: str,
    explanation: str,
    left: dict[str, Any],
    right: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fingerprint = {
        "rule_id": rule_id,
        "rule_version": RULES_VERSION,
        "subject_type": subject_type,
        "subject_key": subject_key,
        "left": left,
        "right": right,
    }
    return {
        "detection_key": _hash(fingerprint),
        "rule_id": rule_id,
        "rule_version": RULES_VERSION,
        "domain": domain,
        "severity": severity,
        "subject_type": subject_type,
        "subject_key": subject_key,
        "explanation": explanation,
        "left": left,
        "right": right,
        "context": context or {},
    }


def detect_tdd_contradictions(reconciliation: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare source-reported TDD candidates and complete calculated delivery."""
    detections: list[dict[str, Any]] = []
    for day in reconciliation.get("days") or []:
        date_label = str(day.get("date") or "")
        reported = day.get("pump_reported") or {}
        candidates = sorted(
            [candidate for candidate in reported.get("candidates") or [] if candidate.get("complete")],
            key=lambda candidate: (
                str(candidate.get("source") or ""),
                float(candidate.get("total_units") or 0),
            ),
        )
        if reported.get("conflict") and len(candidates) >= 2:
            left, right = candidates[0], candidates[1]
            detections.append(
                _detection(
                    "pump_tdd.reported_source_conflict",
                    "pump_tdd",
                    "blocking",
                    "pump_day",
                    date_label,
                    "Two complete pump sources report different daily insulin totals; neither is selected silently.",
                    {
                        "label": "Pump-reported source A",
                        "source": left.get("source"),
                        "value": left.get("total_units"),
                        "unit": "units/day",
                    },
                    {
                        "label": "Pump-reported source B",
                        "source": right.get("source"),
                        "value": right.get("total_units"),
                        "unit": "units/day",
                    },
                    {"date": date_label, "candidate_count": len(candidates)},
                )
            )

        discrepancy = day.get("discrepancy") or {}
        selected = reported.get("selected")
        calculated = (day.get("calculated") or {}).get("total_units")
        if selected and calculated is not None and not discrepancy.get("matches_rounding", True):
            absolute = float(discrepancy.get("absolute_units") or 0)
            percent = _number(discrepancy.get("percent_of_reported"))
            severity = "blocking" if absolute >= 5 or (percent is not None and percent >= 20) else "warning"
            detections.append(
                _detection(
                    "pump_tdd.reported_vs_calculated",
                    "pump_tdd",
                    severity,
                    "pump_day",
                    date_label,
                    "The pump-reported daily total differs from the total reconstructed from complete delivered basal and bolus events.",
                    {
                        "label": "Pump reported",
                        "source": selected.get("source"),
                        "value": selected.get("total_units"),
                        "unit": "units/day",
                    },
                    {
                        "label": "Calculated from delivery events",
                        "source": "complete_delivered_basal_plus_bolus",
                        "value": calculated,
                        "unit": "units/day",
                    },
                    {
                        "date": date_label,
                        "absolute_difference_units": absolute,
                        "percent_of_reported": percent,
                        "blocking_threshold": "5 units or 20%",
                    },
                )
            )
    return detections


def detect_glucose_contradictions(fingersticks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compare the preserved meter value with its contemporaneous CGM match."""
    detections: list[dict[str, Any]] = []
    for row in fingersticks:
        meter = _number(row.get("value"))
        cgm = _number(row.get("cgm_value"))
        if meter is None or cgm is None:
            continue
        delta = cgm - meter
        warning_threshold = max(20.0, abs(meter) * 0.20)
        if abs(delta) < warning_threshold:
            continue
        blocking_threshold = max(40.0, abs(meter) * 0.30)
        severity = "blocking" if abs(delta) >= blocking_threshold else "warning"
        subject_key = str(row.get("id") or f"{row.get('timestamp')}:{meter:g}:{cgm:g}")
        detections.append(
            _detection(
                "glucose.cgm_vs_fingerstick",
                "glucose",
                severity,
                "FingerstickReading",
                subject_key,
                "A paired CGM value and fingerstick meter value differ beyond the deterministic review threshold; both readings are retained.",
                {
                    "label": "Fingerstick meter",
                    "entity_type": "FingerstickReading",
                    "entity_id": row.get("id"),
                    "value": meter,
                    "unit": "mg/dL",
                    "observed_at": row.get("timestamp"),
                    "source": row.get("source") or "manual",
                },
                {
                    "label": "Paired CGM",
                    "entity_type": "GlucoseReading",
                    "value": cgm,
                    "unit": "mg/dL",
                    "observed_at": row.get("cgm_timestamp") or row.get("timestamp"),
                    "source": row.get("cgm_source") or "paired_cgm",
                },
                {
                    "delta_mg_dl": round(delta, 1),
                    "absolute_delta_mg_dl": round(abs(delta), 1),
                    "warning_threshold_mg_dl": round(warning_threshold, 1),
                    "blocking_threshold_mg_dl": round(blocking_threshold, 1),
                    "threshold_is_review_signal_not_device_accuracy_claim": True,
                },
            )
        )
    return detections


def _lab_side(row: dict[str, Any], label: str) -> dict[str, Any]:
    return {
        "label": label,
        "entity_type": "LabExtractionObservation",
        "entity_id": row.get("id"),
        "record_id": row.get("record_entity_id") or row.get("record_id"),
        "name": row.get("normalized_name") or row.get("test_name"),
        "value": row.get("normalized_value") if "normalized_value" in row else row.get("value"),
        "unit": row.get("normalized_unit") if "normalized_unit" in row else row.get("unit"),
        "reference_low": row.get("reference_low"),
        "reference_high": row.get("reference_high"),
        "observed_at": row.get("normalized_collected_date") or row.get("collected_date"),
        "source_page": row.get("source_page"),
    }


def detect_lab_contradictions(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Find conflicting units and ranges for the same test/date/specimen."""
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in sorted(observations, key=lambda item: (str(item.get("id") or ""), _canonical(item))):
        if row.get("verification_status") in {"rejected", "superseded"} or row.get("superseded_at"):
            continue
        name = str(row.get("normalized_name") or row.get("test_name") or "").strip()
        collected = str(row.get("normalized_collected_date") or row.get("collected_date") or "").strip()
        specimen = str(row.get("specimen") or "").strip().lower()
        if name and collected:
            groups.setdefault((name.lower(), collected, specimen), []).append(row)

    detections: list[dict[str, Any]] = []
    for (name, collected, specimen), rows in sorted(groups.items()):
        subject_key = f"{name}|{collected}|{specimen}"
        by_unit: dict[str, dict[str, Any]] = {}
        for row in rows:
            unit = str(row.get("normalized_unit") or row.get("unit") or "").strip().lower()
            if unit:
                by_unit.setdefault(unit, row)
        if len(by_unit) > 1:
            representatives = [by_unit[key] for key in sorted(by_unit)]
            detections.append(
                _detection(
                    "labs.conflicting_units",
                    "labs",
                    "blocking",
                    "lab_identity",
                    subject_key,
                    "The same test, collection date, and specimen was normalized with conflicting units.",
                    _lab_side(representatives[0], "Lab result A"),
                    _lab_side(representatives[1], "Lab result B"),
                    {"distinct_units": sorted(by_unit), "result_count": len(rows)},
                )
            )

        ranges_by_unit: dict[str, dict[tuple[Any, Any], dict[str, Any]]] = {}
        for row in rows:
            low, high = row.get("reference_low"), row.get("reference_high")
            if low is None and high is None:
                continue
            unit = str(row.get("normalized_unit") or row.get("unit") or "").strip().lower()
            ranges_by_unit.setdefault(unit, {}).setdefault((low, high), row)
        for unit, by_range in sorted(ranges_by_unit.items()):
            if len(by_range) < 2:
                continue
            keys = sorted(by_range, key=_canonical)
            detections.append(
                _detection(
                    "labs.conflicting_reference_ranges",
                    "labs",
                    "warning",
                    "lab_identity",
                    f"{subject_key}|{unit}",
                    "The same test, collection date, and specimen has different reported reference ranges; both source ranges remain visible.",
                    _lab_side(by_range[keys[0]], "Lab range A"),
                    _lab_side(by_range[keys[1]], "Lab range B"),
                    {"distinct_range_count": len(by_range), "result_count": len(rows)},
                )
            )
    return detections


def detect_hormone_timing_contradictions(
    labs: list[dict[str, Any]],
    period_logs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compare an explicitly declared expected cycle phase with observed logs.

    The engine never invents a clinically preferred phase.  A rule fires only
    when the source/normalized lab explicitly supplies ``expected_cycle_phase``.
    """
    phases_by_date: dict[str, set[str]] = {}
    logs_by_date: dict[str, list[dict[str, Any]]] = {}
    for log in sorted(period_logs, key=lambda item: (str(item.get("id") or ""), _canonical(item))):
        date_label = str(log.get("date") or "")[:10]
        phase = str(log.get("phase") or "").strip().lower()
        if date_label and phase in _PHASES:
            phases_by_date.setdefault(date_label, set()).add(phase)
            logs_by_date.setdefault(date_label, []).append(log)

    detections: list[dict[str, Any]] = []
    for lab in sorted(labs, key=lambda item: (str(item.get("id") or ""), _canonical(item))):
        raw_expected = lab.get("expected_cycle_phase")
        if isinstance(raw_expected, str):
            expected = {value.strip().lower() for value in raw_expected.split(",")}
        elif isinstance(raw_expected, list):
            expected = {str(value).strip().lower() for value in raw_expected}
        else:
            expected = set()
        expected &= _PHASES
        date_label = str(lab.get("collected_date") or "")[:10]
        observed = phases_by_date.get(date_label, set())
        if not expected or not observed or expected & observed:
            continue
        log = sorted(logs_by_date[date_label], key=lambda row: str(row.get("id") or ""))[0]
        subject_key = str(lab.get("id") or f"{lab.get('test_name')}:{date_label}")
        detections.append(
            _detection(
                "labs.hormone_cycle_phase_timing",
                "hormone_timing",
                "warning",
                "LabResult",
                subject_key,
                "The lab's explicitly declared expected cycle phase does not match the recorded phase on its collection date.",
                {
                    "label": "Lab timing declaration",
                    "entity_type": "LabResult",
                    "entity_id": lab.get("id"),
                    "name": lab.get("test_name"),
                    "observed_at": date_label,
                    "expected_cycle_phases": sorted(expected),
                },
                {
                    "label": "Recorded cycle phase",
                    "entity_type": "PeriodLog",
                    "entity_id": log.get("id"),
                    "observed_at": date_label,
                    "cycle_phases": sorted(observed),
                    "source": log.get("source"),
                },
                {"clinical_phase_expectation_was_source_declared": True},
            )
        )
    return detections


def detect_revised_source_contradictions(source_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Detect changed immutable payloads sharing one provider external identity."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in source_records:
        source_type = str(row.get("source_type") or "").strip()
        external_id = str(row.get("external_id") or "").strip()
        payload_hash = str(row.get("payload_hash") or "").strip()
        if source_type and external_id and payload_hash:
            groups.setdefault((source_type, external_id), []).append(row)

    detections: list[dict[str, Any]] = []
    for (source_type, external_id), rows in sorted(groups.items()):
        by_hash = {str(row["payload_hash"]): row for row in rows}
        if len(by_hash) < 2:
            continue
        versions = sorted(
            by_hash.values(),
            key=lambda row: (str(row.get("received_at") or ""), str(row.get("id") or "")),
        )
        left, right = versions[-2], versions[-1]
        subject_key = _hash({"source_type": source_type, "external_id": external_id})
        detections.append(
            _detection(
                "source.revised_external_record",
                "source_revision",
                "warning",
                "source_external_identity",
                subject_key,
                "An immutable provider record was received again with changed content; both source versions are retained for review.",
                {
                    "label": "Earlier source version",
                    "entity_type": "source_record",
                    "entity_id": left.get("id"),
                    "source": source_type,
                    "observed_at": left.get("observed_at"),
                    "received_at": left.get("received_at"),
                    "content_hash": left.get("payload_hash"),
                },
                {
                    "label": "Revised source version",
                    "entity_type": "source_record",
                    "entity_id": right.get("id"),
                    "source": source_type,
                    "observed_at": right.get("observed_at"),
                    "received_at": right.get("received_at"),
                    "content_hash": right.get("payload_hash"),
                },
                {"version_count": len(by_hash), "external_identity_redacted": True},
            )
        )
    return detections


def evaluate_snapshot(snapshot: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Return stable ordered detections and an input version for pure fixture tests."""
    detections = [
        *detect_tdd_contradictions(snapshot.get("tdd_reconciliation") or {}),
        *detect_glucose_contradictions(snapshot.get("fingersticks") or []),
        *detect_lab_contradictions(snapshot.get("lab_observations") or []),
        *detect_hormone_timing_contradictions(
            snapshot.get("labs") or [], snapshot.get("period_logs") or []
        ),
        *detect_revised_source_contradictions(snapshot.get("source_records") or []),
    ]
    detections.sort(key=lambda row: (row["domain"], row["rule_id"], row["subject_key"], row["detection_key"]))
    ordered = lambda values: sorted(values, key=_canonical)  # noqa: E731 - compact canonicalizer
    input_snapshot = {
        "rules_version": RULES_VERSION,
        "tdd_input_data_version": (snapshot.get("tdd_reconciliation") or {}).get("input_data_version"),
        "fingersticks": ordered(snapshot.get("fingersticks") or []),
        "lab_observations": ordered(snapshot.get("lab_observations") or []),
        "labs": ordered(snapshot.get("labs") or []),
        "period_logs": ordered(snapshot.get("period_logs") or []),
        "source_records": ordered(snapshot.get("source_records") or []),
    }
    return detections, _hash(input_snapshot)


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    output = dict(row)
    output["left"] = json.loads(output.pop("left_json"))
    output["right"] = json.loads(output.pop("right_json"))
    output["context"] = json.loads(output.pop("context_json"))
    return output


def _event_state(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "detection_state": row.get("detection_state"),
        "resolution_state": row.get("resolution_state"),
        "resolution_kind": row.get("resolution_kind"),
        "resolution_note": row.get("resolution_note") or "",
        "resolved_by": row.get("resolved_by"),
        "resolved_at": row.get("resolved_at"),
    }


class SqliteContradictionRepository:
    """Typed compatibility repository for the migration-8 contradiction ledger."""

    def __init__(self, connection: sqlite3.Connection | None = None) -> None:
        self._connection = connection

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

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        contradiction_id: str,
        action: str,
        actor: dict[str, Any],
        before: dict[str, Any],
        after: dict[str, Any],
        reason: str = "",
    ) -> None:
        connection.execute(
            """
            INSERT INTO contradiction_events (
                id, owner_id, contradiction_id, action, actor_id, actor_role,
                actor_name, reason, before_json, after_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "contrevent_" + uuid.uuid4().hex,
                DEPLOYMENT_OWNER_ID,
                contradiction_id,
                action,
                str(actor.get("id") or "unknown")[:200],
                str(actor.get("role") or "unknown")[:100],
                str(actor.get("name") or actor.get("full_name") or "unknown")[:200],
                str(reason or "")[:1000],
                _canonical(before),
                _canonical(after),
                _now(),
            ),
        )

    def reconcile(
        self,
        detections: list[dict[str, Any]],
        input_data_version: str,
    ) -> dict[str, Any]:
        run_id = "contrun_" + uuid.uuid4().hex
        now = _now()
        created = 0
        reactivated = 0
        marked_not_current = 0
        actor = {"id": "rule_engine", "role": "system", "name": RULES_VERSION}
        with self._scope() as connection:
            connection.execute(
                """
                INSERT INTO contradiction_runs (
                    id, owner_id, rules_version, input_data_version, status,
                    started_at, created_at
                ) VALUES (?, ?, ?, ?, 'running', ?, ?)
                """,
                (run_id, DEPLOYMENT_OWNER_ID, RULES_VERSION, input_data_version, now, now),
            )
            seen: set[str] = set()
            for detection in detections:
                key = detection["detection_key"]
                seen.add(key)
                existing_row = connection.execute(
                    "SELECT * FROM contradictions WHERE owner_id=? AND detection_key=?",
                    (DEPLOYMENT_OWNER_ID, key),
                ).fetchone()
                if existing_row is None:
                    contradiction_id = "contr_" + uuid.uuid4().hex
                    connection.execute(
                        """
                        INSERT INTO contradictions (
                            id, owner_id, contradiction_run_id, detection_key,
                            rule_id, rule_version, domain, subject_type, subject_key,
                            severity, explanation, left_json, right_json, context_json,
                            detection_state, resolution_state, first_detected_at,
                            last_detected_at, created_at, updated_at
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            'active', 'unresolved', ?, ?, ?, ?
                        )
                        """,
                        (
                            contradiction_id,
                            DEPLOYMENT_OWNER_ID,
                            run_id,
                            key,
                            detection["rule_id"],
                            detection["rule_version"],
                            detection["domain"],
                            detection["subject_type"],
                            detection["subject_key"],
                            detection["severity"],
                            detection["explanation"],
                            _canonical(detection["left"]),
                            _canonical(detection["right"]),
                            _canonical(detection["context"]),
                            now,
                            now,
                            now,
                            now,
                        ),
                    )
                    created_row = _row_dict(
                        connection.execute(
                            "SELECT * FROM contradictions WHERE id=?", (contradiction_id,)
                        ).fetchone()
                    )
                    self._insert_event(
                        connection,
                        contradiction_id,
                        "detected",
                        actor,
                        {},
                        _event_state(created_row),
                    )
                    created += 1
                    continue

                existing = _row_dict(existing_row)
                was_not_current = existing["detection_state"] == "not_current"
                connection.execute(
                    """
                    UPDATE contradictions
                    SET contradiction_run_id=?, detection_state='active',
                        no_longer_detected_at=NULL, last_detected_at=?, updated_at=?
                    WHERE id=? AND owner_id=?
                    """,
                    (run_id, now, now, existing["id"], DEPLOYMENT_OWNER_ID),
                )
                if was_not_current:
                    current = {**existing, "detection_state": "active"}
                    self._insert_event(
                        connection,
                        existing["id"],
                        "detected",
                        actor,
                        _event_state(existing),
                        _event_state(current),
                    )
                    reactivated += 1

            active_rows = connection.execute(
                "SELECT * FROM contradictions WHERE owner_id=? AND detection_state='active'",
                (DEPLOYMENT_OWNER_ID,),
            ).fetchall()
            for row in active_rows:
                existing = _row_dict(row)
                if existing["detection_key"] in seen:
                    continue
                connection.execute(
                    """
                    UPDATE contradictions
                    SET contradiction_run_id=?, detection_state='not_current',
                        no_longer_detected_at=?, updated_at=?
                    WHERE id=? AND owner_id=?
                    """,
                    (run_id, now, now, existing["id"], DEPLOYMENT_OWNER_ID),
                )
                current = {**existing, "detection_state": "not_current"}
                self._insert_event(
                    connection,
                    existing["id"],
                    "not_current",
                    actor,
                    _event_state(existing),
                    _event_state(current),
                )
                marked_not_current += 1

            connection.execute(
                """
                UPDATE contradiction_runs
                SET status='succeeded', detection_count=?, completed_at=?
                WHERE id=? AND owner_id=?
                """,
                (len(detections), now, run_id, DEPLOYMENT_OWNER_ID),
            )
        return {
            "run_id": run_id,
            "input_data_version": input_data_version,
            "detection_count": len(detections),
            "created": created,
            "reactivated": reactivated,
            "marked_not_current": marked_not_current,
        }

    def list(
        self,
        *,
        domains: tuple[str, ...] = (),
        include_resolved: bool = False,
        limit: int = 250,
    ) -> list[dict[str, Any]]:
        where = ["c.owner_id=?"]
        parameters: list[Any] = [DEPLOYMENT_OWNER_ID]
        if not include_resolved:
            where.append("c.resolution_state='unresolved'")
        if domains:
            where.append(f"c.domain IN ({','.join('?' for _ in domains)})")
            parameters.extend(domains)
        parameters.append(max(1, min(int(limit), 1000)))
        with self._scope() as connection:
            rows = connection.execute(
                f"""
                SELECT c.* FROM contradictions c
                WHERE {' AND '.join(where)}
                ORDER BY
                    CASE c.resolution_state WHEN 'unresolved' THEN 0 ELSE 1 END,
                    CASE c.severity WHEN 'blocking' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                    CASE c.detection_state WHEN 'active' THEN 0 ELSE 1 END,
                    c.last_detected_at DESC, c.id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            output = [_row_dict(row) for row in rows]
            by_id = {row["id"]: row for row in output}
            if by_id:
                placeholders = ",".join("?" for _ in by_id)
                events = connection.execute(
                    f"""
                    SELECT * FROM contradiction_events
                    WHERE owner_id=? AND contradiction_id IN ({placeholders})
                    ORDER BY created_at, id
                    """,
                    (DEPLOYMENT_OWNER_ID, *by_id),
                ).fetchall()
                for event in events:
                    item = dict(event)
                    item["before"] = json.loads(item.pop("before_json"))
                    item["after"] = json.loads(item.pop("after_json"))
                    by_id[item["contradiction_id"]].setdefault("history", []).append(item)
            for row in output:
                row.setdefault("history", [])
            return output

    def _get(self, connection: sqlite3.Connection, contradiction_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM contradictions WHERE id=? AND owner_id=?",
            (contradiction_id, DEPLOYMENT_OWNER_ID),
        ).fetchone()
        if not row:
            raise ContradictionError("contradiction not found")
        return _row_dict(row)

    def resolve(
        self,
        contradiction_id: str,
        resolution_kind: str,
        note: str,
        actor: dict[str, Any],
    ) -> dict[str, Any]:
        resolution_kind = str(resolution_kind or "").strip()
        note = str(note or "").strip()
        if resolution_kind not in _RESOLUTION_KINDS:
            raise ContradictionError("invalid resolution kind")
        with self._scope() as connection:
            current = self._get(connection, contradiction_id)
            if current["resolution_state"] == "resolved":
                return current
            if current["severity"] == "blocking" and not note:
                raise ContradictionError("blocking contradictions require an explicit resolution note")
            now = _now()
            actor_id = str(actor.get("id") or "unknown")[:200]
            connection.execute(
                """
                UPDATE contradictions
                SET resolution_state='resolved', resolution_kind=?, resolution_note=?,
                    resolved_by=?, resolved_at=?, updated_at=?
                WHERE id=? AND owner_id=? AND resolution_state='unresolved'
                """,
                (
                    resolution_kind,
                    note[:1000],
                    actor_id,
                    now,
                    now,
                    contradiction_id,
                    DEPLOYMENT_OWNER_ID,
                ),
            )
            updated = self._get(connection, contradiction_id)
            self._insert_event(
                connection,
                contradiction_id,
                "resolved",
                actor,
                _event_state(current),
                _event_state(updated),
                note,
            )
            return updated

    def reopen(
        self,
        contradiction_id: str,
        note: str,
        actor: dict[str, Any],
    ) -> dict[str, Any]:
        note = str(note or "").strip()
        if not note:
            raise ContradictionError("reopening a contradiction requires a reason")
        with self._scope() as connection:
            current = self._get(connection, contradiction_id)
            if current["resolution_state"] == "unresolved":
                return current
            now = _now()
            connection.execute(
                """
                UPDATE contradictions
                SET resolution_state='unresolved', resolution_kind=NULL,
                    resolution_note='', resolved_by=NULL, resolved_at=NULL, updated_at=?
                WHERE id=? AND owner_id=? AND resolution_state='resolved'
                """,
                (now, contradiction_id, DEPLOYMENT_OWNER_ID),
            )
            updated = self._get(connection, contradiction_id)
            self._insert_event(
                connection,
                contradiction_id,
                "reopened",
                actor,
                _event_state(current),
                _event_state(updated),
                note,
            )
            return updated


def _load_snapshot(now: datetime | None = None) -> dict[str, Any]:
    # Local import avoids the repository catalog's contradiction-repository
    # construction cycle while still routing paired glucose evidence through
    # the I9 shadow/read-switch boundary.
    from .repositories import get_repositories

    current = now or datetime.now(timezone.utc)
    timezone_name = config_value("app_timezone", APP_TIMEZONE)
    tz = ZoneInfo(timezone_name)
    since = current - timedelta(days=WINDOW_DAYS)
    since_iso = since.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    treatments = db.query_entities(
        "Treatment",
        {"owner_email": OWNER_EMAIL, "timestamp": {"$gte": since_iso}},
        "timestamp",
        100000,
    )
    reconciliation = reconcile_treatments(
        treatments,
        tz,
        start_date=since.astimezone(tz).date().isoformat(),
        end_date=current.astimezone(tz).date().isoformat(),
    )
    fingersticks = get_repositories().fingersticks.query(
        {"owner_email": OWNER_EMAIL, "timestamp": {"$gte": since_iso}},
        "timestamp",
        10000,
    )
    labs = db.query_entities("LabResult", {"owner_email": OWNER_EMAIL}, "collected_date", 100000)
    period_logs = db.query_entities("PeriodLog", {"owner_email": OWNER_EMAIL}, "date", 10000)
    with db.connect() as connection:
        lab_observations = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, record_entity_id, normalized_name, normalized_value,
                       normalized_unit, reference_low, reference_high, specimen,
                       normalized_collected_date, source_page, verification_status,
                       superseded_at
                FROM lab_extraction_observations
                WHERE owner_id=?
                """,
                (DEPLOYMENT_OWNER_ID,),
            ).fetchall()
        ]
        source_records = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, source_type, external_id, observed_at, received_at,
                       payload_hash, created_at
                FROM source_records
                WHERE owner_id=? AND external_id IS NOT NULL AND external_id!=''
                ORDER BY source_type, external_id, received_at, id
                """,
                (DEPLOYMENT_OWNER_ID,),
            ).fetchall()
        ]
    return {
        "tdd_reconciliation": reconciliation,
        "fingersticks": fingersticks,
        "lab_observations": lab_observations,
        "labs": labs,
        "period_logs": period_logs,
        "source_records": source_records,
    }


_refresh_lock = threading.Lock()
_last_refresh_monotonic = 0.0


def refresh_current(*, force: bool = False) -> dict[str, Any]:
    global _last_refresh_monotonic
    if not contradiction_engine_enabled():
        return {"enabled": False, "refreshed": False}
    with _refresh_lock:
        if not force and time.monotonic() - _last_refresh_monotonic < REFRESH_SECONDS:
            return {"enabled": True, "refreshed": False}
        snapshot = _load_snapshot()
        detections, input_data_version = evaluate_snapshot(snapshot)
        result = SqliteContradictionRepository().reconcile(detections, input_data_version)
        _last_refresh_monotonic = time.monotonic()
        return {"enabled": True, "refreshed": True, **result}


def contradiction_context(
    *,
    refresh: bool = True,
    domains: tuple[str, ...] = (),
    limit: int = 50,
) -> dict[str, Any]:
    refresh_result = refresh_current() if refresh else {"enabled": contradiction_engine_enabled()}
    if not contradiction_engine_enabled():
        return {"enabled": False, "unresolved": [], "counts": {}}
    rows = SqliteContradictionRepository().list(domains=domains, limit=limit)
    counts = {"blocking": 0, "warning": 0, "info": 0, "active": 0, "not_current": 0}
    for row in rows:
        counts[row["severity"]] = counts.get(row["severity"], 0) + 1
        counts[row["detection_state"]] = counts.get(row["detection_state"], 0) + 1
    return {
        "enabled": True,
        "refresh": refresh_result,
        "counts": counts,
        "unresolved": [{key: value for key, value in row.items() if key != "history"} for row in rows],
        "note": "Unresolved contradictions preserve both sides. Blocking items require an explicit attributed resolution.",
    }


router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/api/contradictions")
def list_contradictions(
    domains: str = Query(default=""),
    include_resolved: bool = Query(default=False),
    refresh: bool = Query(default=True),
    limit: int = Query(default=250, ge=1, le=1000),
):
    selected = tuple(sorted({value.strip() for value in domains.split(",") if value.strip()}))
    refresh_result = refresh_current() if refresh else {"enabled": contradiction_engine_enabled()}
    if not contradiction_engine_enabled():
        return {"enabled": False, "refresh": refresh_result, "contradictions": [], "counts": {}}
    rows = SqliteContradictionRepository().list(
        domains=selected,
        include_resolved=include_resolved,
        limit=limit,
    )
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["severity"]] = counts.get(row["severity"], 0) + 1
    return {
        "enabled": True,
        "refresh": refresh_result,
        "contradictions": rows,
        "counts": counts,
    }


class ResolveBody(BaseModel):
    resolution_kind: str
    note: str = ""


class ReopenBody(BaseModel):
    note: str


def _actor(request: Request) -> dict[str, Any]:
    role = session_role(request)
    user = current_user(role, request.session.get("provider_name", ""))
    return {"id": user.get("id"), "role": role, "name": user.get("full_name") or "admin"}


@router.post("/api/contradictions/{contradiction_id}/resolve")
def resolve_contradiction(contradiction_id: str, body: ResolveBody, request: Request):
    require_admin(request)
    try:
        return SqliteContradictionRepository().resolve(
            contradiction_id,
            body.resolution_kind,
            body.note,
            _actor(request),
        )
    except ContradictionError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/contradictions/{contradiction_id}/reopen")
def reopen_contradiction(contradiction_id: str, body: ReopenBody, request: Request):
    require_admin(request)
    try:
        return SqliteContradictionRepository().reopen(
            contradiction_id,
            body.note,
            _actor(request),
        )
    except ContradictionError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
