"""Versioned identity, time, provenance, assertion, and data-version contracts.

These models are additive design contracts for the typed data layer. Legacy JSON
entities are not validated or rewritten by this module. A future migration may
adopt the contracts only after the corresponding schema is separately reviewed.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator


DATA_CONTRACT_NAME = "glucopilot-clinical-data-contracts"
DATA_CONTRACT_VERSION = "1.0.0"
DEPLOYMENT_OWNER_ID = "urn:glucopilot:owner:self"

# Fixed forever: changing this namespace would change every derived canonical ID.
_CANONICAL_NAMESPACE = uuid.UUID("6988cb42-b1a7-5bd1-b91f-fcf5a2bd8da6")
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_OFFSET = re.compile(r"^[+-](?:0\d|1[0-4]):[0-5]\d$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH = re.compile(r"^\d{4}-\d{2}$")
_YEAR = re.compile(r"^\d{4}$")


def _slug(value: str) -> str:
    slug = re.sub(r"(?<!^)(?=[A-Z])", "-", value).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    if not slug:
        raise ValueError("canonical ID type cannot be empty")
    return slug


def canonical_entity_id(entity_type: str, stable_local_id: str) -> str:
    """Derive a stable, type-scoped ID without exposing the legacy local ID."""
    if not stable_local_id or not stable_local_id.strip():
        raise ValueError("stable local ID cannot be empty")
    entity_slug = _slug(entity_type)
    derived = uuid.uuid5(_CANONICAL_NAMESPACE, f"entity:{entity_slug}:{stable_local_id}")
    return f"urn:glucopilot:{entity_slug}:{derived}"


def canonical_source_record_id(source_system: str, source_record_id: str) -> str:
    """Derive a stable ID in a source-system namespace."""
    if not source_record_id or not source_record_id.strip():
        raise ValueError("source record ID cannot be empty")
    source_slug = _slug(source_system)
    derived = uuid.uuid5(
        _CANONICAL_NAMESPACE,
        f"source-record:{source_slug}:{source_record_id}",
    )
    return f"urn:glucopilot:source-record:{derived}"


class FrozenContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IdentityBasis(StrEnum):
    APPLICATION_GENERATED = "application_generated"
    SOURCE_DETERMINISTIC = "source_deterministic"
    OWNER_SINGLETON = "owner_singleton"
    NATURAL_KEY = "natural_key"
    PARENT_SCOPED = "parent_scoped"
    LEGACY_MAPPED = "legacy_mapped"
    DERIVED_OUTPUT = "derived_output"


class CanonicalIdentity(FrozenContractModel):
    canonical_id: str = Field(pattern=r"^urn:glucopilot:[a-z0-9-]+:[0-9a-f-]{36}$")
    owner_id: Literal["urn:glucopilot:owner:self"] = DEPLOYMENT_OWNER_ID
    basis: IdentityBasis
    source_record_id: str | None = Field(
        default=None,
        pattern=r"^urn:glucopilot:source-record:[0-9a-f-]{36}$",
    )
    parent_id: str | None = Field(
        default=None,
        pattern=r"^urn:glucopilot:[a-z0-9-]+:[0-9a-f-]{36}$",
    )

    @model_validator(mode="after")
    def validate_identity_basis(self) -> CanonicalIdentity:
        if self.basis == IdentityBasis.SOURCE_DETERMINISTIC and not self.source_record_id:
            raise ValueError("source-deterministic identity requires source_record_id")
        if self.basis == IdentityBasis.PARENT_SCOPED and not self.parent_id:
            raise ValueError("parent-scoped identity requires parent_id")
        return self


class TimeMeaning(StrEnum):
    OBSERVED = "observed"
    RECORDED = "recorded"
    RECEIVED = "received"
    EFFECTIVE = "effective"


class TimePrecision(StrEnum):
    SECOND = "second"
    MINUTE = "minute"
    HOUR = "hour"
    DAY = "day"
    MONTH = "month"
    YEAR = "year"
    UNKNOWN = "unknown"


class TimeBasis(StrEnum):
    EXACT = "exact"
    PATIENT_REPORTED = "patient_reported"
    SOURCE_REPORTED = "source_reported"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class DstResolution(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    UNAMBIGUOUS = "unambiguous"
    AMBIGUOUS_EARLIER_OFFSET = "ambiguous_earlier_offset"
    AMBIGUOUS_LATER_OFFSET = "ambiguous_later_offset"
    NONEXISTENT_LOCAL_TIME = "nonexistent_local_time"
    UNRESOLVED = "unresolved"


class LocalTimeContext(FrozenContractModel):
    timezone: str = Field(min_length=1)
    utc_offset: str | None = None
    dst_resolution: DstResolution

    @model_validator(mode="after")
    def validate_local_context(self) -> LocalTimeContext:
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"unknown IANA timezone: {self.timezone}") from error
        resolved = {
            DstResolution.UNAMBIGUOUS,
            DstResolution.AMBIGUOUS_EARLIER_OFFSET,
            DstResolution.AMBIGUOUS_LATER_OFFSET,
        }
        if self.dst_resolution in resolved:
            if not self.utc_offset or not _OFFSET.fullmatch(self.utc_offset):
                raise ValueError("resolved local time requires an explicit UTC offset")
        elif self.utc_offset is not None:
            raise ValueError("unresolved or nonexistent local time cannot claim a UTC offset")
        return self


class PartialTime(FrozenContractModel):
    """A lossless time value, including partial and unresolved local times."""

    value: str | None = None
    precision: TimePrecision
    basis: TimeBasis
    local_context: LocalTimeContext | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> PartialTime:
        if self.precision == TimePrecision.UNKNOWN:
            if self.value is not None or self.basis != TimeBasis.UNKNOWN or self.local_context is not None:
                raise ValueError("unknown time requires a null value and unknown basis")
            return self
        if not self.value:
            raise ValueError("known time precision requires a value")

        if self.precision == TimePrecision.DAY:
            if not _DATE.fullmatch(self.value):
                raise ValueError("day precision requires YYYY-MM-DD")
            datetime.strptime(self.value, "%Y-%m-%d")
        elif self.precision == TimePrecision.MONTH:
            if not _MONTH.fullmatch(self.value):
                raise ValueError("month precision requires YYYY-MM")
            datetime.strptime(self.value, "%Y-%m")
        elif self.precision == TimePrecision.YEAR:
            if not _YEAR.fullmatch(self.value):
                raise ValueError("year precision requires YYYY")
        else:
            self._validate_datetime_value()
        return self

    def _validate_datetime_value(self) -> None:
        normalized = self.value.replace("Z", "+00:00") if self.value else ""
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as error:
            raise ValueError("time precision requires an ISO 8601 datetime") from error
        is_aware = parsed.tzinfo is not None and parsed.utcoffset() is not None
        if is_aware:
            if parsed.utcoffset().total_seconds() != 0:
                raise ValueError("canonical instants must be normalized to UTC")
            if self.local_context and self.local_context.dst_resolution in {
                DstResolution.NONEXISTENT_LOCAL_TIME,
                DstResolution.UNRESOLVED,
            }:
                raise ValueError("an unresolved local time cannot also claim a canonical instant")
        elif not self.local_context:
            raise ValueError("naive local datetime requires explicit local-time context")
        else:
            self._validate_naive_local_time(parsed)

    def _validate_naive_local_time(self, parsed: datetime) -> None:
        context = self.local_context
        if context is None:
            return
        zone = ZoneInfo(context.timezone)
        candidates = []
        for fold in (0, 1):
            aware = parsed.replace(tzinfo=zone, fold=fold)
            as_utc = aware.astimezone(timezone.utc)
            round_trip = as_utc.astimezone(zone).replace(tzinfo=None)
            if round_trip == parsed:
                candidates.append((as_utc, aware.utcoffset()))
        candidates = list(dict.fromkeys(candidates))
        if not candidates:
            if context.dst_resolution != DstResolution.NONEXISTENT_LOCAL_TIME:
                raise ValueError("nonexistent local time must be marked explicitly")
            return
        if len(candidates) == 1:
            if context.dst_resolution != DstResolution.UNAMBIGUOUS:
                raise ValueError("unambiguous local time must be marked explicitly")
            expected = _format_offset(candidates[0][1])
            if context.utc_offset != expected:
                raise ValueError("local-time UTC offset does not match the IANA timezone")
            return
        if context.dst_resolution == DstResolution.UNRESOLVED:
            return
        ordered = sorted(candidates, key=lambda candidate: candidate[0])
        expected_resolution = {
            DstResolution.AMBIGUOUS_EARLIER_OFFSET: ordered[0],
            DstResolution.AMBIGUOUS_LATER_OFFSET: ordered[-1],
        }
        if context.dst_resolution not in expected_resolution:
            raise ValueError("ambiguous local time requires earlier, later, or unresolved status")
        expected = _format_offset(expected_resolution[context.dst_resolution][1])
        if context.utc_offset != expected:
            raise ValueError("selected DST offset does not match the IANA timezone")


def _format_offset(offset) -> str:
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


class EffectiveTimeKind(StrEnum):
    POINT = "point"
    INTERVAL = "interval"
    OPEN_ENDED = "open_ended"
    UNKNOWN = "unknown"


class EffectiveTime(FrozenContractModel):
    kind: EffectiveTimeKind
    start: PartialTime | None = None
    end: PartialTime | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> EffectiveTime:
        if self.kind == EffectiveTimeKind.UNKNOWN:
            if self.start is not None or self.end is not None:
                raise ValueError("unknown effective time cannot include boundaries")
        elif self.kind == EffectiveTimeKind.POINT:
            if self.start is None or self.end is not None:
                raise ValueError("point effective time requires only start")
        elif self.kind == EffectiveTimeKind.INTERVAL:
            if self.start is None or self.end is None:
                raise ValueError("interval effective time requires start and end")
        elif self.kind == EffectiveTimeKind.OPEN_ENDED:
            if self.start is None or self.end is not None:
                raise ValueError("open-ended effective time requires only start")
        return self


class TemporalMetadata(FrozenContractModel):
    observed: PartialTime | None = None
    recorded: PartialTime | None = None
    received: PartialTime
    effective: EffectiveTime

    @model_validator(mode="after")
    def validate_received_time(self) -> TemporalMetadata:
        if self.received.precision != TimePrecision.SECOND or self.received.basis != TimeBasis.EXACT:
            raise ValueError("received time must be an exact second-precision UTC instant")
        return self


class SourceClass(StrEnum):
    DEVICE_OR_PROVIDER = "device_or_provider"
    CLINICAL_DOCUMENT = "clinical_document"
    CLINICIAN = "clinician"
    PATIENT = "patient"
    IMPORT = "import"
    SYSTEM = "system"
    ALGORITHM = "algorithm"
    EXTERNAL_KNOWLEDGE = "external_knowledge"


class AssertionKind(StrEnum):
    SOURCE_FACT = "source_fact"
    PATIENT_REPORT = "patient_report"
    DERIVED_STATISTIC = "derived_statistic"
    HYPOTHESIS = "hypothesis"
    CLINICIAN_CONFIRMATION = "clinician_confirmation"


class AssertionStatus(StrEnum):
    UNVERIFIED = "unverified"
    PROVISIONAL = "provisional"
    CONFIRMED = "confirmed"
    DISPUTED = "disputed"
    REFUTED = "refuted"
    SUPERSEDED = "superseded"
    ENTERED_IN_ERROR = "entered_in_error"


class EvidenceLevel(StrEnum):
    NONE = "none"
    ASSERTION_ONLY = "assertion_only"
    SOURCE_RECORD = "source_record"
    CORROBORATED = "corroborated"
    CLINICIAN_REVIEWED = "clinician_reviewed"


class ConfidenceLabel(StrEnum):
    NOT_ASSESSED = "not_assessed"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ConfidenceMetadata(FrozenContractModel):
    label: ConfidenceLabel = ConfidenceLabel.NOT_ASSESSED
    score: float | None = Field(default=None, ge=0, le=1)
    method: str | None = Field(default=None, min_length=1)
    calibration_version: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_assessment(self) -> ConfidenceMetadata:
        assessed = self.label != ConfidenceLabel.NOT_ASSESSED or self.score is not None
        if assessed and not self.method:
            raise ValueError("assessed confidence requires a method")
        if not assessed and (self.method or self.calibration_version):
            raise ValueError("unassessed confidence cannot claim a method or calibration")
        return self


class AlgorithmVersion(FrozenContractModel):
    algorithm_id: str = Field(pattern=r"^[a-z][a-z0-9._-]*$")
    version: str
    implementation_commit: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{7,40}$",
    )
    model_id: str | None = Field(default=None, min_length=1)
    model_version: str | None = Field(default=None, min_length=1)
    parameters_hash: str | None = None

    @model_validator(mode="after")
    def validate_algorithm_version(self) -> AlgorithmVersion:
        if not _SEMVER.fullmatch(self.version):
            raise ValueError("algorithm version must be semantic versioning")
        if self.parameters_hash and not _SHA256.fullmatch(self.parameters_hash):
            raise ValueError("parameters_hash must be a lowercase sha256 digest")
        if bool(self.model_id) != bool(self.model_version):
            raise ValueError("model_id and model_version must be supplied together")
        return self


class DataVersion(FrozenContractModel):
    contract_name: Literal["glucopilot-clinical-data-contracts"] = DATA_CONTRACT_NAME
    contract_version: Literal["1.0.0"] = DATA_CONTRACT_VERSION
    schema_version: int = Field(ge=1)
    source_revision: str | None = Field(default=None, min_length=1)
    input_snapshot_id: str | None = Field(default=None, min_length=1)
    input_hash: str | None = None
    algorithm: AlgorithmVersion | None = None

    @model_validator(mode="after")
    def validate_input_version(self) -> DataVersion:
        if self.input_hash and not _SHA256.fullmatch(self.input_hash):
            raise ValueError("input_hash must be a lowercase sha256 digest")
        if self.algorithm and not (self.input_snapshot_id or self.input_hash):
            raise ValueError("algorithm data version requires an immutable input version")
        return self

    @staticmethod
    def hash_inputs(canonical_json: bytes) -> str:
        return f"sha256:{hashlib.sha256(canonical_json).hexdigest()}"


class ProvenanceMetadata(FrozenContractModel):
    source_class: SourceClass
    source_system: str = Field(min_length=1)
    assertion_kind: AssertionKind
    assertion_status: AssertionStatus
    evidence_level: EvidenceLevel
    confidence: ConfidenceMetadata = Field(default_factory=ConfidenceMetadata)
    data_version: DataVersion
    evidence_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def prevent_semantic_conflation(self) -> ProvenanceMetadata:
        if self.assertion_kind == AssertionKind.SOURCE_FACT:
            if self.source_class in {SourceClass.ALGORITHM, SourceClass.PATIENT}:
                raise ValueError("an algorithm output or patient report is not a source fact")
            if self.evidence_level == EvidenceLevel.NONE:
                raise ValueError("a source fact requires evidence")
        if self.assertion_kind == AssertionKind.PATIENT_REPORT:
            if self.source_class != SourceClass.PATIENT:
                raise ValueError("patient report requires patient source class")
        if self.assertion_kind == AssertionKind.CLINICIAN_CONFIRMATION:
            if self.source_class != SourceClass.CLINICIAN:
                raise ValueError("clinician confirmation requires clinician source class")
            if self.evidence_level != EvidenceLevel.CLINICIAN_REVIEWED:
                raise ValueError("clinician confirmation requires clinician-reviewed evidence")
            if self.assertion_status in {
                AssertionStatus.UNVERIFIED,
                AssertionStatus.PROVISIONAL,
            }:
                raise ValueError("clinician confirmation cannot be unverified or provisional")
        if self.source_class == SourceClass.ALGORITHM and not self.data_version.algorithm:
            raise ValueError("algorithm provenance requires an algorithm version")
        if self.assertion_kind == AssertionKind.DERIVED_STATISTIC and not self.data_version.algorithm:
            raise ValueError("derived statistics require an algorithm version")
        if (
            self.evidence_level
            in {
                EvidenceLevel.SOURCE_RECORD,
                EvidenceLevel.CORROBORATED,
                EvidenceLevel.CLINICIAN_REVIEWED,
            }
            and not self.evidence_ids
        ):
            raise ValueError("the selected evidence level requires evidence IDs")
        if (
            self.evidence_level
            in {
                EvidenceLevel.NONE,
                EvidenceLevel.ASSERTION_ONLY,
            }
            and self.evidence_ids
        ):
            raise ValueError("none/assertion-only evidence cannot include evidence IDs")
        return self


class IdentityPolicy(StrEnum):
    APPLICATION_RECORD = "application_record"
    SOURCE_OR_LEGACY_RECORD = "source_or_legacy_record"
    OWNER_SINGLETON = "owner_singleton"
    OWNER_NATURAL_KEY = "owner_natural_key"
    PARENT_SCOPED = "parent_scoped"
    DERIVED_OUTPUT = "derived_output"
    LEGACY_UNDEFINED = "legacy_undefined"


@dataclass(frozen=True)
class TimeFieldContract:
    field: str
    meaning: TimeMeaning
    precision: tuple[TimePrecision, ...]


@dataclass(frozen=True)
class EntityContract:
    entity_type: str
    identity_policy: IdentityPolicy
    time_fields: tuple[TimeFieldContract, ...]
    assertion_kinds: frozenset[AssertionKind]
    source_classes: frozenset[SourceClass]
    requires_algorithm_version: bool = False


def _time(
    field: str,
    meaning: TimeMeaning,
    *precision: TimePrecision,
) -> TimeFieldContract:
    return TimeFieldContract(field, meaning, tuple(precision))


FACT = frozenset({AssertionKind.SOURCE_FACT})
PATIENT_ASSERTION = frozenset({AssertionKind.PATIENT_REPORT})
FACT_OR_PATIENT = frozenset({AssertionKind.SOURCE_FACT, AssertionKind.PATIENT_REPORT})
DERIVED = frozenset({AssertionKind.DERIVED_STATISTIC})
DERIVED_OR_HYPOTHESIS = frozenset({AssertionKind.DERIVED_STATISTIC, AssertionKind.HYPOTHESIS})
DEVICE_SOURCE = frozenset({SourceClass.DEVICE_OR_PROVIDER, SourceClass.IMPORT})
PATIENT_SOURCE = frozenset({SourceClass.PATIENT})
CLINICAL_SOURCE = frozenset({SourceClass.CLINICAL_DOCUMENT, SourceClass.CLINICIAN, SourceClass.PATIENT})
SYSTEM_SOURCE = frozenset({SourceClass.SYSTEM})
ALGORITHM_SOURCE = frozenset({SourceClass.ALGORITHM})

UTC_INSTANT = (TimePrecision.SECOND, TimePrecision.MINUTE)
LOCAL_DATE = (TimePrecision.DAY, TimePrecision.MONTH, TimePrecision.YEAR, TimePrecision.UNKNOWN)
ENVELOPE_TIMES = (
    _time("created_date", TimeMeaning.RECEIVED, TimePrecision.SECOND),
    _time("updated_date", TimeMeaning.RECORDED, TimePrecision.SECOND),
)


def _contract(
    entity_type: str,
    identity_policy: IdentityPolicy,
    assertion_kinds: frozenset[AssertionKind],
    source_classes: frozenset[SourceClass],
    *time_fields: TimeFieldContract,
    derived: bool = False,
) -> EntityContract:
    return EntityContract(
        entity_type,
        identity_policy,
        tuple(time_fields) + ENVELOPE_TIMES,
        assertion_kinds,
        source_classes,
        derived,
    )


# Every registered legacy entity has an explicit destination contract. This map
# documents future typed schemas; it does not alter current persistence.
ENTITY_CONTRACTS = {
    item.entity_type: item
    for item in (
        _contract(
            "GlucoseReading",
            IdentityPolicy.SOURCE_OR_LEGACY_RECORD,
            FACT,
            DEVICE_SOURCE,
            _time("timestamp", TimeMeaning.OBSERVED, *UTC_INSTANT),
        ),
        _contract(
            "Treatment",
            IdentityPolicy.SOURCE_OR_LEGACY_RECORD,
            FACT_OR_PATIENT,
            DEVICE_SOURCE | PATIENT_SOURCE,
            _time("timestamp", TimeMeaning.EFFECTIVE, *UTC_INSTANT),
        ),
        _contract("DailySummary", IdentityPolicy.LEGACY_UNDEFINED, DERIVED, ALGORITHM_SOURCE, derived=True),
        _contract("WeeklySummary", IdentityPolicy.LEGACY_UNDEFINED, DERIVED, ALGORITHM_SOURCE, derived=True),
        _contract(
            "Pattern",
            IdentityPolicy.DERIVED_OUTPUT,
            DERIVED_OR_HYPOTHESIS,
            ALGORITHM_SOURCE,
            _time("first_detected", TimeMeaning.RECORDED, *UTC_INSTANT),
            _time("last_detected", TimeMeaning.RECORDED, *UTC_INSTANT),
            derived=True,
        ),
        _contract(
            "Insight",
            IdentityPolicy.DERIVED_OUTPUT,
            DERIVED_OR_HYPOTHESIS,
            ALGORITHM_SOURCE,
            _time("date_generated", TimeMeaning.RECORDED, *UTC_INSTANT),
            derived=True,
        ),
        _contract(
            "AIConversation",
            IdentityPolicy.LEGACY_UNDEFINED,
            PATIENT_ASSERTION | frozenset({AssertionKind.HYPOTHESIS}),
            PATIENT_SOURCE | SYSTEM_SOURCE | ALGORITHM_SOURCE,
        ),
        _contract(
            "PeriodLog",
            IdentityPolicy.OWNER_NATURAL_KEY,
            FACT_OR_PATIENT | DERIVED,
            DEVICE_SOURCE | PATIENT_SOURCE | ALGORITHM_SOURCE,
            _time("date", TimeMeaning.OBSERVED, *LOCAL_DATE),
        ),
        _contract("NightscoutProfile", IdentityPolicy.OWNER_NATURAL_KEY, FACT, DEVICE_SOURCE),
        _contract(
            "OuraConnection",
            IdentityPolicy.OWNER_SINGLETON,
            FACT,
            DEVICE_SOURCE,
            _time("expires_at", TimeMeaning.EFFECTIVE, *UTC_INSTANT),
            _time("last_sync", TimeMeaning.RECORDED, *UTC_INSTANT),
        ),
        _contract(
            "OuraDaily",
            IdentityPolicy.OWNER_NATURAL_KEY,
            FACT,
            DEVICE_SOURCE,
            _time("date", TimeMeaning.OBSERVED, TimePrecision.DAY),
        ),
        _contract(
            "OuraHeartRate",
            IdentityPolicy.SOURCE_OR_LEGACY_RECORD,
            FACT,
            DEVICE_SOURCE,
            _time("timestamp", TimeMeaning.OBSERVED, *UTC_INSTANT),
        ),
        _contract("UserSettings", IdentityPolicy.OWNER_SINGLETON, FACT_OR_PATIENT, PATIENT_SOURCE | SYSTEM_SOURCE),
        _contract(
            "DexcomConnection",
            IdentityPolicy.OWNER_SINGLETON,
            FACT,
            DEVICE_SOURCE,
            _time("expires_at", TimeMeaning.EFFECTIVE, *UTC_INSTANT),
            _time("last_sync", TimeMeaning.RECORDED, *UTC_INSTANT),
        ),
        _contract(
            "MedicalRecord",
            IdentityPolicy.OWNER_NATURAL_KEY,
            FACT,
            frozenset({SourceClass.CLINICAL_DOCUMENT}),
            _time("record_date", TimeMeaning.EFFECTIVE, *LOCAL_DATE),
            _time("uploaded_at", TimeMeaning.RECEIVED, *UTC_INSTANT),
        ),
        _contract(
            "LabResult",
            IdentityPolicy.PARENT_SCOPED,
            FACT,
            frozenset({SourceClass.CLINICAL_DOCUMENT, SourceClass.CLINICIAN}),
            _time("collected_date", TimeMeaning.OBSERVED, *LOCAL_DATE),
        ),
        _contract(
            "FitbitConnection",
            IdentityPolicy.OWNER_SINGLETON,
            FACT,
            DEVICE_SOURCE,
            _time("expires_at", TimeMeaning.EFFECTIVE, *UTC_INSTANT),
            _time("last_sync", TimeMeaning.RECORDED, *UTC_INSTANT),
        ),
        _contract(
            "FitbitDaily",
            IdentityPolicy.OWNER_NATURAL_KEY,
            FACT,
            DEVICE_SOURCE,
            _time("date", TimeMeaning.OBSERVED, TimePrecision.DAY),
        ),
        _contract(
            "FitbitHeartRate",
            IdentityPolicy.SOURCE_OR_LEGACY_RECORD,
            FACT,
            DEVICE_SOURCE,
            _time("timestamp", TimeMeaning.OBSERVED, *UTC_INSTANT),
        ),
        _contract(
            "FingerstickReading",
            IdentityPolicy.APPLICATION_RECORD,
            PATIENT_ASSERTION,
            PATIENT_SOURCE,
            _time("timestamp", TimeMeaning.OBSERVED, *UTC_INSTANT),
        ),
        _contract(
            "GoogleHealthConnection",
            IdentityPolicy.OWNER_SINGLETON,
            FACT,
            DEVICE_SOURCE,
            _time("expires_at", TimeMeaning.EFFECTIVE, *UTC_INSTANT),
            _time("last_sync", TimeMeaning.RECORDED, *UTC_INSTANT),
        ),
        _contract(
            "HealthProfile",
            IdentityPolicy.OWNER_SINGLETON,
            PATIENT_ASSERTION,
            PATIENT_SOURCE,
            _time("date_of_birth", TimeMeaning.OBSERVED, *LOCAL_DATE),
        ),
        _contract(
            "WeightLog",
            IdentityPolicy.APPLICATION_RECORD,
            PATIENT_ASSERTION,
            PATIENT_SOURCE,
            _time("date", TimeMeaning.OBSERVED, TimePrecision.DAY),
        ),
        _contract(
            "Diagnosis",
            IdentityPolicy.APPLICATION_RECORD,
            frozenset({AssertionKind.PATIENT_REPORT, AssertionKind.CLINICIAN_CONFIRMATION}),
            frozenset({SourceClass.PATIENT, SourceClass.CLINICIAN}),
            _time("diagnosed_date", TimeMeaning.EFFECTIVE, *LOCAL_DATE),
        ),
        _contract(
            "Medication",
            IdentityPolicy.APPLICATION_RECORD,
            frozenset({AssertionKind.PATIENT_REPORT, AssertionKind.CLINICIAN_CONFIRMATION}),
            frozenset({SourceClass.PATIENT, SourceClass.CLINICIAN}),
        ),
        _contract(
            "Allergy",
            IdentityPolicy.APPLICATION_RECORD,
            frozenset({AssertionKind.PATIENT_REPORT, AssertionKind.CLINICIAN_CONFIRMATION}),
            frozenset({SourceClass.PATIENT, SourceClass.CLINICIAN}),
        ),
        _contract(
            "InsuranceInfo",
            IdentityPolicy.OWNER_SINGLETON,
            FACT_OR_PATIENT,
            CLINICAL_SOURCE,
            _time("effective_date", TimeMeaning.EFFECTIVE, *LOCAL_DATE),
        ),
        _contract(
            "SymptomLog",
            IdentityPolicy.APPLICATION_RECORD,
            PATIENT_ASSERTION,
            PATIENT_SOURCE,
            _time("entry_date", TimeMeaning.OBSERVED, *LOCAL_DATE),
        ),
        _contract(
            "HistoryEntry",
            IdentityPolicy.APPLICATION_RECORD,
            PATIENT_ASSERTION,
            PATIENT_SOURCE,
            _time("entry_date", TimeMeaning.EFFECTIVE, *LOCAL_DATE),
        ),
        _contract(
            "HealthSummary",
            IdentityPolicy.DERIVED_OUTPUT,
            DERIVED,
            ALGORITHM_SOURCE,
            _time("generated_at", TimeMeaning.RECORDED, *UTC_INSTANT),
            derived=True,
        ),
        _contract(
            "HealthMemory",
            IdentityPolicy.APPLICATION_RECORD,
            frozenset({AssertionKind.PATIENT_REPORT, AssertionKind.HYPOTHESIS}),
            frozenset({SourceClass.PATIENT, SourceClass.ALGORITHM}),
        ),
        _contract(
            "CompanionThread", IdentityPolicy.APPLICATION_RECORD, FACT_OR_PATIENT, PATIENT_SOURCE | SYSTEM_SOURCE
        ),
        _contract(
            "ChatMessage",
            IdentityPolicy.PARENT_SCOPED,
            PATIENT_ASSERTION | frozenset({AssertionKind.HYPOTHESIS}),
            PATIENT_SOURCE | ALGORITHM_SOURCE,
        ),
        _contract("BugReport", IdentityPolicy.APPLICATION_RECORD, PATIENT_ASSERTION, PATIENT_SOURCE),
    )
}

if len(ENTITY_CONTRACTS) != 34:
    raise RuntimeError("entity contract mapping must contain all 34 registered entities")
