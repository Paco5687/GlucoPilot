"""Deterministic, allowlist-only privacy-reviewed share exports."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import db
from .auth import require_login, session_role
from .clinician_briefs import build_brief
from .config import OWNER_EMAIL
from .evidence_bundle import EvidenceBundleQuery, EvidenceDomain, build_bundle


router = APIRouter(prefix="/api/share-exports", dependencies=[Depends(require_login)])
POLICY_VERSION = "share-export-policy/1.0.0"
MODES = {
    "full_private": {"roles": {"admin"}, "expires_days": 7, "watermark": "PRIVATE — intended recipient only"},
    "clinician": {"roles": {"admin", "provider"}, "expires_days": 14, "watermark": "CLINICAL REVIEW COPY — not a complete medical record"},
    "emergency": {"roles": {"admin", "provider"}, "expires_days": 2, "watermark": "EMERGENCY SUMMARY — verify with the patient"},
    "anonymized_research": {"roles": {"admin"}, "expires_days": 30, "watermark": "ANONYMIZED RESEARCH COPY — no re-identification"},
    "demo": {"roles": {"admin"}, "expires_days": 30, "watermark": "SYNTHETIC DEMO DATA — not a real person"},
}
ENTITY_FIELDS = {
    "full_private": {
        "Diagnosis": ("name", "diagnosed_date", "status", "notes"),
        "Medication": ("name", "dose", "frequency", "route", "status", "notes"),
        "Allergy": ("allergen", "reaction", "severity", "notes"),
        "HealthProfile": (
            "diabetes_type",
            "blood_type",
            "sex",
            "height_cm",
            "weight_kg",
            "units",
        ),
        "WeightLog": ("date", "weight_kg"),
        "SymptomLog": ("symptom", "severity", "entry_date", "notes"),
        "HistoryEntry": ("title", "category", "entry_date", "notes"),
        "GlucoseReading": ("timestamp", "value", "trend", "source"),
        "FingerstickReading": ("timestamp", "value", "context"),
        "Treatment": ("timestamp", "type", "amount", "carbs", "duration"),
        "LabResult": ("test_name", "value", "unit", "reference_range", "flag", "collected_date", "verification_status"),
        "PeriodLog": ("date", "flow", "phase", "symptoms"),
        "OuraDaily": ("date", "sleep_score", "readiness_score", "activity_score", "hrv", "resting_heart_rate"),
        "FitbitDaily": ("date", "steps", "resting_heart_rate", "sleep_minutes"),
    },
    "emergency": {
        "Diagnosis": ("name", "status"),
        "Medication": ("name", "dose", "frequency", "route", "status"),
        "Allergy": ("allergen", "reaction", "severity"),
        "HealthProfile": (
            "diabetes_type",
            "blood_type",
            "sex",
            "height_cm",
            "weight_kg",
            "units",
        ),
    },
}
RESEARCH_FIELDS = {
    "OuraDaily": ("sleep_score", "readiness_score", "activity_score", "hrv", "resting_heart_rate"),
    "FitbitDaily": ("steps", "resting_heart_rate", "sleep_minutes"),
    "DailySummary": ("average_glucose", "time_in_range", "total_insulin", "carbs"),
    "WeeklySummary": ("average_glucose", "time_in_range", "total_insulin", "carbs"),
}
TEMPORAL_FIELDS = {
    "SymptomLog": "entry_date",
    "HistoryEntry": "entry_date",
    "GlucoseReading": "timestamp",
    "FingerstickReading": "timestamp",
    "Treatment": "timestamp",
    "LabResult": "collected_date",
    "PeriodLog": "date",
    "OuraDaily": "date",
    "FitbitDaily": "date",
    "WeightLog": "date",
}
FORBIDDEN_PARTS = (
    "id", "token", "secret", "password", "credential", "authorization", "cookie",
    "email", "employer", "member_id", "subscriber_id", "rx_id", "rx_bin",
    "bin", "pcn", "group_number", "internal_id", "owner_id", "owner_email",
    "external_id",
)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
SECRET_TEXT_RE = re.compile(
    r"\b(token|secret|password|credential|authorization|member[_ -]?id|rx[_ -]?id)"
    r"\s*[:=]\s*\S+",
    re.IGNORECASE,
)


def _instant(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00",
        "Z",
    )


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str
    days: int = Field(default=90, ge=7, le=365)
    generated_at: datetime | None = None
    preview_checksum: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )

    @field_validator("mode")
    @classmethod
    def valid_mode(cls, value: str) -> str:
        if value not in MODES:
            raise ValueError("unsupported export mode")
        return value

    @field_validator("generated_at")
    @classmethod
    def utc_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value.astimezone(timezone.utc)


def _forbidden(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(
        normalized == part
        or normalized.startswith(f"{part}_")
        or normalized.endswith(f"_{part}")
        for part in FORBIDDEN_PARTS
    )


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _safe(item)
            for key, item in value.items()
            if not _forbidden(str(key)) and str(key).lower() not in {"id", "url", "href", "source_links"}
        }
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        if "://" in value or value.lower().startswith(("bearer ", "urn:glucopilot:owner:")):
            return "[omitted]"
        value = EMAIL_RE.sub("[omitted]", value)
        value = SECRET_TEXT_RE.sub("[omitted]", value)
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _in_window(row: dict[str, Any], field: str, cutoff: datetime, end: datetime) -> bool:
    try:
        observed = datetime.fromisoformat(str(row[field])[:10]).date()
    except (KeyError, TypeError, ValueError):
        return False
    return cutoff.date() <= observed <= end.date()


def _entities(mode: str, days: int, generated: datetime) -> dict[str, list[dict[str, Any]]]:
    cutoff = generated - timedelta(days=days)
    output = {}
    for entity_type, fields in ENTITY_FIELDS[mode].items():
        rows = db.query_entities(
            entity_type,
            {"owner_email": OWNER_EMAIL, "created_date": {"$lte": _instant(generated)}},
            "-created_date",
            500,
        )
        if mode == "full_private" and entity_type in TEMPORAL_FIELDS:
            rows = [
                row
                for row in rows
                if _in_window(row, TEMPORAL_FIELDS[entity_type], cutoff, generated)
            ]
        output[entity_type] = [
            _safe({field: row[field] for field in fields if field in row})
            for row in rows
        ]
    return output


def _clinician(days: int, generated: datetime) -> dict[str, Any]:
    brief = build_brief("clinician", days, end=generated)
    item_fields = (
        "entity_type",
        "title",
        "observed_at",
        "evidence_strength",
        "display_label",
        "description",
    )

    def brief_item(item: Any) -> dict[str, Any]:
        if not isinstance(item, dict):
            return {}
        return _safe({
            field: item[field]
            for field in item_fields
            if field in item and item[field] is not None
        })

    sections = {
        name: [brief_item(item) for item in brief["sections"].get(name, [])]
        for name in (
            "concerns",
            "objective_patterns",
            "glucose_insulin",
            "management_burden",
            "labs_imaging",
            "hypotheses",
        )
    }
    sections["reassuring_evidence"] = [
        {
            "evidence": brief_item(item.get("evidence")),
            "reason": _safe(item.get("reason")),
        }
        for item in brief["sections"].get("reassuring_evidence", [])
        if isinstance(item, dict)
    ]
    sections["opposing_evidence"] = [
        _safe({"reason": item.get("reason")})
        for item in brief["sections"].get("opposing_evidence", [])
        if isinstance(item, dict)
    ]
    sections["contradictions"] = [
        _safe({
            field: item.get(field)
            for field in ("rule", "severity", "status", "explanation")
            if field in item
        })
        for item in brief["sections"].get("contradictions", [])
        if isinstance(item, dict)
    ]
    sections["limitations"] = [
        _safe({
            field: item.get(field)
            for field in ("code", "domain", "message")
            if field in item
        })
        for item in brief["sections"].get("limitations", [])
        if isinstance(item, dict)
    ]
    sections["questions"] = _safe(brief["sections"].get("questions", []))
    return {
        "mode_label": brief["mode_label"],
        "window": brief["window"],
        "language": brief["language"],
        "privacy": _safe({
            field: brief["privacy"].get(field)
            for field in ("policy", "note")
            if field in brief["privacy"]
        }),
        "evidence_bundle_version": brief["evidence_bundle"]["version"],
        "sections": sections,
    }


def _research(days: int, generated: datetime) -> dict[str, Any]:
    start = generated - timedelta(days=days)
    bundle = build_bundle(EvidenceBundleQuery(
        start=start,
        end=generated,
        domains=(EvidenceDomain.ANALYTICS, EvidenceDomain.WEARABLES),
        question_intent="deidentified daily summary observations for research export",
        item_budget=250,
        normalized_entity_types=tuple(RESEARCH_FIELDS),
    ))
    observations = []
    for section in ("direct_observations", "derived_metrics"):
        for item in bundle["evidence"][section]:
            entity_type = item.get("entity_type")
            if entity_type not in RESEARCH_FIELDS:
                continue
            data = item.get("data") if isinstance(item.get("data"), dict) else {}
            confidence = item.get("confidence")
            observations.append({
                "observation_type": entity_type,
                "day_offset": max(
                    0,
                    (
                        datetime.fromisoformat(
                            str(item["observed_at"]).replace("Z", "+00:00")
                        ).date()
                        - start.date()
                    ).days,
                ),
                "values": _safe({
                    field: data[field]
                    for field in RESEARCH_FIELDS[entity_type]
                    if field in data
                }),
                "confidence": _safe({
                    field: confidence.get(field)
                    for field in ("label", "score", "method", "discovery_status")
                    if isinstance(confidence, dict) and field in confidence
                }),
            })
    return {
        "cohort": "single-participant-deidentified",
        "time_axis": "days_from_window_start",
        "bundle_version": bundle["bundle_version"],
        "bundle_confidence": _safe(bundle["confidence"]),
        "observations": observations,
        "limitations": _safe(bundle["missing_data_caveats"]),
        "truncated": bundle["budget"]["truncated"],
    }


def _demo() -> dict[str, Any]:
    return {
        "synthetic": True,
        "conditions": [{"name": "Synthetic type 1 diabetes", "status": "active"}],
        "medications": [{"name": "Synthetic rapid-acting insulin", "frequency": "with meals"}],
        "summary": {"average_glucose": 123, "time_in_range": 78, "units": "mg/dL"},
    }


def build_export(body: ExportRequest, role: str) -> tuple[dict[str, Any], ExportRequest]:
    policy = MODES[body.mode]
    if role not in policy["roles"]:
        raise HTTPException(status_code=403, detail="export mode is not permitted for this role")
    generated = body.generated_at or datetime.now(timezone.utc)
    if body.mode in ENTITY_FIELDS:
        content = {"entities": _entities(body.mode, body.days, generated)}
    elif body.mode == "clinician":
        content = _clinician(body.days, generated)
    elif body.mode == "anonymized_research":
        content = _research(body.days, generated)
    else:
        content = _demo()
    payload = {
        "policy": {
            "version": POLICY_VERSION,
            "mode": body.mode,
            "field_policy": "explicit_allowlist",
            "watermark": policy["watermark"],
            "generated_at": generated.isoformat().replace("+00:00", "Z"),
            "expires_at": (generated + timedelta(days=policy["expires_days"])).isoformat().replace("+00:00", "Z"),
            "invariant_exclusions": list(FORBIDDEN_PARTS),
        },
        "content": _safe(content),
    }
    payload["checksum"] = "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    normalized = body.model_copy(update={
        "generated_at": generated,
        "preview_checksum": payload["checksum"],
    })
    return payload, normalized


@router.post("/preview")
def preview_export(request: Request, body: ExportRequest):
    if body.generated_at is not None or body.preview_checksum is not None:
        raise HTTPException(status_code=400, detail="preview metadata is server-generated")
    payload, normalized = build_export(body, session_role(request))
    response = JSONResponse({
        "request": normalized.model_dump(mode="json"),
        "export": payload,
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/download")
def download_export(request: Request, body: ExportRequest):
    if body.generated_at is None or body.preview_checksum is None:
        raise HTTPException(status_code=400, detail="preview-generated request is required")
    now = datetime.now(timezone.utc)
    if body.generated_at > now + timedelta(seconds=5):
        raise HTTPException(status_code=400, detail="preview generation time is invalid")
    if now > body.generated_at + timedelta(days=MODES[body.mode]["expires_days"]):
        raise HTTPException(status_code=410, detail="export preview expired; generate a new preview")
    payload, _normalized = build_export(body, session_role(request))
    if payload["checksum"] != body.preview_checksum:
        raise HTTPException(
            status_code=409,
            detail="export data changed after preview; generate a new preview",
        )
    response = JSONResponse(payload)
    response.headers["Content-Disposition"] = (
        f'attachment; filename="glucopilot-{body.mode}-export.json"'
    )
    response.headers["Cache-Control"] = "no-store"
    return response
