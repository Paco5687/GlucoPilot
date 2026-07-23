"""Deterministic, bounded evidence bundles over the governed data layer."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import db
from .activity_position import analysis_for_range as activity_position_analysis
from .auth import require_login
from .config import APP_TIMEZONE, OWNER_EMAIL
from .contradictions import SqliteContradictionRepository
from .claims import CLAIM_CONTRACT_VERSION, SqliteClaimVersionRepository
from .data_contracts import DEPLOYMENT_OWNER_ID
from .evidence_sets import (
    SqliteEvidenceSetRepository,
    StaleEvidenceError,
    evidence_set_reads_enabled,
)
from .lab_audit import qualification as lab_qualification
from .insulin_response import build_response_events
from .management_burden import analysis_for_range as management_burden_analysis
from .relationship_api import _public_edge
from .relationships import SqliteRelationshipRepository, relationship_reads_enabled
from .repositories import LegacyRepositoryCatalog


BUNDLE_GENERATOR = "evidence-bundle"
BUNDLE_VERSION = "2.5.0"
MAX_ITEM_BUDGET = 250
MAX_SOURCE_ROWS = 100_000
MAX_CONTRADICTIONS = 1_000
MAX_CACHE_ENTRIES = 128
ORDERING = (
    "question_intent_relevance_desc",
    "category_priority_desc",
    "observed_at_desc",
    "entity_type",
    "entity_id",
)


class EvidenceDomain(StrEnum):
    GLUCOSE = "glucose"
    INSULIN = "insulin"
    WEARABLES = "wearables"
    CYCLE = "cycle"
    LABS = "labs"
    RECORDS = "records"
    CLINICAL = "clinical"
    ANALYTICS = "analytics"


class EvidenceBundleQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime
    domains: tuple[EvidenceDomain, ...]
    question_intent: str = Field(min_length=1, max_length=500)
    item_budget: int = Field(default=50, ge=1, le=MAX_ITEM_BUDGET)
    normalized_entity_types: tuple[str, ...] | None = None

    @field_validator("start", "end")
    @classmethod
    def canonical_instant(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("time range must use timezone-aware instants")
        return value.astimezone(timezone.utc)

    @field_validator("domains")
    @classmethod
    def canonical_domains(cls, value: tuple[EvidenceDomain, ...]) -> tuple[EvidenceDomain, ...]:
        if not value:
            raise ValueError("at least one evidence domain is required")
        return tuple(sorted(set(value), key=str))

    @field_validator("question_intent")
    @classmethod
    def canonical_intent(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("normalized_entity_types")
    @classmethod
    def canonical_entity_types(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        selected = tuple(sorted(set(value)))
        if not selected:
            raise ValueError("normalized_entity_types cannot be empty")
        return selected

    @model_validator(mode="after")
    def valid_range(self) -> EvidenceBundleQuery:
        if self.end < self.start:
            raise ValueError("end cannot precede start")
        if self.normalized_entity_types is not None:
            available = set(_selected_types(self.domains))
            invalid = set(self.normalized_entity_types) - available
            if invalid:
                raise ValueError(
                    "normalized_entity_types must belong to the selected domains: "
                    + ", ".join(sorted(invalid))
                )
        return self


class EvidenceBundleError(RuntimeError):
    """Raised when a bounded bundle cannot be returned without hiding safety data."""


_ENTITY_DOMAIN: dict[str, EvidenceDomain] = {
    "GlucoseReading": EvidenceDomain.GLUCOSE,
    "FingerstickReading": EvidenceDomain.GLUCOSE,
    "Treatment": EvidenceDomain.INSULIN,
    "NightscoutProfile": EvidenceDomain.INSULIN,
    "OuraDaily": EvidenceDomain.WEARABLES,
    "OuraHeartRate": EvidenceDomain.WEARABLES,
    "FitbitDaily": EvidenceDomain.WEARABLES,
    "FitbitHeartRate": EvidenceDomain.WEARABLES,
    "ActivityPositionInterval": EvidenceDomain.WEARABLES,
    "PeriodLog": EvidenceDomain.CYCLE,
    "LabResult": EvidenceDomain.LABS,
    "MedicalRecord": EvidenceDomain.RECORDS,
    "WeightLog": EvidenceDomain.CLINICAL,
    "Diagnosis": EvidenceDomain.CLINICAL,
    "Medication": EvidenceDomain.CLINICAL,
    "Allergy": EvidenceDomain.CLINICAL,
    "SymptomLog": EvidenceDomain.CLINICAL,
    "HistoryEntry": EvidenceDomain.CLINICAL,
    "HealthProfile": EvidenceDomain.CLINICAL,
    "InsuranceInfo": EvidenceDomain.CLINICAL,
    "Pattern": EvidenceDomain.ANALYTICS,
    "Insight": EvidenceDomain.ANALYTICS,
    "DailySummary": EvidenceDomain.ANALYTICS,
    "WeeklySummary": EvidenceDomain.ANALYTICS,
    "HealthSummary": EvidenceDomain.ANALYTICS,
    "ActivityPositionEffect": EvidenceDomain.ANALYTICS,
    "ManagementBurdenSummary": EvidenceDomain.ANALYTICS,
}

_DOMAIN_TYPES: dict[EvidenceDomain, tuple[str, ...]] = {
    domain: tuple(sorted(entity_type for entity_type, assigned in _ENTITY_DOMAIN.items() if assigned == domain))
    for domain in EvidenceDomain
}
# Labs and records are one evidence chain: selecting either includes both ends.
_DOMAIN_TYPES[EvidenceDomain.LABS] = tuple(
    sorted(set(_DOMAIN_TYPES[EvidenceDomain.LABS]) | {"MedicalRecord"})
)
_DOMAIN_TYPES[EvidenceDomain.RECORDS] = tuple(
    sorted(set(_DOMAIN_TYPES[EvidenceDomain.RECORDS]) | {"LabResult"})
)

_TIME_FIELDS: dict[str, tuple[str, Literal["instant", "date"], bool]] = {
    "GlucoseReading": ("timestamp", "instant", False),
    "FingerstickReading": ("timestamp", "instant", False),
    "Treatment": ("timestamp", "instant", False),
    "OuraDaily": ("date", "date", False),
    "OuraHeartRate": ("timestamp", "instant", False),
    "FitbitDaily": ("date", "date", False),
    "FitbitHeartRate": ("timestamp", "instant", False),
    "PeriodLog": ("date", "date", False),
    "LabResult": ("collected_date", "date", True),
    "MedicalRecord": ("record_date", "date", True),
    "WeightLog": ("date", "date", True),
    "Diagnosis": ("diagnosed_date", "date", True),
    "SymptomLog": ("entry_date", "date", True),
    "HistoryEntry": ("entry_date", "date", True),
    "Pattern": ("last_detected", "instant", True),
    "Insight": ("date_generated", "instant", True),
    "DailySummary": ("date", "date", True),
    "WeeklySummary": ("week_start", "date", True),
    "HealthSummary": ("generated_at", "instant", True),
}

_DERIVED_TYPES = {"Pattern", "Insight", "DailySummary", "WeeklySummary", "HealthSummary"}
_CANONICAL_EPISODE_TYPES = {"HealthEpisode", "MedicationExposure"}
_DOCUMENT_TYPES = {"MedicalRecord"}
_CATEGORY_PRIORITY = {"derived_metric": 3, "document": 2, "direct_observation": 1, "relationship": 0}
_CONTRADICTION_DOMAINS: dict[EvidenceDomain, frozenset[str]] = {
    EvidenceDomain.GLUCOSE: frozenset({"glucose", "source_revision"}),
    EvidenceDomain.INSULIN: frozenset({"pump_tdd", "source_revision"}),
    EvidenceDomain.WEARABLES: frozenset({"source_revision"}),
    EvidenceDomain.CYCLE: frozenset({"hormone_timing", "source_revision"}),
    EvidenceDomain.LABS: frozenset({"labs", "hormone_timing", "source_revision"}),
    EvidenceDomain.RECORDS: frozenset({"labs", "hormone_timing", "source_revision"}),
    EvidenceDomain.CLINICAL: frozenset({"source_revision"}),
    EvidenceDomain.ANALYTICS: frozenset(),
}

_SECRET_PARTS = ("token", "password", "secret", "credential", "authorization", "cookie", "api_key")
_OMITTED_KEYS = {"owner_email", "stored_as", "file_path", "path"}
_TOKEN = re.compile(r"[a-z0-9]+")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _checksum(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _opaque(value: str | None) -> str | None:
    return _checksum(str(value)) if value else None


def _instant(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    """Bound response size and remove credentials/internal filesystem locators."""
    if depth > 8:
        return "[nested data omitted]"
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key in sorted(value, key=str)[:80]:
            normalized = str(key).lower()
            if normalized in _OMITTED_KEYS or any(part in normalized for part in _SECRET_PARTS):
                continue
            output[str(key)] = _sanitize(value[key], depth=depth + 1)
        return output
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return value[:2_000]
    if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
        return None
    return value


def _source_href(entity_type: str, entity_id: str) -> str:
    return f"/api/evidence/sources/{quote(entity_type, safe='')}/{quote(entity_id, safe='')}"


def _document_link(entity_type: str, entity_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
    record_id = entity_id if entity_type == "MedicalRecord" else str(data.get("record_id") or "")
    if not record_id:
        return None
    link: dict[str, Any] = {
        "kind": "source_document",
        "record_id": record_id,
        "href": f"/api/records/file/{quote(record_id, safe='')}?inline=true",
    }
    if data.get("source_page") is not None:
        link["page"] = data["source_page"]
    return link


def _confidence(
    entity_type: str | dict[str, Any],
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Preserve the pre-G8 internal helper form used by analytics regressions.
    if data is None:
        data = entity_type if isinstance(entity_type, dict) else {}
        entity_type = ""
    if entity_type == "LabResult":
        qualification = lab_qualification(data)
        verification = qualification["verification_status"]
        clinically_verified = (
            verification in {"approved", "edited"}
            and qualification["validation_status"] != "invalid"
        )
        score = data.get("parser_confidence", data.get("confidence_score"))
        try:
            numeric = float(score)
        except (TypeError, ValueError):
            numeric = None
        if numeric is not None and not 0 <= numeric <= 1:
            numeric = None
        limitations = list(qualification["limitations"])
        if not clinically_verified:
            limitations.insert(
                0,
                "Machine extraction has not been approved or edited against the source document.",
            )
        return {
            "label": "high" if clinically_verified else "unverified",
            "score": numeric,
            "method": "human_verification_status" if clinically_verified else "machine_extraction",
            "verification_status": verification,
            "validation_status": qualification["validation_status"],
            "clinically_verified": clinically_verified,
            "limitations": limitations,
        }
    if entity_type == "FingerstickReading" and data.get("reconciliation_version"):
        return {
            "label": "not_assessed",
            "score": None,
            "method": data["reconciliation_version"],
            "limitations": [
                "The meter and CGM values are separate observations.",
                "Temporal pairing does not establish either source as clinical truth.",
            ],
        }
    analytics = data.get("analytics_confidence")
    if isinstance(analytics, dict):
        score = analytics.get("confidence_score")
        try:
            numeric = float(score)
        except (TypeError, ValueError):
            numeric = None
        if numeric is not None and 0 <= numeric <= 1:
            return {
                "label": analytics.get("confidence_label") or (
                    "high" if numeric >= 0.85 else "medium" if numeric >= 0.60 else "low"
                ),
                "score": numeric,
                "method": analytics.get("version") or "analytics-confidence",
                "discovery_status": analytics.get("discovery_status"),
            }
    verification = str(data.get("verification_status") or "").lower()
    if verification in {"approved", "edited"}:
        return {"label": "high", "score": None, "method": "human_verification_status"}
    score = data.get("parser_confidence", data.get("confidence_score"))
    try:
        numeric = float(score)
    except (TypeError, ValueError):
        numeric = None
    if numeric is not None and 0 <= numeric <= 1:
        label = "high" if numeric >= 0.85 else "medium" if numeric >= 0.60 else "low"
        return {"label": label, "score": numeric, "method": "source_reported_confidence"}
    label = str(data.get("confidence") or "").lower()
    if label in {"low", "medium", "high"}:
        return {"label": label, "score": None, "method": "source_reported_confidence"}
    return {"label": "not_assessed", "score": None, "method": None}


def _explicit_stance(data: dict[str, Any]) -> tuple[str | None, str | None]:
    flag = str(data.get("normalized_flag", data.get("flag", ""))).strip().lower().replace(" ", "_")
    if flag in {"normal", "within_range", "in_range"}:
        return "reassuring", f"Source explicitly labels this result {flag.replace('_', ' ')}."
    if flag in {"abnormal", "high", "low", "critical", "out_of_range"}:
        return "opposing", f"Source explicitly labels this result {flag.replace('_', ' ')}."
    return None, None


def _selected_types(
    domains: tuple[EvidenceDomain, ...],
    normalized_entity_types: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    available = {entity_type for domain in domains for entity_type in _DOMAIN_TYPES[domain]}
    if normalized_entity_types is not None:
        available &= set(normalized_entity_types)
    return tuple(sorted(available))


def _item_domain(
    entity_type: str,
    requested: tuple[EvidenceDomain, ...],
) -> EvidenceDomain:
    native = _ENTITY_DOMAIN.get(entity_type, EvidenceDomain.ANALYTICS)
    if native in requested:
        return native
    return next(
        (domain for domain in requested if entity_type in _DOMAIN_TYPES[domain]),
        requested[0],
    )


def _entity_where(entity_type: str, query: EvidenceBundleQuery) -> tuple[str, list[Any]]:
    where = ["type=?", "json_extract(data, '$.owner_email')=?"]
    parameters: list[Any] = [entity_type, OWNER_EMAIL]
    if entity_type == "Diagnosis":
        # Legacy suspected entries are tentative and must never be presented as
        # confirmed clinical facts. G10 exposes them through the hypothesis ledger.
        where.append("COALESCE(json_extract(data, '$.status'), 'active')!='suspected'")
    time_contract = _TIME_FIELDS.get(entity_type)
    if time_contract:
        field, precision, include_undated = time_contract
        expression = f"json_extract(data, '$.{field}')"
        start = query.start.date().isoformat() if precision == "date" else _instant(query.start)
        end = query.end.date().isoformat() if precision == "date" else _instant(query.end)
        range_clause = f"({expression}>=? AND {expression}<=?)"
        if include_undated:
            range_clause = f"({range_clause} OR {expression} IS NULL OR {expression}='')"
        where.append(range_clause)
        parameters.extend((start, end))
    return " AND ".join(where), parameters


def _observed_at(entity_type: str, row: sqlite3.Row, data: dict[str, Any]) -> str | None:
    contract = _TIME_FIELDS.get(entity_type)
    value = data.get(contract[0]) if contract else None
    return str(value) if value not in (None, "") else None


def _category(entity_type: str) -> str:
    if entity_type in _DERIVED_TYPES:
        return "derived_metric"
    if entity_type in _DOCUMENT_TYPES:
        return "document"
    return "direct_observation"


def _relevance(intent_tokens: set[str], entity_type: str, data: dict[str, Any]) -> int:
    haystack = " ".join(
        str(value) for key, value in data.items()
        if key in {"test_name", "title", "type", "category", "source", "description", "summary"}
    )
    entity_words = re.sub(r"(?<!^)(?=[A-Z])", " ", entity_type)
    tokens = set(_TOKEN.findall(f"{entity_words} {haystack}".lower()))
    return len(intent_tokens & tokens)


def _candidate_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    return (
        -int(candidate["_relevance"]),
        -_CATEGORY_PRIORITY[candidate["category"]],
        candidate.get("observed_at") is None,
        _reverse_text(candidate.get("observed_at") or ""),
        candidate.get("entity_type", ""),
        candidate["id"],
    )


def _reverse_text(value: str) -> tuple[int, ...]:
    """Make canonical timestamps sort descending while keeping a tuple key."""
    return tuple(-ord(character) for character in value)


def _load_entity_candidates(
    connection: sqlite3.Connection,
    query: EvidenceBundleQuery,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    intent_tokens = set(_TOKEN.findall(query.question_intent.lower()))
    candidates: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    total = 0
    for entity_type in _selected_types(query.domains, query.normalized_entity_types):
        where, parameters = _entity_where(entity_type, query)
        count = int(connection.execute(f"SELECT COUNT(*) FROM entities WHERE {where}", parameters).fetchone()[0])
        counts[entity_type] = count
        total += count
        if total > MAX_SOURCE_ROWS:
            raise EvidenceBundleError(
                f"query matches more than {MAX_SOURCE_ROWS} source rows; narrow the time range or domains"
            )
        rows = connection.execute(
            f"SELECT id, type, data, created_date, updated_date FROM entities WHERE {where} ORDER BY id",
            parameters,
        ).fetchall()
        for row in rows:
            try:
                raw = json.loads(row["data"])
            except (TypeError, ValueError):
                continue
            public = _sanitize(raw)
            data = public if isinstance(public, dict) else {}
            category = _category(entity_type)
            source_links = [{
                "kind": "normalized_entity",
                "entity_type": entity_type,
                "entity_id": row["id"],
                "href": _source_href(entity_type, row["id"]),
            }]
            document = _document_link(entity_type, row["id"], data)
            if document:
                source_links.append(document)
            candidates.append({
                "id": f"entity:{entity_type}:{row['id']}",
                "category": category,
                "domain": _item_domain(entity_type, query.domains).value,
                "entity_type": entity_type,
                "entity_id": row["id"],
                "observed_at": _observed_at(entity_type, row, data),
                "recorded_at": row["updated_date"],
                "data": data,
                "confidence": _confidence(entity_type, data),
                "source_links": source_links,
                "_relevance": _relevance(intent_tokens, entity_type, data),
            })
    return candidates, counts


def _response_source_rows(
    connection: sqlite3.Connection,
    entity_type: str,
    field: str,
    lower: str,
    upper: str,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"""
        SELECT id, data
        FROM entities
        WHERE type=? AND json_extract(data, '$.owner_email')=?
          AND json_extract(data, '$.{field}')>=?
          AND json_extract(data, '$.{field}')<=?
        ORDER BY id
        LIMIT ?
        """,
        (entity_type, OWNER_EMAIL, lower, upper, MAX_SOURCE_ROWS + 1),
    ).fetchall()
    if len(rows) > MAX_SOURCE_ROWS:
        raise EvidenceBundleError(
            "insulin response source rows exceed the bounded safety limit"
        )
    output = []
    for row in rows:
        try:
            data = json.loads(row["data"])
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            output.append({"id": row["id"], **data})
    return output


def _response_source_links(event: dict[str, Any]) -> list[dict[str, Any]]:
    identities: set[tuple[str, str]] = set()

    def add(entity_type: str, source: dict[str, Any] | None) -> None:
        entity_id = str((source or {}).get("id") or "")
        if entity_id:
            identities.add((entity_type, entity_id))

    observed = event["observed"]
    add("Treatment", observed.get("bolus"))
    add("GlucoseReading", observed.get("start_glucose"))
    add("GlucoseReading", observed.get("end_glucose"))
    add("GlucoseReading", observed.get("nadir_glucose"))
    add("FingerstickReading", observed.get("context_source"))
    for source in observed.get("carbohydrates", []):
        add("Treatment", source)
    for source in observed.get("subsequent_boluses", []):
        add("Treatment", source)
    for source in event["calculations"].get("iob_contributors", []):
        entity_id = str(source.get("entity_id") or "")
        if entity_id:
            identities.add(("Treatment", entity_id))
    return [
        {
            "kind": "normalized_entity",
            "entity_type": entity_type,
            "entity_id": entity_id,
            "href": _source_href(entity_type, entity_id),
        }
        for entity_type, entity_id in sorted(identities)
    ]


def _load_insulin_response_candidates(
    connection: sqlite3.Connection,
    query: EvidenceBundleQuery,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if EvidenceDomain.INSULIN not in query.domains:
        return [], {}
    source_start = query.start - timedelta(minutes=240)
    source_end = query.end + timedelta(minutes=130)
    lower = _instant(source_start)
    upper = _instant(source_end)
    start_date = (query.start.date() - timedelta(days=1)).isoformat()
    end_date = (query.end.date() + timedelta(days=1)).isoformat()
    treatments = _response_source_rows(
        connection, "Treatment", "timestamp", lower, upper
    )
    glucose = _response_source_rows(
        connection, "GlucoseReading", "timestamp", lower, upper
    )
    periods = _response_source_rows(
        connection, "PeriodLog", "date", start_date, end_date
    )
    oura_days = _response_source_rows(
        connection, "OuraDaily", "date", start_date, end_date
    )
    fitbit_days = _response_source_rows(
        connection, "FitbitDaily", "date", start_date, end_date
    )
    fingersticks = _response_source_rows(
        connection, "FingerstickReading", "timestamp", lower, upper
    )
    if sum(
        len(rows)
        for rows in (
            treatments,
            glucose,
            periods,
            oura_days,
            fitbit_days,
            fingersticks,
        )
    ) > MAX_SOURCE_ROWS:
        raise EvidenceBundleError(
            f"insulin response query matches more than {MAX_SOURCE_ROWS} source rows; "
            "narrow the time range"
        )
    response = build_response_events(
        treatments,
        glucose,
        period_logs=periods,
        wearable_days=[*oura_days, *fitbit_days],
        fingersticks=fingersticks,
        timezone_name=db.config_value("app_timezone", APP_TIMEZONE),
        event_start=query.start,
        event_end=query.end,
    )
    intent_tokens = set(_TOKEN.findall(query.question_intent.lower()))
    candidates = []
    for event in response["events"]:
        observed_at = str(event["observed"]["bolus"].get("timestamp") or "")
        try:
            event_time = datetime.fromisoformat(
                observed_at.replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        if not query.start <= event_time <= query.end:
            continue
        data = _sanitize(event)
        candidates.append(
            {
                "id": f"derived:InsulinResponseEvent:{event['id']}",
                "category": "derived_metric",
                "domain": EvidenceDomain.INSULIN.value,
                "entity_type": "InsulinResponseEvent",
                "entity_id": event["id"],
                "observed_at": observed_at,
                "recorded_at": None,
                "data": data,
                "confidence": {
                    "label": "not_assessed",
                    "score": None,
                    "method": event["algorithm_version"],
                    "limitations": [
                        "This is an observational response window; causation, resistance, and absorption are not inferred.",
                        event["assumptions"]["iob"]["limitations"],
                    ],
                },
                "source_links": _response_source_links(event),
                "_relevance": _relevance(
                    intent_tokens, "InsulinResponseEvent", data
                ),
            }
        )
    return candidates, {"InsulinResponseEvent": len(candidates)}


def _activity_position_source_links(effect: dict[str, Any]) -> list[dict[str, Any]]:
    links = []
    seen = set()
    for source in effect.get("source_refs") or []:
        entity_type = str(source.get("entity_type") or "")
        entity_id = str(source.get("entity_id") or "")
        if not entity_type or not entity_id:
            continue
        identity = (entity_type, entity_id)
        if identity in seen:
            continue
        seen.add(identity)
        if entity_type == "ActivityPositionInterval":
            href = f"/api/activity-position/intervals/{quote(entity_id, safe='')}"
            kind = "canonical_interval"
        elif entity_type in _ENTITY_DOMAIN:
            href = _source_href(entity_type, entity_id)
            kind = "normalized_entity"
        else:
            continue
        links.append(
            {
                "kind": kind,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "href": href,
            }
        )
    return links


def _load_activity_position_candidates(
    connection: sqlite3.Connection,
    query: EvidenceBundleQuery,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not {
        EvidenceDomain.WEARABLES,
        EvidenceDomain.ANALYTICS,
    }.intersection(query.domains):
        return [], {}
    if not connection.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='activity_position_intervals'"
    ).fetchone():
        return [], {}
    analysis = activity_position_analysis(query.start, query.end, connection=connection)
    intent_tokens = set(_TOKEN.findall(query.question_intent.lower()))
    candidates = []
    candidate_domain = (
        EvidenceDomain.WEARABLES
        if EvidenceDomain.WEARABLES in query.domains
        else EvidenceDomain.ANALYTICS
    )
    for effect in analysis["effects"]:
        confidence = effect["analytics_confidence"]
        data = _sanitize(effect)
        candidates.append(
            {
                "id": f"derived:ActivityPositionEffect:{effect['id']}",
                "category": "derived_metric",
                "domain": candidate_domain.value,
                "entity_type": "ActivityPositionEffect",
                "entity_id": effect["id"],
                "observed_at": query.end.isoformat().replace("+00:00", "Z"),
                "recorded_at": None,
                "data": data,
                "confidence": {
                    "label": confidence["confidence_label"],
                    "score": confidence["confidence_score"],
                    "method": effect["algorithm_version"],
                    "discovery_status": confidence["discovery_status"],
                    "replication_status": effect["replication_status"],
                    "limitations": [
                        "Activity/position comparisons are temporal associations and do not establish causation.",
                        "Only timestamped intervals contribute event-time state; daily wearable totals are not position evidence.",
                    ],
                },
                "source_links": _activity_position_source_links(effect),
                "_relevance": _relevance(
                    intent_tokens, "ActivityPositionEffect", data
                ),
            }
        )
    return candidates, {
        "ActivityPositionInterval": analysis["counts"]["intervals"],
        "ActivityPositionEffect": len(candidates),
    }


def _load_management_burden_candidates(
    connection: sqlite3.Connection,
    query: EvidenceBundleQuery,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if EvidenceDomain.ANALYTICS not in query.domains:
        return [], {}
    if not connection.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='management_burden_events'"
    ).fetchone():
        return [], {}
    analysis = management_burden_analysis(
        query.start,
        query.end,
        connection=connection,
    )
    source_links = []
    seen = set()
    for event in analysis["events"]:
        entity_type = str(event.get("source_entity_type") or "")
        entity_id = str(event.get("source_entity_id") or "")
        identity = (entity_type, entity_id)
        if not entity_type or not entity_id or identity in seen:
            continue
        seen.add(identity)
        if entity_type in _ENTITY_DOMAIN:
            source_links.append(
                {
                    "kind": "normalized_entity",
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "href": _source_href(entity_type, entity_id),
                }
            )
    identity = _checksum(
        {
            "algorithm_version": analysis["algorithm_version"],
            "window": analysis["window"],
            "summary": analysis["summary"],
            "components": analysis["components"],
            "outcomes": analysis["outcomes"],
            "source_coverage": analysis["source_coverage"],
            "event_ids": sorted(event["original_event_id"] for event in analysis["events"]),
        }
    ).removeprefix("sha256:")[:32]
    data = _sanitize(
        {
            key: analysis[key]
            for key in (
                "algorithm_version",
                "semantic_class",
                "window",
                "summary",
                "components",
                "source_coverage",
                "outcomes",
                "outcome_vs_effort",
                "analytics_confidence",
                "language",
            )
        }
    )
    confidence = analysis["analytics_confidence"]
    candidate = {
        "id": f"derived:ManagementBurdenSummary:{identity}",
        "category": "derived_metric",
        "domain": EvidenceDomain.ANALYTICS.value,
        "entity_type": "ManagementBurdenSummary",
        "entity_id": identity,
        "observed_at": query.end.isoformat().replace("+00:00", "Z"),
        "recorded_at": None,
        "data": data,
        "confidence": {
            "label": confidence["confidence_label"],
            "score": confidence["confidence_score"],
            "method": analysis["algorithm_version"],
            "discovery_status": confidence["discovery_status"],
            "replication_status": confidence["replication"]["status"],
            "limitations": [
                analysis["language"]["observed_only"],
                analysis["language"]["missing_sources"],
                analysis["language"]["clinical"],
            ],
        },
        "source_links": source_links,
        "_relevance": _relevance(
            set(_TOKEN.findall(query.question_intent.lower())),
            "ManagementBurdenSummary",
            data,
        ),
    }
    return [candidate], {
        "ManagementBurdenEvent": analysis["event_count"],
        "ManagementBurdenSummary": 1,
    }


def _interval_bounds(start: str, end: str | None) -> tuple[datetime, datetime | None]:
    if len(start) == 10:
        lower = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    else:
        lower = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(timezone.utc)
    if end is None:
        return lower, None
    if len(end) == 10:
        upper = datetime.fromisoformat(end).replace(
            hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc
        )
    else:
        upper = datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone(timezone.utc)
    return lower, upper


def _load_episode_candidates(
    connection: sqlite3.Connection,
    query: EvidenceBundleQuery,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if EvidenceDomain.CLINICAL not in query.domains:
        return [], {}
    intent_tokens = set(_TOKEN.findall(query.question_intent.lower()))
    candidates = []
    counts = {"HealthEpisode": 0, "MedicationExposure": 0}
    source_total = 0
    definitions = (
        (
            "HealthEpisode",
            "health_episodes",
            "/api/episodes",
            """
            SELECT id,episode_type,title,description,origin_kind,origin_label,status,
                   start_time,end_time,time_precision,confidence_json,association_only,
                   membership_revision,input_hash,created_at,updated_at
            FROM health_episodes
            WHERE owner_id=? AND status!='dismissed'
            ORDER BY start_time,id
            """,
        ),
        (
            "MedicationExposure",
            "medication_exposures",
            "/api/medication-exposures",
            """
            SELECT id,medication_entity_id,medication_name,dose,formulation,frequency,
                   start_time,end_time,time_precision,origin_kind,origin_label,status,
                   confidence_json,created_at,updated_at
            FROM medication_exposures
            WHERE owner_id=? AND status!='dismissed'
            ORDER BY start_time,id
            """,
        ),
    )
    for entity_type, table, href_base, statement in definitions:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            continue
        rows = connection.execute(statement, (DEPLOYMENT_OWNER_ID,)).fetchall()
        source_total += len(rows)
        if source_total > MAX_SOURCE_ROWS:
            raise EvidenceBundleError(
                "canonical episode rows exceed the bounded safety limit"
            )
        for row in rows:
            observed_start, observed_end = _interval_bounds(row["start_time"], row["end_time"])
            if observed_start > query.end or (
                observed_end is not None and observed_end < query.start
            ):
                continue
            counts[entity_type] += 1
            data = {
                key: row[key]
                for key in row.keys()
                if key not in {"confidence_json", "created_at", "updated_at"}
            }
            data["semantic_class"] = (
                "patient_report"
                if row["origin_kind"] == "manual"
                else "derived_temporal_episode"
            )
            data["temporal_association_only"] = True
            confidence = json.loads(row["confidence_json"])
            candidates.append({
                "id": f"canonical:{entity_type}:{row['id']}",
                "category": (
                    "direct_observation"
                    if row["origin_kind"] == "manual"
                    else "derived_metric"
                ),
                "domain": EvidenceDomain.CLINICAL.value,
                "entity_type": entity_type,
                "entity_id": row["id"],
                "observed_at": row["start_time"],
                "recorded_at": row["updated_at"],
                "data": _sanitize(data),
                "confidence": {
                    "label": confidence.get("confidence_label", "not_assessed"),
                    "score": confidence.get("confidence_score"),
                    "method": confidence.get("method"),
                    "limitations": [
                        "Temporal membership and co-occurrence do not establish causation."
                    ],
                },
                "source_links": [{
                    "kind": "canonical_episode",
                    "entity_type": entity_type,
                    "entity_id": row["id"],
                    "href": f"{href_base}/{quote(row['id'], safe='')}",
                }],
                "_relevance": _relevance(intent_tokens, entity_type, data),
            })
    return candidates, counts


def _load_contradictions(
    connection: sqlite3.Connection,
    query: EvidenceBundleQuery,
) -> list[dict[str, Any]]:
    domains = tuple(sorted({value for domain in query.domains for value in _CONTRADICTION_DOMAINS[domain]}))
    if not domains:
        return []
    placeholders = ",".join("?" for _ in domains)
    unresolved_count = int(connection.execute(
        f"""
        SELECT COUNT(*) FROM contradictions
        WHERE owner_id=? AND resolution_state='unresolved'
          AND domain IN ({placeholders})
        """,
        (DEPLOYMENT_OWNER_ID, *domains),
    ).fetchone()[0])
    if unresolved_count > MAX_CONTRADICTIONS:
        raise EvidenceBundleError(
            "unresolved contradictions exceed the bounded safety limit; resolve or narrow the domains"
        )
    rows = SqliteContradictionRepository(connection).list(
        domains=domains,
        include_resolved=False,
        limit=MAX_CONTRADICTIONS,
    )
    output = []
    for row in rows:
        output.append({
            "id": row["id"],
            "domain": row["domain"],
            "severity": row["severity"],
            "explanation": row["explanation"],
            "subject": {"type": row["subject_type"], "key": row["subject_key"]},
            "left": _sanitize(row["left"]),
            "right": _sanitize(row["right"]),
            "detection_state": row["detection_state"],
            "resolution_state": row["resolution_state"],
            "last_detected_at": row["last_detected_at"],
        })
    return output


def _load_relationship_candidates(
    connection: sqlite3.Connection,
    anchors: list[dict[str, Any]],
    intent_tokens: set[str],
    requested_domains: tuple[EvidenceDomain, ...],
) -> list[dict[str, Any]]:
    if not relationship_reads_enabled():
        return []
    repository = SqliteRelationshipRepository(connection)
    edges: dict[str, Any] = {}
    for anchor in anchors:
        if len(edges) >= MAX_ITEM_BUDGET:
            break
        remaining = MAX_ITEM_BUDGET - len(edges)
        for edge in repository.for_entity(
            OWNER_EMAIL, anchor["entity_type"], anchor["entity_id"], limit=remaining + 1
        ):
            edges[edge.id or repr(edge)] = edge
        remaining = MAX_ITEM_BUDGET - len(edges)
        if remaining <= 0:
            break
        for edge in repository.reverse_for_entity(
            OWNER_EMAIL, anchor["entity_type"], anchor["entity_id"], limit=remaining + 1
        ):
            edges[edge.id or repr(edge)] = edge
    candidates = []
    for edge in sorted(edges.values(), key=lambda item: (
        item.predicate, item.subject_type, item.subject_id, item.object_type, item.object_id, item.id or ""
    ))[:MAX_ITEM_BUDGET]:
        public = _public_edge(edge)
        hrefs = []
        for entity_type, entity_id in (
            (edge.subject_type, edge.subject_id),
            (edge.object_type, edge.object_id),
        ):
            hrefs.append({
                "kind": "normalized_entity",
                "entity_type": entity_type,
                "entity_id": entity_id,
                "href": _source_href(entity_type, entity_id),
            })
        relation_types = " ".join(
            re.sub(r"(?<!^)(?=[A-Z])", " ", value)
            for value in (edge.subject_type, edge.object_type)
        )
        relation_tokens = set(_TOKEN.findall(f"{edge.predicate} {relation_types}".lower()))
        candidates.append({
            "id": f"relationship:{edge.id}",
            "category": "relationship",
            "domain": _item_domain(edge.subject_type, requested_domains).value,
            "entity_type": "Relationship",
            "entity_id": edge.id,
            "observed_at": edge.valid_from,
            "recorded_at": edge.generated_at,
            "data": public,
            "confidence": public["confidence"],
            "source_links": hrefs,
            "_relevance": len(intent_tokens & relation_tokens),
        })
    return candidates


def _archive_refs(
    connection: sqlite3.Connection,
    selected: list[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    output: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in selected:
        if item["category"] == "relationship":
            continue
        key = (item["entity_type"], item["entity_id"])
        rows = connection.execute(
            """
            SELECT source_record_id, source_file_id, sync_run_id, parser_version, linked_at
            FROM normalized_source_links
            WHERE owner_id=? AND entity_type=? AND entity_id=?
            ORDER BY linked_at, id
            """,
            (DEPLOYMENT_OWNER_ID, *key),
        ).fetchall()
        output[key] = [{
            "kind": "immutable_source_reference",
            "source_record_ref": _opaque(row["source_record_id"]),
            "source_file_ref": _opaque(row["source_file_id"]),
            "sync_run_ref": _opaque(row["sync_run_id"]),
            "parser_version": row["parser_version"],
            "linked_at": row["linked_at"],
        } for row in rows]
    return output


def _attach_claim_evidence(
    connection: sqlite3.Connection,
    selected: list[dict[str, Any]],
) -> None:
    catalog = LegacyRepositoryCatalog(connection)
    claim_repository = SqliteClaimVersionRepository(connection)
    for item in selected:
        if item["entity_type"] not in _DERIVED_TYPES:
            continue
        references = catalog.evidence.for_claim(OWNER_EMAIL, item["entity_type"], item["entity_id"])
        item["claim_evidence"] = [{
            "kind": reference.evidence_kind,
            "locator_ref": _opaque(reference.locator),
            "value": _sanitize(reference.value),
        } for reference in references]
        if item["entity_type"] not in {"Pattern", "Insight"}:
            continue
        claim = claim_repository.for_entity(
            OWNER_EMAIL, item["entity_type"], item["entity_id"]
        )
        if not claim or not claim.get("evidence_set_id"):
            continue
        href = (
            f"/api/evidence/claims/{quote(item['entity_type'], safe='')}/"
            f"{quote(item['entity_id'], safe='')}"
        )
        item["claim"] = {
            "claim_version_id": claim["id"],
            "version_number": claim["version_number"],
            "assertion_kind": claim["assertion_kind"],
            "assertion_status": claim["assertion_status"],
            "algorithm": {
                "id": claim["algorithm_id"],
                "version": claim["algorithm_version"],
            },
            "evidence_set_id": claim["evidence_set_id"],
            "href": href,
        }
        item["source_links"].append({
            "kind": "claim_evidence",
            "entity_type": item["entity_type"],
            "entity_id": item["entity_id"],
            "href": href,
        })


def _caveats(
    query: EvidenceBundleQuery,
    counts: dict[str, int],
    selected: list[dict[str, Any]],
) -> list[dict[str, str]]:
    caveats = [{
        "code": "absence_is_not_proof",
        "domain": "all",
        "message": (
            "The bundle reflects records available to this deployment in the requested range; "
            "missing records do not prove that an event did not occur."
        ),
    }]
    for domain in query.domains:
        entity_types = _DOMAIN_TYPES[domain]
        available = sum(counts.get(entity_type, 0) for entity_type in entity_types)
        if domain == EvidenceDomain.CLINICAL:
            available += sum(counts.get(entity_type, 0) for entity_type in _CANONICAL_EPISODE_TYPES)
        if available == 0:
            message = "No source records were available for this domain and time range."
            code = "domain_missing"
        else:
            returned = sum(1 for item in selected if item["domain"] == domain.value)
            message = (
                f"{available} matching source records were available; {returned} ranked items were "
                "returned within the shared item budget. Coverage and freshness are not inferred from count alone."
            )
            code = "domain_partial_context"
        caveats.append({"code": code, "domain": domain.value, "message": message})
    if not relationship_reads_enabled():
        caveats.append({
            "code": "relationship_reads_disabled",
            "domain": "all",
            "message": "Governed relationship reads are disabled, so this bundle contains no graph projection items.",
        })
    return caveats


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if not key.startswith("_")}


class _BundleCache:
    def __init__(self) -> None:
        self._items: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._items.get(key)
            if value is None:
                return None
            self._items.move_to_end(key)
            return copy.deepcopy(value)

    def put(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._items[key] = copy.deepcopy(value)
            self._items.move_to_end(key)
            while len(self._items) > MAX_CACHE_ENTRIES:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class _DatabaseRevision:
    """Observe commits cheaply while exact public content remains the response version."""

    def __init__(self) -> None:
        self._connection: sqlite3.Connection | None = None
        self._identity: tuple[str, int, int] | None = None
        self._lock = threading.RLock()

    def current(self) -> tuple[str, int, int, int]:
        path = db.DB_PATH.resolve()
        stat = path.stat()
        identity = (str(path), stat.st_dev, stat.st_ino)
        with self._lock:
            if self._connection is None or self._identity != identity:
                if self._connection is not None:
                    self._connection.close()
                self._connection = sqlite3.connect(path, check_same_thread=False)
                self._identity = identity
            version = int(self._connection.execute("PRAGMA data_version").fetchone()[0])
        return (*identity, version)

    def reset(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
            self._connection = None
            self._identity = None


_BUNDLE_CACHE = _BundleCache()
_DATABASE_REVISION = _DatabaseRevision()


def clear_bundle_cache() -> None:
    _BUNDLE_CACHE.clear()
    _DATABASE_REVISION.reset()


def build_bundle(query: EvidenceBundleQuery) -> dict[str, Any]:
    canonical_query = {
        "start": _instant(query.start),
        "end": _instant(query.end),
        "domains": [domain.value for domain in query.domains],
        "question_intent": query.question_intent,
        "item_budget": query.item_budget,
    }
    if query.normalized_entity_types is not None:
        canonical_query["normalized_entity_types"] = list(query.normalized_entity_types)
    query_checksum = _checksum(canonical_query)
    intent_tokens = set(_TOKEN.findall(query.question_intent.lower()))
    cache_key = _checksum({
        "bundle_version": BUNDLE_VERSION,
        "query_checksum": query_checksum,
        "database_revision": _DATABASE_REVISION.current(),
        "relationship_reads_enabled": relationship_reads_enabled(),
        "evidence_set_reads_enabled": evidence_set_reads_enabled(),
    })
    if cached := _BUNDLE_CACHE.get(cache_key):
        return cached

    with db.connect() as connection:
        connection.execute("BEGIN")
        entity_candidates, counts = _load_entity_candidates(connection, query)
        response_candidates, response_counts = _load_insulin_response_candidates(
            connection, query
        )
        entity_candidates.extend(response_candidates)
        counts.update(response_counts)
        activity_candidates, activity_counts = _load_activity_position_candidates(
            connection, query
        )
        entity_candidates.extend(activity_candidates)
        counts.update(activity_counts)
        burden_candidates, burden_counts = _load_management_burden_candidates(
            connection, query
        )
        entity_candidates.extend(burden_candidates)
        counts.update(burden_counts)
        episode_candidates, episode_counts = _load_episode_candidates(connection, query)
        counts.update(episode_counts)
        anchors = sorted(entity_candidates, key=_candidate_key)[:query.item_budget]
        relationship_candidates = _load_relationship_candidates(
            connection,
            anchors,
            intent_tokens,
            query.domains,
        )
        selected = sorted(
            [*entity_candidates, *episode_candidates, *relationship_candidates],
            key=_candidate_key,
        )[:query.item_budget]
        archive_refs = _archive_refs(connection, selected)
        for item in selected:
            item["provenance"] = archive_refs.get((item["entity_type"], item["entity_id"]), [])
        _attach_claim_evidence(connection, selected)
        contradictions = _load_contradictions(connection, query)
        schema_version = int(connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0])
        connection.rollback()

    caveats = _caveats(query, counts, selected)
    snapshot = {
        "schema_version": schema_version,
        "counts": counts,
        "items": [_public_item(item) for item in selected],
        "contradictions": contradictions,
        "caveats": caveats,
        "generator": {"id": BUNDLE_GENERATOR, "version": BUNDLE_VERSION},
    }
    data_version = _checksum(snapshot)
    bundle_id = "urn:glucopilot:evidence-bundle:" + _checksum(
        {"query_checksum": query_checksum, "data_version": data_version}
    ).removeprefix("sha256:")

    sections = {
        "direct_observations": [],
        "derived_metrics": [],
        "relationships": [],
        "documents": [],
        "reassuring_evidence": [],
        "opposing_evidence": [],
    }
    source_links: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in selected:
        item = _public_item(raw)
        section = {
            "direct_observation": "direct_observations",
            "derived_metric": "derived_metrics",
            "relationship": "relationships",
            "document": "documents",
        }[raw["category"]]
        sections[section].append(item)
        for link in raw["source_links"]:
            source_links[(link["kind"], link["href"])] = link
        stance, reason = _explicit_stance(raw["data"])
        if stance:
            sections[f"{stance}_evidence"].append({
                "evidence_item_id": raw["id"],
                "reason": reason,
            })
    for contradiction in contradictions:
        sections["opposing_evidence"].append({
            "contradiction_id": contradiction["id"],
            "reason": contradiction["explanation"],
            "left": contradiction["left"],
            "right": contradiction["right"],
        })

    protected_blocking = [item for item in contradictions if item["severity"] == "blocking"]
    response = {
        "bundle_id": bundle_id,
        "bundle_version": BUNDLE_VERSION,
        "query": canonical_query,
        "query_checksum": query_checksum,
        "data_version": {
            "contract_name": "glucopilot-clinical-data-contracts",
            "contract_version": "1.0.0",
            "schema_version": schema_version,
            "input_hash": data_version,
            "algorithm": {"algorithm_id": BUNDLE_GENERATOR, "version": BUNDLE_VERSION},
        },
        "evidence": sections,
        "contradictions": contradictions,
        "missing_data_caveats": caveats,
        "source_links": sorted(source_links.values(), key=lambda item: (item["kind"], item["href"])),
        "confidence": {
            "label": "not_assessed",
            "score": None,
            "method": None,
            "note": "The bundle preserves source confidence and does not infer an overall clinical confidence score.",
        },
        "budget": {
            "item_limit": query.item_budget,
            "returned_items": len(selected),
            "available_source_items": sum(counts.values()),
            "protected_blocking_contradictions": len(protected_blocking),
            "blocking_contradictions_count_against_item_limit": False,
            "truncated": (
                len(entity_candidates) + len(episode_candidates) + len(relationship_candidates)
                > len(selected)
            ),
        },
        "ordering": list(ORDERING),
    }
    _BUNDLE_CACHE.put(cache_key, response)
    return copy.deepcopy(response)


def source_detail(entity_type: str, entity_id: str) -> dict[str, Any]:
    if entity_type not in _ENTITY_DOMAIN:
        raise HTTPException(status_code=404, detail="Evidence source not found")
    with db.connect() as connection:
        row = connection.execute(
            """
            SELECT id, type, data, created_date, updated_date FROM entities
            WHERE type=? AND id=? AND json_extract(data, '$.owner_email')=?
            """,
            (entity_type, entity_id, OWNER_EMAIL),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Evidence source not found")
        try:
            raw = json.loads(row["data"])
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=404, detail="Evidence source not found") from error
        data = _sanitize(raw)
        refs = _archive_refs(connection, [{
            "category": _category(entity_type),
            "entity_type": entity_type,
            "entity_id": entity_id,
        }]).get((entity_type, entity_id), [])
    links = []
    if document := _document_link(entity_type, entity_id, data):
        links.append(document)
    return {
        "entity": {"type": entity_type, "id": entity_id},
        "data": data,
        "created_at": row["created_date"],
        "updated_at": row["updated_date"],
        "confidence": _confidence(entity_type, data),
        "provenance": refs,
        "source_links": links,
    }


def claim_detail(claim_type: str, claim_id: str) -> dict[str, Any]:
    if claim_type not in {"Pattern", "Insight"}:
        raise HTTPException(status_code=404, detail="Evidence-backed claim not found")
    claim_repository = SqliteClaimVersionRepository()
    claim = claim_repository.for_entity(OWNER_EMAIL, claim_type, claim_id)
    if not claim or not claim.get("evidence_set_id"):
        raise HTTPException(status_code=404, detail="Evidence-backed claim not found")
    entity = LegacyRepositoryCatalog().entity(claim_type).get(claim_id)
    if not entity or entity.get("owner_email") != OWNER_EMAIL:
        raise HTTPException(status_code=404, detail="Evidence-backed claim not found")
    with db.connect() as connection:
        evidence_set = connection.execute(
            """
            SELECT * FROM evidence_sets
            WHERE id=? AND owner_id=? AND owner_email=? AND claim_type=? AND claim_id=?
            """,
            (
                claim["evidence_set_id"],
                DEPLOYMENT_OWNER_ID,
                OWNER_EMAIL,
                claim_type,
                claim_id,
            ),
        ).fetchone()
        if not evidence_set:
            raise HTTPException(status_code=404, detail="Evidence-backed claim not found")
        rows = connection.execute(
            """
            SELECT observation_windows.*, evidence_set_windows.evidence_role,
                   evidence_set_windows.rationale, evidence_set_windows.ordinal
            FROM evidence_set_windows
            JOIN observation_windows
              ON observation_windows.id=evidence_set_windows.observation_window_id
            WHERE evidence_set_windows.evidence_set_id=?
              AND observation_windows.owner_id=? AND observation_windows.owner_email=?
            ORDER BY evidence_set_windows.ordinal
            """,
            (evidence_set["id"], DEPLOYMENT_OWNER_ID, OWNER_EMAIL),
        ).fetchall()
    evidence: dict[str, list[dict[str, Any]]] = {
        "supporting": [],
        "opposing": [],
        "limiting": [],
    }
    for row in rows:
        member_ids = json.loads(row["member_ids_json"])
        public = {
            "window_id": row["id"],
            "entity_type": row["entity_type"],
            "window_start": row["window_start"],
            "window_end": row["window_end"],
            "observation_count": row["observation_count"],
            "observation_checksum": row["observation_checksum"],
            "status": row["status"],
            "rationale": row["rationale"],
            "href": f"/api/evidence/windows/{quote(row['id'], safe='')}",
            "source_preview": [
                {
                    "entity_type": row["entity_type"],
                    "entity_id": member_id,
                    "href": _source_href(row["entity_type"], member_id),
                }
                for member_id in member_ids[:5]
            ],
            "source_preview_truncated": len(member_ids) > 5,
        }
        evidence[row["evidence_role"]].append(public)
    evidence["limiting"].extend(json.loads(evidence_set["limitations_json"]))
    history = claim_repository.history(OWNER_EMAIL, claim_type, claim["claim_key"])
    return {
        "claim_contract_version": CLAIM_CONTRACT_VERSION,
        "claim": {
            "claim_version_id": claim["id"],
            "claim_type": claim_type,
            "claim_id": claim_id,
            "claim_key": claim["claim_key"],
            "version_number": claim["version_number"],
            "assertion_kind": claim["assertion_kind"],
            "assertion_status": claim["assertion_status"],
            "algorithm": {
                "id": claim["algorithm_id"],
                "version": claim["algorithm_version"],
            },
            "input_data_version": claim["input_data_version"],
            "content_checksum": claim["content_checksum"],
            "analytics_confidence": claim["analytics_confidence"],
            "data": _sanitize(entity),
        },
        "evidence_set": {
            "id": evidence_set["id"],
            "checksum": evidence_set["set_checksum"],
            "status": evidence_set["status"],
            "summary": _sanitize(json.loads(evidence_set["summary_json"])),
        },
        "evidence": evidence,
        "lineage": [
            {
                "claim_version_id": version["id"],
                "claim_id": version["claim_entity_id"],
                "version_number": version["version_number"],
                "assertion_status": version["assertion_status"],
                "created_at": version["created_at"],
                "supersedes_claim_version_id": version["supersedes_claim_version_id"],
                "superseded_by_claim_version_id": version["superseded_by_claim_version_id"],
                "href": f"/api/evidence/claims/{quote(claim_type, safe='')}/"
                f"{quote(version['claim_entity_id'], safe='')}",
            }
            for version in history
        ],
    }


def window_detail(window_id: str, *, offset: int = 0, limit: int = 50) -> dict[str, Any]:
    repository = SqliteEvidenceSetRepository()
    window = repository.get_window(window_id)
    if not window or window.get("owner_email") != OWNER_EMAIL:
        raise HTTPException(status_code=404, detail="Evidence window not found")
    try:
        observations = repository.drill_down(window_id)
    except StaleEvidenceError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    page = observations[offset : offset + limit]
    return {
        "window": {
            key: window[key]
            for key in (
                "id",
                "entity_type",
                "query_definition",
                "window_start",
                "window_end",
                "observation_count",
                "observation_checksum",
                "summary",
                "status",
            )
        },
        "offset": offset,
        "limit": limit,
        "returned": len(page),
        "has_more": offset + len(page) < len(observations),
        "observations": [
            {
                "entity_type": window["entity_type"],
                "entity_id": observation["id"],
                "data": _sanitize(observation),
                "href": _source_href(window["entity_type"], observation["id"]),
            }
            for observation in page
        ],
    }


router = APIRouter(prefix="/api/evidence", dependencies=[Depends(require_login)])


@router.post("/bundles/query")
def query_bundle(body: EvidenceBundleQuery):
    try:
        return build_bundle(body)
    except EvidenceBundleError as error:
        raise HTTPException(status_code=413, detail=str(error)) from error


@router.get("/sources/{entity_type}/{entity_id}")
def get_source(entity_type: str, entity_id: str):
    return source_detail(entity_type, entity_id)


@router.get("/claims/{claim_type}/{claim_id}")
def get_claim(claim_type: str, claim_id: str):
    return claim_detail(claim_type, claim_id)


@router.get("/windows/{window_id}")
def get_window(window_id: str, offset: int = 0, limit: int = 50):
    if offset < 0 or not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail="offset must be non-negative and limit 1-100")
    return window_detail(window_id, offset=offset, limit=limit)
