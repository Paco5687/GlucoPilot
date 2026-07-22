"""Governed vocabulary for the rebuildable SQLite relationship projection.

Registry rows are inserted only by ordered migrations.  Runtime code imports
the same immutable definitions so an accepted edge cannot drift from the
schema-level subject/object and provenance rules.
"""

from __future__ import annotations

from dataclasses import dataclass

from .data_contracts import AssertionStatus, EvidenceLevel


@dataclass(frozen=True)
class PredicateSpec:
    name: str
    subject_type: str
    object_type: str
    inverse: str
    description: str
    derived_only: bool = True


@dataclass(frozen=True)
class AssertionStatusSpec:
    name: AssertionStatus
    terminal: bool
    description: str


@dataclass(frozen=True)
class EvidenceLevelSpec:
    name: EvidenceLevel
    rank: int
    requires_evidence: bool
    clinician_reviewed: bool
    description: str


@dataclass(frozen=True)
class AlgorithmSpec:
    algorithm_id: str
    version: str
    output_kind: str
    deterministic: bool
    rebuildable: bool
    description: str


ASSERTION_STATUSES = (
    AssertionStatusSpec(AssertionStatus.UNVERIFIED, False, "Not independently checked"),
    AssertionStatusSpec(AssertionStatus.PROVISIONAL, False, "Tentative pending more evidence"),
    AssertionStatusSpec(AssertionStatus.CONFIRMED, False, "Confirmed within its assertion kind"),
    AssertionStatusSpec(AssertionStatus.DISPUTED, False, "Conflicting evidence or attribution exists"),
    AssertionStatusSpec(AssertionStatus.REFUTED, True, "Evidence refutes this assertion"),
    AssertionStatusSpec(AssertionStatus.SUPERSEDED, True, "Replaced by a newer versioned assertion"),
    AssertionStatusSpec(AssertionStatus.ENTERED_IN_ERROR, True, "Recorded in error and excluded from use"),
)

EVIDENCE_LEVELS = (
    EvidenceLevelSpec(EvidenceLevel.NONE, 0, False, False, "No supporting evidence claimed"),
    EvidenceLevelSpec(EvidenceLevel.ASSERTION_ONLY, 1, False, False, "Attribution only; no source evidence"),
    EvidenceLevelSpec(EvidenceLevel.SOURCE_RECORD, 2, True, False, "Linked immutable source evidence"),
    EvidenceLevelSpec(EvidenceLevel.CORROBORATED, 3, True, False, "Supported by independent evidence"),
    EvidenceLevelSpec(EvidenceLevel.CLINICIAN_REVIEWED, 4, True, True, "Explicitly reviewed by a clinician"),
)

PREDICATES = (
    PredicateSpec(
        "extracted_from",
        "LabResult",
        "MedicalRecord",
        "has_lab_result",
        "A lab result was extracted from a medical record",
    ),
    PredicateSpec(
        "has_lab_result",
        "MedicalRecord",
        "LabResult",
        "extracted_from",
        "A medical record contains an extracted lab result",
    ),
    PredicateSpec(
        "member_of_thread",
        "ChatMessage",
        "CompanionThread",
        "has_message",
        "A message belongs to a Companion thread",
    ),
    PredicateSpec(
        "has_message",
        "CompanionThread",
        "ChatMessage",
        "member_of_thread",
        "A Companion thread contains a message",
    ),
)

ALGORITHMS = (
    AlgorithmSpec(
        "legacy-reference-projection",
        "1.0.0",
        "relationship",
        True,
        True,
        "Projects governed edges from versioned legacy entity references",
    ),
)

ASSERTION_STATUS_BY_NAME = {str(item.name): item for item in ASSERTION_STATUSES}
EVIDENCE_LEVEL_BY_NAME = {str(item.name): item for item in EVIDENCE_LEVELS}
PREDICATE_BY_NAME = {item.name: item for item in PREDICATES}
ALGORITHM_BY_KEY = {(item.algorithm_id, item.version): item for item in ALGORITHMS}

if any(
    len(index) != len(items)
    for index, items in (
        (ASSERTION_STATUS_BY_NAME, ASSERTION_STATUSES),
        (EVIDENCE_LEVEL_BY_NAME, EVIDENCE_LEVELS),
        (PREDICATE_BY_NAME, PREDICATES),
        (ALGORITHM_BY_KEY, ALGORITHMS),
    )
):
    raise RuntimeError("duplicate governed relationship registry entry")
