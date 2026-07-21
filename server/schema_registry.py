"""Canonical registry for entity types stored in the legacy JSON entity table.

Registry membership is broader than generic API exposure. Dedicated feature
APIs use several entity types that must be known to migrations and auditing but
must not become generic CRUD endpoints as a side effect of registration.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EntitySchema:
    name: str
    domain: str
    owner_scope: str
    api_exposure: str
    lifecycle: str
    description: str
    schema_version: int = 1
    storage_kind: str = "json"


def _schema(
    name: str,
    domain: str,
    description: str,
    *,
    api: str = "dedicated",
    lifecycle: str = "mutable",
    owner_scope: str = "deployment_owner",
) -> EntitySchema:
    return EntitySchema(name, domain, owner_scope, api, lifecycle, description)


# Migration 2 owns this immutable snapshot. New entities must be appended to
# ENTITY_SCHEMAS and inserted by a new migration; never edit this tuple after
# the first migration release.
BASELINE_ENTITY_SCHEMAS = (
    _schema("GlucoseReading", "glucose", "Continuous glucose observation", api="generic", lifecycle="observation"),
    _schema("Treatment", "insulin", "Insulin, carbohydrate, basal, pump, or related treatment event", api="generic", lifecycle="event"),
    _schema("DailySummary", "legacy", "Legacy daily summary with no active writer", api="generic", lifecycle="legacy"),
    _schema("WeeklySummary", "legacy", "Legacy weekly summary with no active writer", api="generic", lifecycle="legacy"),
    _schema("Pattern", "analytics", "Detected glucose pattern and supporting data", api="generic", lifecycle="derived"),
    _schema("Insight", "analytics", "Cross-domain analytical insight", api="generic", lifecycle="derived"),
    _schema("AIConversation", "companion", "Legacy serialized AI conversation", api="generic", lifecycle="legacy"),
    _schema("PeriodLog", "cycle", "Manual, imported, or inferred cycle-day observation", api="generic", lifecycle="observation"),
    _schema("NightscoutProfile", "insulin", "Nightscout pump profile and schedules", api="generic"),
    _schema("OuraConnection", "connections", "Oura OAuth connection", api="generic", lifecycle="credential"),
    _schema("OuraDaily", "wearables", "Oura daily sleep, readiness, and activity observation", api="generic", lifecycle="observation"),
    _schema("OuraHeartRate", "wearables", "Oura heart-rate observation", api="generic", lifecycle="observation"),
    _schema("UserSettings", "settings", "Legacy entity-backed connection settings", api="generic"),
    _schema("DexcomConnection", "connections", "Dexcom OAuth connection", api="generic", lifecycle="credential"),
    _schema("MedicalRecord", "records", "Uploaded medical-document processing record", api="generic"),
    _schema("LabResult", "records", "Extracted laboratory or imaging measurement", api="generic", lifecycle="observation"),
    _schema("FitbitConnection", "connections", "Fitbit OAuth connection", api="generic", lifecycle="credential"),
    _schema("FitbitDaily", "wearables", "Fitbit or Google Health daily observation", api="generic", lifecycle="observation"),
    _schema("FitbitHeartRate", "wearables", "Fitbit or Google Health heart-rate observation", api="generic", lifecycle="observation"),
    _schema("FingerstickReading", "glucose", "Manual blood-glucose meter observation", lifecycle="observation"),
    _schema("GoogleHealthConnection", "connections", "Google Health OAuth connection", lifecycle="credential"),
    _schema("HealthProfile", "profile", "Current owner health and body profile"),
    _schema("WeightLog", "profile", "Timestamped body-weight observation", lifecycle="observation"),
    _schema("Diagnosis", "clinical", "Owner-reported diagnosis or condition", lifecycle="event"),
    _schema("Medication", "clinical", "Owner-reported medication or supplement"),
    _schema("Allergy", "clinical", "Owner-reported allergy"),
    _schema("InsuranceInfo", "clinical", "Current insurance profile"),
    _schema("SymptomLog", "clinical", "Owner-reported symptom journal event", lifecycle="event"),
    _schema("HistoryEntry", "clinical", "Owner-reported health-history event", lifecycle="event"),
    _schema("HealthSummary", "analytics", "Generated health overview payload", lifecycle="derived"),
    _schema("HealthMemory", "companion", "Companion memory or owner note", lifecycle="assertion"),
    _schema("CompanionThread", "companion", "Companion conversation thread"),
    _schema("ChatMessage", "companion", "Companion message", lifecycle="event"),
    _schema("BugReport", "operations", "Locally retained in-app bug report", lifecycle="event"),
)

ENTITY_SCHEMAS = BASELINE_ENTITY_SCHEMAS

ENTITY_SCHEMA_BY_NAME = {schema.name: schema for schema in ENTITY_SCHEMAS}
if len(ENTITY_SCHEMA_BY_NAME) != len(ENTITY_SCHEMAS):
    raise RuntimeError("duplicate entity type in schema registry")

GENERIC_API_TYPES = frozenset(
    schema.name for schema in ENTITY_SCHEMAS if schema.api_exposure == "generic"
)
