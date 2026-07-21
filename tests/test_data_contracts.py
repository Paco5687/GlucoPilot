from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

pytestmark = pytest.mark.risk_critical

from server.data_contracts import (
    DATA_CONTRACT_NAME,
    DATA_CONTRACT_VERSION,
    DEPLOYMENT_OWNER_ID,
    AlgorithmVersion,
    AssertionKind,
    AssertionStatus,
    CanonicalIdentity,
    ConfidenceLabel,
    ConfidenceMetadata,
    DataVersion,
    DstResolution,
    EffectiveTime,
    EffectiveTimeKind,
    ENTITY_CONTRACTS,
    EvidenceLevel,
    IdentityBasis,
    LocalTimeContext,
    PartialTime,
    ProvenanceMetadata,
    SourceClass,
    TemporalMetadata,
    TimeBasis,
    TimeMeaning,
    TimePrecision,
    canonical_entity_id,
    canonical_source_record_id,
)
from server.schema_registry import ENTITY_SCHEMA_BY_NAME


def _utc(value: str = "2026-07-21T17:05:00Z") -> PartialTime:
    return PartialTime(
        value=value,
        precision=TimePrecision.SECOND,
        basis=TimeBasis.EXACT,
    )


def _version(*, algorithm: bool = False) -> DataVersion:
    algorithm_version = None
    if algorithm:
        algorithm_version = AlgorithmVersion(
            algorithm_id="cycle-inference",
            version="1.2.0",
            implementation_commit="9f00d89",
            parameters_hash="sha256:" + "a" * 64,
        )
    return DataVersion(
        schema_version=1,
        input_snapshot_id="urn:glucopilot:input-snapshot:test",
        input_hash="sha256:" + "b" * 64,
        algorithm=algorithm_version,
    )


def test_all_registered_entities_have_one_versioned_contract():
    assert set(ENTITY_CONTRACTS) == set(ENTITY_SCHEMA_BY_NAME)
    assert len(ENTITY_CONTRACTS) == 34
    assert DATA_CONTRACT_NAME == "glucopilot-clinical-data-contracts"
    assert DATA_CONTRACT_VERSION == "1.0.0"
    assert DEPLOYMENT_OWNER_ID == "urn:glucopilot:owner:self"

    for entity_type, contract in ENTITY_CONTRACTS.items():
        assert contract.entity_type == entity_type
        assert contract.assertion_kinds
        assert contract.source_classes
        fields = [item.field for item in contract.time_fields]
        assert fields.count("created_date") == 1
        assert fields.count("updated_date") == 1
        assert all(item.precision for item in contract.time_fields)


def test_published_document_maps_every_registered_entity_and_core_semantic():
    document = (Path(__file__).parents[1] / "docs" / "data-platform" / "DATA_CONTRACTS.md").read_text(encoding="utf-8")
    for entity_type in ENTITY_SCHEMA_BY_NAME:
        assert f"`{entity_type}`" in document
    for term in (
        "observed",
        "recorded",
        "received",
        "effective",
        "source_fact",
        "patient_report",
        "derived_statistic",
        "hypothesis",
        "clinician_confirmation",
    ):
        assert f"`{term}`" in document


def test_canonical_ids_are_stable_type_scoped_and_value_hiding():
    first = canonical_entity_id("GlucoseReading", "legacy-123")
    assert first == canonical_entity_id("GlucoseReading", "legacy-123")
    assert first != canonical_entity_id("Treatment", "legacy-123")
    assert "legacy-123" not in first

    source = canonical_source_record_id("dexcom-v3", "provider-record-456")
    assert source == canonical_source_record_id("dexcom-v3", "provider-record-456")
    assert "provider-record-456" not in source


def test_identity_basis_requires_source_or_parent_linkage():
    canonical_id = canonical_entity_id("LabResult", "row-1")
    with pytest.raises(ValidationError, match="source_record_id"):
        CanonicalIdentity(
            canonical_id=canonical_id,
            basis=IdentityBasis.SOURCE_DETERMINISTIC,
        )
    with pytest.raises(ValidationError, match="parent_id"):
        CanonicalIdentity(
            canonical_id=canonical_id,
            basis=IdentityBasis.PARENT_SCOPED,
        )

    identity = CanonicalIdentity(
        canonical_id=canonical_id,
        basis=IdentityBasis.PARENT_SCOPED,
        parent_id=canonical_entity_id("MedicalRecord", "record-1"),
    )
    assert identity.owner_id == DEPLOYMENT_OWNER_ID
    source_identity = CanonicalIdentity(
        canonical_id=canonical_entity_id("GlucoseReading", "row-2"),
        basis=IdentityBasis.SOURCE_DETERMINISTIC,
        source_record_id=canonical_source_record_id("dexcom-v3", "record-2"),
    )
    assert source_identity.source_record_id.startswith("urn:glucopilot:source-record:")


def test_partial_time_preserves_exact_date_inferred_and_unknown_semantics():
    assert _utc().precision == TimePrecision.SECOND
    date_only = PartialTime(
        value="2026-07-21",
        precision=TimePrecision.DAY,
        basis=TimeBasis.SOURCE_REPORTED,
    )
    assert date_only.value == "2026-07-21"
    inferred = PartialTime(
        value="2026-07",
        precision=TimePrecision.MONTH,
        basis=TimeBasis.INFERRED,
    )
    assert inferred.basis == TimeBasis.INFERRED
    unknown = PartialTime(
        value=None,
        precision=TimePrecision.UNKNOWN,
        basis=TimeBasis.UNKNOWN,
    )
    assert unknown.value is None

    with pytest.raises(ValidationError, match="normalized to UTC"):
        PartialTime(
            value="2026-07-21T13:05:00-04:00",
            precision=TimePrecision.SECOND,
            basis=TimeBasis.EXACT,
        )
    with pytest.raises(ValidationError, match="unknown time"):
        PartialTime(
            value="2026",
            precision=TimePrecision.UNKNOWN,
            basis=TimeBasis.UNKNOWN,
        )
    with pytest.raises(ValidationError, match="unknown time"):
        PartialTime(
            value=None,
            precision=TimePrecision.UNKNOWN,
            basis=TimeBasis.UNKNOWN,
            local_context=LocalTimeContext(
                timezone="America/New_York",
                dst_resolution=DstResolution.NOT_APPLICABLE,
            ),
        )


def test_dst_ambiguity_and_nonexistent_local_time_are_explicit():
    earlier = LocalTimeContext(
        timezone="America/New_York",
        utc_offset="-04:00",
        dst_resolution=DstResolution.AMBIGUOUS_EARLIER_OFFSET,
    )
    ambiguous = PartialTime(
        value="2026-11-01T01:30:00",
        precision=TimePrecision.MINUTE,
        basis=TimeBasis.SOURCE_REPORTED,
        local_context=earlier,
    )
    assert ambiguous.local_context.dst_resolution == DstResolution.AMBIGUOUS_EARLIER_OFFSET

    nonexistent = PartialTime(
        value="2026-03-08T02:30:00",
        precision=TimePrecision.MINUTE,
        basis=TimeBasis.SOURCE_REPORTED,
        local_context=LocalTimeContext(
            timezone="America/New_York",
            dst_resolution=DstResolution.NONEXISTENT_LOCAL_TIME,
        ),
    )
    assert nonexistent.local_context.utc_offset is None

    with pytest.raises(ValidationError, match="explicit UTC offset"):
        LocalTimeContext(
            timezone="America/New_York",
            dst_resolution=DstResolution.AMBIGUOUS_LATER_OFFSET,
        )
    with pytest.raises(ValidationError, match="cannot claim a UTC offset"):
        LocalTimeContext(
            timezone="America/New_York",
            utc_offset="-05:00",
            dst_resolution=DstResolution.UNRESOLVED,
        )
    with pytest.raises(ValidationError, match="unambiguous local time"):
        PartialTime(
            value="2026-07-21T13:30:00",
            precision=TimePrecision.MINUTE,
            basis=TimeBasis.SOURCE_REPORTED,
            local_context=LocalTimeContext(
                timezone="America/New_York",
                dst_resolution=DstResolution.UNRESOLVED,
            ),
        )


def test_temporal_roles_and_effective_interval_remain_distinct():
    temporal = TemporalMetadata(
        observed=PartialTime(
            value="2026-07-21",
            precision=TimePrecision.DAY,
            basis=TimeBasis.PATIENT_REPORTED,
        ),
        recorded=_utc("2026-07-22T09:00:00Z"),
        received=_utc("2026-07-22T09:00:01Z"),
        effective=EffectiveTime(
            kind=EffectiveTimeKind.OPEN_ENDED,
            start=PartialTime(
                value="2026-07",
                precision=TimePrecision.MONTH,
                basis=TimeBasis.PATIENT_REPORTED,
            ),
        ),
    )
    assert temporal.observed.value == "2026-07-21"
    assert temporal.effective.kind == EffectiveTimeKind.OPEN_ENDED

    with pytest.raises(ValidationError, match="received time"):
        TemporalMetadata(
            received=PartialTime(
                value="2026-07-22T09:00:00Z",
                precision=TimePrecision.MINUTE,
                basis=TimeBasis.EXACT,
            ),
            effective=EffectiveTime(kind=EffectiveTimeKind.UNKNOWN),
        )


def test_assertion_categories_cannot_be_conflated():
    source_fact = ProvenanceMetadata(
        source_class=SourceClass.CLINICAL_DOCUMENT,
        source_system="uploaded-record",
        assertion_kind=AssertionKind.SOURCE_FACT,
        assertion_status=AssertionStatus.UNVERIFIED,
        evidence_level=EvidenceLevel.SOURCE_RECORD,
        evidence_ids=("urn:glucopilot:evidence:document-page-1",),
        data_version=_version(),
    )
    assert source_fact.assertion_kind == AssertionKind.SOURCE_FACT

    patient_report = ProvenanceMetadata(
        source_class=SourceClass.PATIENT,
        source_system="manual-entry",
        assertion_kind=AssertionKind.PATIENT_REPORT,
        assertion_status=AssertionStatus.CONFIRMED,
        evidence_level=EvidenceLevel.ASSERTION_ONLY,
        data_version=_version(),
    )
    assert patient_report.assertion_status == AssertionStatus.CONFIRMED
    assert patient_report.assertion_kind != AssertionKind.CLINICIAN_CONFIRMATION

    derived = ProvenanceMetadata(
        source_class=SourceClass.ALGORITHM,
        source_system="cycle-inference",
        assertion_kind=AssertionKind.DERIVED_STATISTIC,
        assertion_status=AssertionStatus.PROVISIONAL,
        evidence_level=EvidenceLevel.CORROBORATED,
        evidence_ids=("urn:glucopilot:input-snapshot:test",),
        confidence=ConfidenceMetadata(
            label=ConfidenceLabel.MEDIUM,
            score=0.72,
            method="temperature-rule-calibration",
            calibration_version="1.0.0",
        ),
        data_version=_version(algorithm=True),
    )
    assert derived.data_version.algorithm.version == "1.2.0"

    clinician = ProvenanceMetadata(
        source_class=SourceClass.CLINICIAN,
        source_system="clinician-review",
        assertion_kind=AssertionKind.CLINICIAN_CONFIRMATION,
        assertion_status=AssertionStatus.CONFIRMED,
        evidence_level=EvidenceLevel.CLINICIAN_REVIEWED,
        evidence_ids=("urn:glucopilot:evidence:review-1",),
        data_version=_version(),
    )
    assert clinician.assertion_kind == AssertionKind.CLINICIAN_CONFIRMATION


@pytest.mark.parametrize(
    ("source_class", "assertion_kind", "evidence_level", "algorithm", "match"),
    [
        (
            SourceClass.ALGORITHM,
            AssertionKind.SOURCE_FACT,
            EvidenceLevel.SOURCE_RECORD,
            True,
            "not a source fact",
        ),
        (
            SourceClass.PATIENT,
            AssertionKind.SOURCE_FACT,
            EvidenceLevel.ASSERTION_ONLY,
            False,
            "not a source fact",
        ),
        (
            SourceClass.SYSTEM,
            AssertionKind.PATIENT_REPORT,
            EvidenceLevel.ASSERTION_ONLY,
            False,
            "patient source class",
        ),
        (
            SourceClass.CLINICAL_DOCUMENT,
            AssertionKind.CLINICIAN_CONFIRMATION,
            EvidenceLevel.SOURCE_RECORD,
            False,
            "clinician source class",
        ),
        (
            SourceClass.ALGORITHM,
            AssertionKind.HYPOTHESIS,
            EvidenceLevel.ASSERTION_ONLY,
            False,
            "algorithm version",
        ),
    ],
)
def test_invalid_assertion_conflation_is_rejected(
    source_class,
    assertion_kind,
    evidence_level,
    algorithm,
    match,
):
    with pytest.raises(ValidationError, match=match):
        ProvenanceMetadata(
            source_class=source_class,
            source_system="test",
            assertion_kind=assertion_kind,
            assertion_status=AssertionStatus.PROVISIONAL,
            evidence_level=evidence_level,
            evidence_ids=("urn:glucopilot:evidence:test",) if evidence_level == EvidenceLevel.SOURCE_RECORD else (),
            data_version=_version(algorithm=algorithm),
        )


def test_confidence_and_version_hashes_are_explicit():
    with pytest.raises(ValidationError, match="requires a method"):
        ConfidenceMetadata(label=ConfidenceLabel.HIGH, score=0.9)

    first = DataVersion.hash_inputs(b'{"a":1}')
    assert first == DataVersion.hash_inputs(b'{"a":1}')
    assert first != DataVersion.hash_inputs(b'{"a":2}')
    assert first.startswith("sha256:")

    with pytest.raises(ValidationError, match="immutable input version"):
        DataVersion(
            schema_version=1,
            algorithm=AlgorithmVersion(
                algorithm_id="test-algorithm",
                version="1.0.0",
            ),
        )

    with pytest.raises(ValidationError, match="cannot include evidence IDs"):
        ProvenanceMetadata(
            source_class=SourceClass.PATIENT,
            source_system="manual-entry",
            assertion_kind=AssertionKind.PATIENT_REPORT,
            assertion_status=AssertionStatus.CONFIRMED,
            evidence_level=EvidenceLevel.ASSERTION_ONLY,
            evidence_ids=("urn:glucopilot:evidence:unexpected",),
            data_version=_version(),
        )


def test_entity_mapping_keeps_time_roles_and_derived_versions_explicit():
    glucose_times = {item.field: item.meaning for item in ENTITY_CONTRACTS["GlucoseReading"].time_fields}
    assert glucose_times == {
        "timestamp": TimeMeaning.OBSERVED,
        "created_date": TimeMeaning.RECEIVED,
        "updated_date": TimeMeaning.RECORDED,
    }

    for entity_type in ("Pattern", "Insight", "HealthSummary", "DailySummary", "WeeklySummary"):
        assert ENTITY_CONTRACTS[entity_type].requires_algorithm_version
