"""Deterministic, evidence-linked clinician and specialty briefs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from .auth import require_login
from .evidence_bundle import EvidenceBundleQuery, EvidenceDomain, build_bundle
from .hypotheses import report_block as hypothesis_report


router = APIRouter(dependencies=[Depends(require_login)])
BRIEF_VERSION = "clinician-brief/1.0.0"

COMMON_TYPES = {
    "Diagnosis", "Medication", "Allergy", "SymptomLog", "HistoryEntry",
    "HealthProfile", "HealthEpisode", "MedicationExposure", "Pattern",
    "Insight", "ManagementBurdenSummary",
}
MODE_CONFIG = {
    "clinician": {
        "label": "Concise clinician",
        "domains": tuple(EvidenceDomain),
        "types": COMMON_TYPES | {
            "GlucoseReading", "FingerstickReading", "Treatment",
            "InsulinResponseEvent", "ActivityPositionEffect", "PeriodLog",
            "LabResult", "MedicalRecord", "OuraDaily", "FitbitDaily",
        },
        "keywords": (),
        "always": set(),
        "intent": "clinical concerns objective patterns glucose insulin management burden labs imaging reassuring opposing contradictions limitations",
        "questions": (
            "Which findings need confirmation or follow-up?",
            "Do the outcomes appear sustainable given the recorded management effort?",
            "Which contradictions or missing sources could change interpretation?",
        ),
    },
    "endocrinology": {
        "label": "Endocrinology",
        "domains": (EvidenceDomain.GLUCOSE, EvidenceDomain.INSULIN, EvidenceDomain.ANALYTICS, EvidenceDomain.LABS, EvidenceDomain.CLINICAL),
        "types": COMMON_TYPES | {"GlucoseReading", "FingerstickReading", "Treatment", "InsulinResponseEvent", "ActivityPositionEffect", "LabResult", "MedicalRecord"},
        "keywords": ("glucose", "insulin", "diabetes", "thyroid", "hormone", "metabolic", "a1c"),
        "always": {"GlucoseReading", "FingerstickReading", "Treatment", "InsulinResponseEvent", "ManagementBurdenSummary", "ActivityPositionEffect"},
        "intent": "endocrinology glucose insulin response burden metabolic thyroid labs contradictions",
        "questions": ("Which glucose or insulin observations merit review?", "Are data gaps or contradictions limiting interpretation?", "Is recorded management effort sustainable?"),
    },
    "gastroenterology": {
        "label": "Gastroenterology",
        "domains": (EvidenceDomain.CLINICAL, EvidenceDomain.LABS, EvidenceDomain.RECORDS, EvidenceDomain.ANALYTICS),
        "types": COMMON_TYPES | {"LabResult", "MedicalRecord"},
        "keywords": ("gastro", "stomach", "bowel", "nausea", "celiac", "liver", "abdominal", "digestion"),
        "always": {"ManagementBurdenSummary"},
        "intent": "gastroenterology abdominal gastrointestinal nutrition celiac liver labs symptoms burden contradictions",
        "questions": ("Which GI symptoms or objective findings need follow-up?", "Are nutritional or medication factors documented?", "Which missing tests or contradictions limit the brief?"),
    },
    "neurology_autonomic": {
        "label": "Neurology / autonomic",
        "domains": (EvidenceDomain.CLINICAL, EvidenceDomain.WEARABLES, EvidenceDomain.GLUCOSE, EvidenceDomain.LABS, EvidenceDomain.RECORDS, EvidenceDomain.ANALYTICS),
        "types": COMMON_TYPES | {"OuraDaily", "OuraHeartRate", "FitbitDaily", "FitbitHeartRate", "GlucoseReading", "LabResult", "MedicalRecord", "ActivityPositionEffect"},
        "keywords": ("neuro", "autonomic", "dizzy", "syncope", "heart rate", "neuropathy", "headache", "orthostatic"),
        "always": {"OuraDaily", "OuraHeartRate", "FitbitDaily", "FitbitHeartRate", "GlucoseReading", "ActivityPositionEffect", "ManagementBurdenSummary"},
        "intent": "neurology autonomic orthostatic dizziness heart rate sleep glucose symptoms labs burden contradictions",
        "questions": ("Are symptoms temporally associated with objective measurements?", "Which neurologic or autonomic findings remain unconfirmed?", "What additional measurements would reduce uncertainty?"),
    },
    "hematology": {
        "label": "Hematology",
        "domains": (EvidenceDomain.LABS, EvidenceDomain.RECORDS, EvidenceDomain.CLINICAL, EvidenceDomain.ANALYTICS),
        "types": COMMON_TYPES | {"LabResult", "MedicalRecord"},
        "keywords": ("blood", "hemat", "iron", "ferritin", "anemia", "platelet", "hemoglobin", "white cell"),
        "always": {"ManagementBurdenSummary"},
        "intent": "hematology blood count iron ferritin anemia platelets labs records symptoms contradictions",
        "questions": ("Which hematology results are verified and abnormal?", "Is there reassuring or opposing evidence?", "Which trends or missing studies need review?"),
    },
    "gynecology_reproductive": {
        "label": "Gynecology / reproductive",
        "domains": (EvidenceDomain.CYCLE, EvidenceDomain.CLINICAL, EvidenceDomain.LABS, EvidenceDomain.RECORDS, EvidenceDomain.GLUCOSE, EvidenceDomain.ANALYTICS),
        "types": COMMON_TYPES | {"PeriodLog", "LabResult", "MedicalRecord", "GlucoseReading"},
        "keywords": ("cycle", "period", "menstrual", "gynec", "reproductive", "pelvic", "fertility", "hormone"),
        "always": {"PeriodLog", "GlucoseReading", "ManagementBurdenSummary"},
        "intent": "gynecology reproductive menstrual cycle pelvic hormone glucose labs symptoms contradictions",
        "questions": ("Which cycle observations and symptoms are directly recorded?", "Are hormonal or reproductive hypotheses still tentative?", "Which missing evidence limits interpretation?"),
    },
    "primary_care": {
        "label": "Primary care",
        "domains": (EvidenceDomain.CLINICAL, EvidenceDomain.LABS, EvidenceDomain.RECORDS, EvidenceDomain.GLUCOSE, EvidenceDomain.INSULIN, EvidenceDomain.ANALYTICS, EvidenceDomain.WEARABLES, EvidenceDomain.CYCLE),
        "types": COMMON_TYPES | {"LabResult", "MedicalRecord", "GlucoseReading", "FingerstickReading", "Treatment", "InsulinResponseEvent", "OuraDaily", "FitbitDaily", "PeriodLog"},
        "keywords": (),
        "always": set(),
        "intent": "primary care active conditions medications allergies symptoms preventive labs glucose burden contradictions",
        "questions": ("Which active concerns need coordination or follow-up?", "Which objective findings are reassuring or contradictory?", "What missing evidence could change prioritization?"),
    },
}


class BriefRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str = "clinician"
    days: int = Field(default=90, ge=7, le=365)


def _checksum(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def _items(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = bundle["evidence"]
    return [
        *evidence["derived_metrics"],
        *evidence["direct_observations"],
        *evidence["documents"],
        *evidence["relationships"],
    ]


def _strength(item: dict[str, Any]) -> dict[str, Any]:
    confidence = item.get("confidence") or {}
    data = item.get("data") or {}
    status = (
        confidence.get("discovery_status")
        or (data.get("analytics_confidence") or {}).get("discovery_status")
        or data.get("assertion_status")
        or "observed"
    )
    leads = {
        "invalid": "Available data do not support a valid estimate.",
        "exploratory": "Exploratory observation from limited evidence.",
        "emerging": "Emerging observational signal.",
        "reproduced": "Repeated observational signal with temporal replication.",
        "not-reproduced": "Earlier signal was not reproduced in the later sample.",
        "confirmed": "Clinician-confirmed record.",
        "observed": "Recorded observation.",
    }
    return {
        "status": status,
        "label": confidence.get("label") or "not_assessed",
        "score": confidence.get("score"),
        "lead": leads.get(str(status), "Recorded or calculated item; review its evidence and limitations."),
        "definitive_allowed": status in {"confirmed"},
        "causal_allowed": False,
    }


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    return {
        "id": item["id"],
        "domain": item.get("domain"),
        "entity_type": item.get("entity_type"),
        "entity_id": item.get("entity_id"),
        "observed_at": item.get("observed_at"),
        "title": data.get("title") or data.get("test_name") or data.get("name") or item.get("entity_type"),
        "data": data,
        "evidence_strength": _strength(item),
        "source_links": item.get("source_links") or [],
        "claim": item.get("claim"),
    }


def _specialty_relevant(item: dict[str, Any], config: dict[str, Any]) -> bool:
    if not config["keywords"] or item.get("entity_type") in config["always"]:
        return True
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    text = json.dumps(data, sort_keys=True, default=str).lower()
    return any(keyword in text for keyword in config["keywords"])


def _relevant_hypotheses(config: dict[str, Any]) -> list[dict[str, Any]]:
    keywords = config["keywords"]
    output = []
    for hypothesis in hypothesis_report():
        text = " ".join(str(hypothesis.get(key) or "") for key in ("title", "description", "suggested_verification")).lower()
        if keywords and not any(keyword in text for keyword in keywords):
            continue
        confirmed = hypothesis.get("status") == "confirmed"
        output.append({
            **hypothesis,
            "semantic_class": "clinician_confirmed_hypothesis" if confirmed else "unconfirmed_hypothesis_not_diagnosis",
            "display_label": "Clinician-confirmed hypothesis" if confirmed else "Unconfirmed hypothesis — not a diagnosis",
            "definitive_allowed": confirmed,
        })
    return output


def build_brief(mode: str, days: int) -> dict[str, Any]:
    if mode not in MODE_CONFIG:
        mode = "clinician"
    config = MODE_CONFIG[mode]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=max(7, min(int(days), 365)))
    bundle = build_bundle(EvidenceBundleQuery(
        start=start,
        end=end,
        domains=list(config["domains"]),
        question_intent=config["intent"],
        item_budget=150,
    ))
    selected = [
        item for item in _items(bundle)
        if item.get("entity_type") in config["types"]
        and item.get("entity_type") != "InsuranceInfo"
        and _specialty_relevant(item, config)
    ]
    public = [_public_item(item) for item in selected]
    by_id = {item["id"]: item for item in public}
    concerns = [
        item for item in public
        if str((item["data"] or {}).get("flag") or "").lower() in {"high", "low", "abnormal", "critical"}
        or str((item["data"] or {}).get("severity") or "").lower() in {"moderate", "severe", "high", "critical"}
    ]
    patterns = [
        item for item in public
        if item["entity_type"] in {"Pattern", "Insight", "ActivityPositionEffect"}
    ]
    glucose_insulin = [
        item for item in public if item["domain"] in {"glucose", "insulin"}
    ]
    burden = [
        item for item in public if item["entity_type"] == "ManagementBurdenSummary"
    ]
    labs_imaging = [
        item for item in public if item["entity_type"] in {"LabResult", "MedicalRecord"}
    ]
    reassuring = [
        {"evidence": by_id.get(entry.get("evidence_item_id")), "reason": entry.get("reason")}
        for entry in bundle["evidence"]["reassuring_evidence"]
        if entry.get("evidence_item_id") in by_id
    ]
    opposing = [
        entry for entry in bundle["evidence"]["opposing_evidence"]
        if not entry.get("evidence_item_id") or entry.get("evidence_item_id") in by_id
    ]
    limitations = list(bundle["missing_data_caveats"])
    for item in public:
        for limitation in (item["data"].get("limitations") or []):
            limitations.append({"code": "item_limitation", "domain": item["domain"], "message": str(limitation), "evidence_item_id": item["id"]})
    contradictions = bundle["contradictions"]
    hypotheses = _relevant_hypotheses(config)
    appendix = [
        {
            "id": item["id"],
            "entity_type": item["entity_type"],
            "entity_id": item["entity_id"],
            "title": item["title"],
            "observed_at": item["observed_at"],
            "evidence_strength": item["evidence_strength"],
            "source_links": item["source_links"],
        }
        for item in public
    ]
    identity = {
        "version": BRIEF_VERSION,
        "mode": mode,
        "bundle_id": bundle["bundle_id"],
        "bundle_data_version": bundle["data_version"]["input_hash"],
        "selected_ids": [item["id"] for item in appendix],
        "hypothesis_ids": [item["id"] for item in hypotheses],
    }
    return {
        "brief_id": "urn:glucopilot:clinician-brief:" + _checksum(identity).removeprefix("sha256:"),
        "brief_version": BRIEF_VERSION,
        "mode": mode,
        "mode_label": config["label"],
        "generated_at": end.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "window": {"start": start.isoformat().replace("+00:00", "Z"), "end": end.isoformat().replace("+00:00", "Z"), "days": days},
        "evidence_bundle": {
            "id": bundle["bundle_id"],
            "version": bundle["bundle_version"],
            "data_version": bundle["data_version"],
            "query": bundle["query"],
        },
        "sections": {
            "concerns": concerns,
            "objective_patterns": patterns,
            "glucose_insulin": glucose_insulin,
            "management_burden": burden,
            "labs_imaging": labs_imaging,
            "reassuring_evidence": reassuring,
            "opposing_evidence": opposing,
            "contradictions": contradictions,
            "limitations": limitations,
            "hypotheses": hypotheses,
            "questions": list(config["questions"]),
        },
        "appendix": appendix,
        "privacy": {
            "policy": "specialty_minimum_necessary/1.0.0",
            "selected_entity_types": sorted({item["entity_type"] for item in public}),
            "always_omitted_entity_types": ["InsuranceInfo"],
            "note": "Only specialty-allowlisted Evidence Bundle items are included; other PHI is omitted.",
        },
        "language": {
            "hypotheses": "Unconfirmed hypotheses are tentative and are not diagnoses.",
            "associations": "Observed associations and calculations do not establish causation.",
            "clinical": "This brief supports clinician review and does not recommend treatment.",
        },
    }


@router.post("/api/briefs/clinician")
def clinician_brief(body: BriefRequest):
    return build_brief(body.mode, body.days)
