"""Deterministic, evidence-linked clinician and specialty briefs.

A brief is assembled from several bounded Evidence Bundle scopes so that the
standing clinical record (problems, medications, allergies, weight, labs) never
competes with high-volume rows for the item budget. High-volume sources (CGM,
pump events, wearable days, cycle days) are summarized into calculated items
instead of being listed row by row.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from statistics import mean, pstdev
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .auth import require_login
from .config import APP_TIMEZONE, OWNER_EMAIL
from .data_quality import assess_cgm, cgm_points
from .db import config_value
from .evidence_bundle import (
    MAX_ITEM_BUDGET,
    EvidenceBundleError,
    EvidenceBundleQuery,
    EvidenceDomain,
    build_bundle,
)
from .hypotheses import report_block as hypothesis_report
from .insulin_reconciliation import reconcile_treatments
from .insulin_response import build_response_events
from .lab_audit import ABNORMAL_FLAGS, effective_flag
from .lab_audit import summary_eligible as lab_summary_eligible
from .repositories import get_repositories
from .lab_names import canonical_lab_name


router = APIRouter(dependencies=[Depends(require_login)])
BRIEF_VERSION = "clinician-brief/2.0.0"

COMMON_TYPES = {
    "Diagnosis", "Medication", "Allergy", "WeightLog", "SymptomLog", "HistoryEntry",
    "HealthProfile", "HealthEpisode", "MedicationExposure", "Pattern",
    "Insight", "DailySummary", "WeeklySummary", "HealthSummary",
    "ManagementBurdenSummary",
}
HIGH_FREQUENCY_RAW_TYPES = {"GlucoseReading", "OuraHeartRate", "FitbitHeartRate"}
AUXILIARY_TYPES = {"HealthEpisode", "MedicationExposure", "InsulinResponseEvent"}
# Listed in every mode: the standing record a clinician reads first.
CORE_RECORD_TYPES = frozenset({"Diagnosis", "Medication", "Allergy", "WeightLog"})
# Never listed row by row; each becomes one calculated summary item.
SUMMARIZED_TYPES = frozenset({
    "GlucoseReading", "FingerstickReading", "Treatment", "InsulinResponseEvent",
    "OuraDaily", "FitbitDaily", "OuraHeartRate", "FitbitHeartRate", "PeriodLog",
})
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
        # Unconfirmed hypotheses are specialist work-up questions, not part of a
        # primary-care problem list.
        "omit_unconfirmed_hypotheses": True,
    },
}

# Evidence scopes. Each is its own bounded bundle query, so standing records,
# labs and weight are never ranked against analytics or raw rows.
LAB_LOOKBACK_DAYS = 730
WEIGHT_LOOKBACK_DAYS = 365
STANDING_START = datetime(1900, 1, 1, tzinfo=timezone.utc)
SCOPES = (
    # name, domain, entity types, intent, lookback (None = brief window, "all" = full history)
    ("standing", (EvidenceDomain.CLINICAL,), ("Diagnosis", "Medication", "Allergy", "HealthProfile"),
     "diagnosis medication allergy health profile", "all"),
    ("history", (EvidenceDomain.CLINICAL,), ("HistoryEntry", "SymptomLog", "HealthEpisode", "MedicationExposure"),
     "history entry symptom log", None),
    ("weight", (EvidenceDomain.CLINICAL,), ("WeightLog",), "weight log", WEIGHT_LOOKBACK_DAYS),
    ("labs_records", (EvidenceDomain.LABS, EvidenceDomain.RECORDS), ("LabResult", "MedicalRecord"),
     "lab result medical record", LAB_LOOKBACK_DAYS),
    ("analytics", (EvidenceDomain.ANALYTICS,),
     ("Pattern", "Insight", "DailySummary", "WeeklySummary", "HealthSummary"), None, None),
    # Glucose/insulin contradictions only; the rows themselves are summarized.
    ("signals", (EvidenceDomain.GLUCOSE, EvidenceDomain.INSULIN), ("FingerstickReading", "NightscoutProfile"),
     "fingerstick", None),
)
ANALYTICS_BUDGET = 150
SIGNALS_BUDGET = 1
# Raw high-volume reads for summaries (365 days of 5-minute CGM is ~105k rows).
SUMMARY_ROW_LIMIT = 400_000

CLINICAL_DOC_TYPES = frozenset({
    "lab_report", "imaging_report", "visit_summary", "pathology_report",
    "discharge_summary", "procedure_report", "operative_report", "consult_note",
    "referral", "radiology_report",
})
_INSURANCE_TEXT = re.compile(
    r"\binsurance\b|\binsurer\b|member\s*id|subscriber|\brx\s*(bin|pcn|grp|group)\b"
    r"|group\s*(number|no\b|#)|explanation of benefits|\beob\b|policy\s*(number|holder)"
    r"|\bcopay\b|health\s*plan\s*card",
    re.IGNORECASE,
)
_SUPPLEMENT_PROTOCOL = re.compile(
    r"\bsupplements?\b[^.]{0,80}\b(protocol|regimen|stack|schedule)\b"
    r"|\b(protocol|regimen|stack)\b[^.]{0,80}\bsupplements?\b",
    re.IGNORECASE,
)
_TENTATIVE_DIAGNOSIS = re.compile(
    r"\b(suspected|suspect|possible|probable|presumed|query|rule[\s-]?out|r/o|"
    r"provisional|working diagnosis|unconfirmed)\b|\?",
    re.IGNORECASE,
)
_REPORTED_SYMPTOMS = re.compile(r"^\s*reported symptoms?\b", re.IGNORECASE)
_ABNORMAL_FLAGS = ABNORMAL_FLAGS
_FLAG_LABELS = {
    "high": "High", "h": "High", "hh": "Critical high", "critical_high": "Critical high",
    "low": "Low", "l": "Low", "ll": "Critical low", "critical_low": "Critical low",
    "abnormal": "Abnormal", "a": "Abnormal", "critical": "Critical",
    "out_of_range": "Out of range", "positive": "Positive", "normal": "Normal",
}
_SPO2_TEXT = re.compile(r"spo2|blood oxygen|oxygen saturation", re.IGNORECASE)
SPO2_PLAUSIBLE = (70.0, 100.0)
# International consensus CGM targets (Battelino et al., Diabetes Care 2019).
TBR70_TARGET_PCT = 4.0
TBR54_TARGET_PCT = 1.0
WEIGHT_LOSS_CONCERN_PCT = 5.0
# Concerns stay a short, ranked list: weight loss, hypoglycemia and reported
# symptoms lead; abnormal labs follow as one grouped entry.
MAX_SYMPTOM_CONCERNS = 5
LAB_CONCERN_LISTED = 8
KG_PER_LB = 0.45359237


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


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fmt(value: Any, digits: int = 1) -> str:
    number = _number(value)
    if number is None:
        return str(value) if value not in (None, "") else ""
    rounded = round(number, digits)
    return f"{rounded:g}" if digits else f"{round(number):d}"


def _words(entity_type: str) -> str:
    text = re.sub(r"(?<!^)(?=[A-Z])", " ", entity_type or "Item").lower()
    return text[:1].upper() + text[1:]


def _day(value: Any) -> str | None:
    text = str(value or "")[:10]
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text


_OBJECT_PRECEDERS = {
    "for", "to", "with", "at", "from", "by", "of", "on", "about", "help", "helps",
    "let", "lets", "give", "gives", "show", "shows", "tell", "tells", "than",
}


def clinician_voice(text: Any, *, headline: bool = False) -> str:
    """Rewrite patient-facing second-person wording for a clinician reader.

    Headlines drop the possessive ("Your sleep lines up with your time in range"
    becomes "Sleep lines up with time in range"); body text moves to the third
    person, which keeps verb agreement intact ("you sleep" -> "they sleep").
    """
    value = " ".join(str(text or "").split())
    if not value:
        return ""
    if headline:
        value = re.sub(r"\byour\s+", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\byou're\b", "they're", value, flags=re.IGNORECASE)
    value = re.sub(r"\byou've\b", "they've", value, flags=re.IGNORECASE)
    value = re.sub(r"\byou'll\b", "they'll", value, flags=re.IGNORECASE)
    value = re.sub(r"\byourself\b", "themselves", value, flags=re.IGNORECASE)
    value = re.sub(r"\byours\b", "theirs", value, flags=re.IGNORECASE)
    value = re.sub(r"\byour\b", "their", value, flags=re.IGNORECASE)

    def pronoun(match: re.Match[str]) -> str:
        before = re.findall(r"[a-z]+", value[: match.start()].lower())
        return "them" if before and before[-1] in _OBJECT_PRECEDERS else "they"

    value = re.sub(r"\byou\b", pronoun, value, flags=re.IGNORECASE)
    return value[:1].upper() + value[1:]


def _strength(item: dict[str, Any]) -> dict[str, Any]:
    confidence = item.get("confidence") or {}
    data = item.get("data") or {}
    status = (
        confidence.get("discovery_status")
        or (data.get("analytics_confidence") or {}).get("discovery_status")
        or data.get("assertion_status")
        or ("calculated" if confidence.get("method") == "brief_summary" else None)
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
        "calculated": "Calculated from the recorded source data; descriptive only.",
    }
    return {
        "status": status,
        "label": confidence.get("label") or "not_assessed",
        "score": confidence.get("score"),
        "lead": leads.get(str(status), "Recorded or calculated item; review its evidence and limitations."),
        "definitive_allowed": status in {"confirmed"},
        "causal_allowed": False,
        "verified": confidence.get("clinically_verified"),
    }


def _reference(data: dict[str, Any]) -> str | None:
    low, high = _number(data.get("reference_low")), _number(data.get("reference_high"))
    if low is not None and high is not None and (low or high):
        return f"{_fmt(low, 2)}–{_fmt(high, 2)}"
    if high:
        return f"≤ {_fmt(high, 2)}"
    if low:
        return f"≥ {_fmt(low, 2)}"
    text = str(data.get("reference_range") or "").strip()
    return text or None


def _lab_flag(data: dict[str, Any]) -> str:
    """The flag the value and its reference range support (see effective_flag)."""
    return effective_flag(data)[0]


def _flag_label(flag: str) -> str:
    return _FLAG_LABELS.get(flag, flag.replace("_", " ").title())


def _display(item: dict[str, Any]) -> dict[str, Any]:
    """Readable label, value, unit, date, reference range and flag for one item."""
    entity_type = item.get("entity_type") or ""
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    display: dict[str, Any] = {
        "label": None, "value": None, "unit": None, "date": _day(item.get("observed_at")),
        "reference": None, "flag": None, "status": None, "detail": None,
    }
    if entity_type == "LabResult":
        flag, replaced = effective_flag(data)
        display.update({
            "label": canonical_lab_name(data["test_name"]) if data.get("test_name") else "Lab result",
            "value": _fmt(data.get("value"), 2) or str(data.get("value_text") or ""),
            "unit": data.get("unit") or None,
            "reference": _reference(data),
            "flag": _flag_label(flag) if flag else None,
            "detail": data.get("category") or None,
        })
        if replaced:
            display["flag_note"] = (
                f"Source flagged {_flag_label(replaced)}; shown as {_flag_label(flag)} from the value and its reference range."
            )
        confidence = item.get("confidence") or {}
        if confidence.get("clinically_verified") is False:
            display["status"] = "Unverified extraction"
    elif entity_type == "Diagnosis":
        display.update({
            "label": data.get("name") or "Diagnosis",
            "status": str(data.get("status") or "active"),
            "date": _day(data.get("diagnosed_date")),
            "detail": data.get("notes") or None,
        })
    elif entity_type == "Medication":
        kind = str(data.get("kind") or "medication").lower()
        display.update({
            "label": data.get("name") or "Medication",
            "value": data.get("dose") or None,
            "detail": " · ".join(part for part in (data.get("frequency"), data.get("route"), data.get("notes")) if part) or None,
            "status": "supplement" if kind == "supplement" else str(data.get("status") or "active"),
            "date": None,
        })
    elif entity_type == "Allergy":
        display.update({
            "label": data.get("allergen") or "Allergy",
            "detail": data.get("reaction") or data.get("notes") or None,
            "flag": (str(data.get("severity")).title() if data.get("severity") else None),
            "date": None,
        })
    elif entity_type == "WeightLog":
        kg = _number(data.get("weight_kg"))
        display.update({
            "label": "Weight",
            "value": f"{_fmt(kg)} kg ({_fmt(kg / KG_PER_LB)} lb)" if kg else None,
            "date": _day(data.get("date")),
        })
    elif entity_type == "HistoryEntry":
        display.update({
            "label": data.get("title") or "History entry",
            "status": data.get("kind") or data.get("category") or None,
            "date": _day(data.get("entry_date")),
            "detail": (str(data.get("details") or data.get("notes") or "")[:600] or None),
        })
    elif entity_type == "SymptomLog":
        display.update({
            "label": data.get("symptom") or data.get("title") or "Reported symptom",
            "flag": (str(data.get("severity")).title() if data.get("severity") else None),
            "date": _day(data.get("entry_date")),
            "detail": data.get("notes") or data.get("description") or None,
        })
    elif entity_type == "MedicalRecord":
        doc_type = str(data.get("doc_type") or "")
        display.update({
            "label": data.get("title") or _words(doc_type.title().replace("_", "")) or "Document",
            "value": doc_type.replace("_", " ").capitalize() or None,
            "date": _day(data.get("record_date")),
            "detail": (str(data.get("summary") or "")[:600] or None),
        })
    elif entity_type == "Pattern":
        display.update({
            "label": clinician_voice(data.get("title"), headline=True) or _words(str(data.get("pattern_type") or "Pattern").replace("_", " ")),
            "value": f"{data['occurrences']} occurrences" if data.get("occurrences") else None,
            "status": data.get("time_of_day") or None,
            "detail": clinician_voice(data.get("explanation")) or None,
        })
    elif entity_type == "Insight":
        display.update({
            "label": clinician_voice(data.get("title"), headline=True) or "Insight",
            "detail": clinician_voice(data.get("description")) or None,
        })
    elif entity_type == "HealthEpisode":
        display.update({
            "label": data.get("title") or _words(str(data.get("episode_type") or "Episode")),
            "date": _day(data.get("start_time")),
            "detail": data.get("description") or None,
        })
    elif entity_type == "MedicationExposure":
        display.update({
            "label": data.get("medication_name") or "Medication exposure",
            "value": data.get("dose") or None,
            "date": _day(data.get("start_time")),
            "detail": " – ".join(part for part in (_day(data.get("start_time")), _day(data.get("end_time")) or "ongoing") if part),
        })
    elif entity_type == "ManagementBurdenSummary":
        summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
        outcome = data.get("outcome_vs_effort") if isinstance(data.get("outcome_vs_effort"), dict) else {}
        parts = []
        if summary.get("average_active_management_minutes_per_day") is not None:
            parts.append(f"{_fmt(summary['average_active_management_minutes_per_day'], 0)} min/day of active management")
        if summary.get("measured_interactions_per_day") is not None:
            parts.append(f"{_fmt(summary['measured_interactions_per_day'])} recorded interactions/day")
        if summary.get("days_with_measured_effort") is not None:
            parts.append(f"{summary['days_with_measured_effort']} days measured")
        detail = "; ".join(parts) + "." if parts else ""
        if outcome.get("language"):
            detail = f"{detail} {outcome['language']}".strip()
        display.update({
            "label": "Management burden",
            "value": f"Effort index {summary['measured_effort_index']}" if summary.get("measured_effort_index") is not None else None,
            "flag": "Sustainability review" if outcome.get("sustainability_review_flag") else None,
            "detail": detail or None,
        })
    elif entity_type == "HealthProfile":
        parts = []
        if data.get("diabetes_type"):
            parts.append(f"Diabetes type: {data['diabetes_type']}")
        if data.get("sex"):
            parts.append(f"Sex: {data['sex']}")
        if _number(data.get("height_cm")):
            parts.append(f"Height {_fmt(data['height_cm'], 0)} cm")
        display.update({"label": "Health profile", "detail": "; ".join(parts) or None, "date": None})
    if isinstance(item.get("display"), dict):
        display.update({key: value for key, value in item["display"].items() if value is not None})
    if not display["label"]:
        display["label"] = clinician_voice(data.get("title") or data.get("name"), headline=True) or _words(entity_type)
    return display


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    display = _display(item)
    return {
        "id": item["id"],
        "domain": item.get("domain"),
        "entity_type": item.get("entity_type"),
        "entity_id": item.get("entity_id"),
        "observed_at": item.get("observed_at"),
        "title": display["label"],
        "display": display,
        "data": data,
        "evidence_strength": _strength(item),
        "source_links": item.get("source_links") or [],
        "claim": item.get("claim"),
    }


def _specialty_relevant(item: dict[str, Any], config: dict[str, Any]) -> bool:
    if (
        not config["keywords"]
        or item.get("entity_type") in config["always"]
        or item.get("entity_type") in CORE_RECORD_TYPES
        or item.get("_derived")
    ):
        return True
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    text = json.dumps(data, sort_keys=True, default=str).lower()
    return any(keyword in text for keyword in config["keywords"])


def document_exclusion(data: dict[str, Any]) -> str | None:
    """Return why a MedicalRecord must not appear in a clinical brief, if it must not."""
    doc_type = str(data.get("doc_type") or "").strip().lower()
    text = " ".join(
        str(data.get(key) or "")
        for key in ("title", "summary", "filename", "source_name", "doc_type")
    )
    if "insurance" in doc_type or _INSURANCE_TEXT.search(text):
        return "insurance"
    if doc_type not in CLINICAL_DOC_TYPES:
        return "non_clinical"
    # Extraction can label a self-authored supplement regimen a visit summary.
    if doc_type not in {"lab_report", "imaging_report", "radiology_report", "pathology_report"} and _SUPPLEMENT_PROTOCOL.search(text):
        return "non_clinical"
    return None


def _is_tentative_diagnosis(data: dict[str, Any]) -> bool:
    status = str(data.get("status") or "").lower()
    if status in {"suspected", "possible", "provisional", "rule_out", "unconfirmed"}:
        return True
    return bool(_TENTATIVE_DIAGNOSIS.search(str(data.get("name") or "")))


def _hypothesis_labels(hypothesis: dict[str, Any]) -> dict[str, Any]:
    confirmed = hypothesis.get("status") == "confirmed"
    evidence_state = hypothesis.get("evidence_state") or "preliminary"
    if confirmed:
        semantic_class = "clinician_confirmed_hypothesis"
        display_label = "Clinician-confirmed hypothesis"
        status_label = "Confirmed"
    elif evidence_state == "not_evaluated":
        semantic_class = "unevaluated_hypothesis_not_diagnosis"
        display_label = (
            "Unconfirmed — not a diagnosis. Not evaluated — no supporting or opposing evidence recorded"
        )
        status_label = "Unconfirmed"
    else:
        semantic_class = "unconfirmed_hypothesis_not_diagnosis"
        state = hypothesis.get("evidence_state_label") or "evidence under review"
        display_label = f"Unconfirmed — not a diagnosis. {state}"
        status_label = "Unconfirmed"
    return {
        **hypothesis,
        "semantic_class": semantic_class,
        "display_label": display_label,
        "status_label": status_label,
        "definitive_allowed": confirmed,
    }


def _relevant_hypotheses(config: dict[str, Any]) -> list[dict[str, Any]]:
    keywords = config["keywords"]
    output = []
    for hypothesis in hypothesis_report():
        text = " ".join(str(hypothesis.get(key) or "") for key in ("title", "description", "suggested_verification")).lower()
        if keywords and not any(keyword in text for keyword in keywords):
            continue
        labelled = _hypothesis_labels(hypothesis)
        if config.get("omit_unconfirmed_hypotheses") and not labelled["definitive_allowed"]:
            continue
        output.append(labelled)
    return output


# ---------------------------------------------------------------------------
# Raw source reads for calculated summaries


def _timezone() -> ZoneInfo:
    try:
        return ZoneInfo(config_value("app_timezone", APP_TIMEZONE))
    except (sqlite3.Error, LookupError, ValueError):
        return ZoneInfo(APP_TIMEZONE)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _source_rows(entity_type: str, field: str, start: datetime, end: datetime, *, dated: bool = False) -> list[dict[str, Any]]:
    """Bounded owner-scoped rows of one type inside the brief window."""
    lower = start.date().isoformat() if dated else _iso(start)
    upper = end.date().isoformat() if dated else _iso(end)
    repositories = get_repositories()
    repository = {
        "OuraDaily": repositories.oura_daily,
        "FitbitDaily": repositories.fitbit_daily,
    }.get(entity_type) or repositories.entity(entity_type)
    rows: list[dict[str, Any]] = []
    page_size = 5000
    while True:
        page = repository.query(
            {"owner_email": OWNER_EMAIL, field: {"$gte": lower, "$lte": upper}},
            field,
            page_size,
            len(rows),
        )
        rows.extend(page)
        if len(rows) > SUMMARY_ROW_LIMIT:
            raise EvidenceBundleError(
                f"{entity_type} rows exceed the bounded summary limit; narrow the time range"
            )
        if len(page) < page_size:
            return rows


def _summary_item(entity_type: str, domain: str, data: dict[str, Any], display: dict[str, Any], *, source_links=None) -> dict[str, Any]:
    identity = _checksum({"type": entity_type, "data": data}).removeprefix("sha256:")[:32]
    return {
        "id": f"derived:{entity_type}:{identity}",
        "entity_type": entity_type,
        "entity_id": identity,
        "domain": domain,
        "observed_at": data.get("window_end"),
        "data": data,
        "display": display,
        "confidence": {"label": "not_assessed", "score": None, "method": "brief_summary"},
        "source_links": source_links or [],
        "claim": None,
        "_derived": True,
    }


def low_episodes(points: list[tuple[datetime, float]], tz: ZoneInfo) -> dict[str, int]:
    """CGM hypoglycemia episodes: ≥15 minutes below 70 mg/dL (level 2: below 54)."""
    episodes = level2 = nocturnal = 0
    run: list[tuple[datetime, float]] = []

    def close() -> None:
        nonlocal episodes, level2, nocturnal
        if run and (run[-1][0] - run[0][0]) >= timedelta(minutes=10):
            episodes += 1
            if 0 <= run[0][0].astimezone(tz).hour < 6:
                nocturnal += 1
            below54 = [point for point in run if point[1] < 54]
            if len(below54) >= 3 and below54[-1][0] - below54[0][0] >= timedelta(minutes=10):
                level2 += 1

    for timestamp, value in points:
        if value < 70 and (not run or timestamp - run[-1][0] <= timedelta(minutes=15)):
            run.append((timestamp, value))
            continue
        close()
        run = [(timestamp, value)] if value < 70 else []
    close()
    return {"episodes": episodes, "level2_episodes": level2, "nocturnal_episodes": nocturnal}


def _glucose_summary(rows: list[dict[str, Any]], tz: ZoneInfo, start: datetime, end: datetime, fingersticks: int) -> dict[str, Any] | None:
    points = cgm_points(rows, tz, start=start, end=end)
    if not points:
        return None
    values = [value for _, value in points]
    count = len(values)
    average = mean(values)
    quality = assess_cgm(rows, tz, start=start, end=end, as_of=end.astimezone(tz).date())

    def pct(predicate) -> float:
        return round(sum(1 for value in values if predicate(value)) / count * 100, 1)

    lows = low_episodes(points, tz)
    days = max(1.0, (end - start).total_seconds() / 86400)
    data = {
        "window_start": _iso(start),
        "window_end": _iso(end),
        "readings": count,
        "coverage_pct": quality["coverage_pct"],
        "data_through": quality["data_through"],
        "mean_mg_dl": round(average),
        "gmi_pct": round(3.31 + 0.02392 * average, 1),
        "cv_pct": round(pstdev(values) / average * 100, 1) if average else None,
        "tir_pct": pct(lambda value: 70 <= value <= 180),
        "tbr70_pct": pct(lambda value: value < 70),
        "tbr54_pct": pct(lambda value: value < 54),
        "tar180_pct": pct(lambda value: value > 180),
        "tar250_pct": pct(lambda value: value > 250),
        **lows,
        "episodes_per_week": round(lows["episodes"] / days * 7, 1),
        "fingerstick_readings": fingersticks,
        "targets": {"tbr70_pct": TBR70_TARGET_PCT, "tbr54_pct": TBR54_TARGET_PCT, "tir_pct": 70.0},
        "limitations": quality["limitations"],
    }
    detail = (
        f"Mean {data['mean_mg_dl']} mg/dL, GMI {data['gmi_pct']}%, CV {data['cv_pct']}%. "
        f"Above 180: {data['tar180_pct']}% (above 250: {data['tar250_pct']}%). "
        f"{lows['episodes']} low episodes ≥15 min ({data['episodes_per_week']}/week; "
        f"{lows['nocturnal_episodes']} overnight 00:00–06:00; {lows['level2_episodes']} below 54). "
        f"{count} CGM readings, {data['coverage_pct']}% coverage, data through {data['data_through']}."
    )
    if fingersticks:
        detail += f" {fingersticks} fingerstick readings recorded."
    return _summary_item("GlucoseSummary", "glucose", data, {
        "label": "CGM summary",
        "value": f"In range {data['tir_pct']}% · below 70: {data['tbr70_pct']}% · below 54: {data['tbr54_pct']}%",
        "date": data["data_through"],
        "reference": f"Targets: in range >70%, below 70 <{TBR70_TARGET_PCT:g}%, below 54 <{TBR54_TARGET_PCT:g}%",
        "detail": detail,
    })


def _insulin_summary(treatments: list[dict[str, Any]], glucose_rows: list[dict[str, Any]], tz: ZoneInfo, start: datetime, end: datetime) -> dict[str, Any] | None:
    if not treatments:
        return None
    start_day = start.astimezone(tz).date().isoformat()
    end_day = end.astimezone(tz).date().isoformat()
    reconciliation = reconcile_treatments(treatments, tz, start_date=start_day, end_date=end_day)
    summary = reconciliation["summary"]
    reconciled_days = reconciliation["days"]
    bolus_units = sum(float(day["calculated"]["bolus_units"] or 0) for day in reconciled_days)
    bolus_events = sum(int(day["calculated"]["bolus_events"] or 0) for day in reconciled_days)
    carbs = [_number(row.get("amount")) for row in treatments if row.get("type") == "carb"]
    carb_total = sum(value for value in carbs if value and value > 0)
    active_days = set()
    for row in treatments:
        if row.get("type") not in {"insulin", "carb"}:
            continue
        try:
            instant = datetime.fromisoformat(str(row.get("timestamp")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        active_days.add(instant.astimezone(tz).date())
    days = max(1, len(active_days))
    mode_minutes: dict[str, float] = {}
    for row in treatments:
        if row.get("type") == "pump_mode" and row.get("mode"):
            mode_minutes[str(row["mode"]).lower()] = mode_minutes.get(str(row["mode"]).lower(), 0.0) + (_number(row.get("duration")) or 0.0)
    total_mode = sum(mode_minutes.values())
    automated = round(mode_minutes.get("automatic", 0.0) / total_mode * 100) if total_mode else None
    temp_basals = sum(1 for row in treatments if row.get("type") == "tempbasal_correction")
    suspensions = sum(1 for row in treatments if row.get("type") == "suspension")
    tdd = summary.get("pump_reported_avg_tdd")
    tdd_source = "pump-reported"
    if tdd is None:
        tdd, tdd_source = summary.get("calculated_avg_tdd"), "calculated from delivery records"
    response = build_response_events(
        treatments, glucose_rows, timezone_name=str(tz), event_start=start, event_end=end
    ) if glucose_rows else None
    analysis = (response or {}).get("analysis") or {}
    data = {
        "window_start": _iso(start),
        "window_end": _iso(end),
        "days_with_pump_data": len(active_days),
        "avg_tdd_units": tdd,
        "tdd_source": tdd_source if tdd is not None else None,
        "tdd_days": summary.get("pump_reported_days") if tdd_source == "pump-reported" else summary.get("calculated_days"),
        "bolus_units_per_day": round(bolus_units / days, 1),
        "boluses_per_day": round(bolus_events / days, 1),
        "carbs_g_per_day": round(carb_total / days),
        "carb_entries": sum(1 for value in carbs if value),
        "manual_temp_basals": temp_basals,
        "suspensions": suspensions,
        "automated_mode_pct": automated,
        "correction_windows_clean": (response or {}).get("counts", {}).get("clean"),
        "correction_windows_total": (response or {}).get("counts", {}).get("total"),
        "median_drop_per_unit_mg_dl": analysis.get("median_nadir_drop_per_unit_mg_dl"),
        "limitations": list(summary.get("limitations") or []),
        "algorithm_version": reconciliation.get("algorithm_version"),
    }
    parts = [
        f"Bolus {data['bolus_units_per_day']} U/day over {data['boluses_per_day']} boluses/day",
        f"carbohydrate {data['carbs_g_per_day']} g/day ({data['carb_entries']} entries)",
    ]
    if automated is not None:
        parts.append(f"automated mode {automated}% of logged pump-mode time")
    if temp_basals:
        parts.append(f"{temp_basals} manual temp basals")
    if suspensions:
        parts.append(f"{suspensions} suspensions")
    detail = "; ".join(parts) + f". {len(active_days)} days with pump data."
    if data["median_drop_per_unit_mg_dl"] is not None:
        detail += (
            f" Unconfounded bolus windows: median glucose drop {data['median_drop_per_unit_mg_dl']} mg/dL per unit "
            f"(n={analysis.get('sample_count')} of {data['correction_windows_total']} windows)."
        )
    return _summary_item("InsulinSummary", "insulin", data, {
        "label": "Insulin & carbohydrate summary",
        "value": f"TDD {_fmt(tdd)} U/day ({tdd_source})" if tdd is not None else None,
        "date": end_day,
        "detail": detail,
    })


def _plausible_spo2(value: Any) -> float | None:
    number = _number(value)
    if number is None or not SPO2_PLAUSIBLE[0] <= number <= SPO2_PLAUSIBLE[1]:
        return None
    return number


def _wearable_summary(oura: list[dict[str, Any]], fitbit: list[dict[str, Any]], start: datetime, end: datetime) -> dict[str, Any] | None:
    if not oura and not fitbit:
        return None

    def avg(rows: list[dict[str, Any]], field: str, *, transform=None) -> float | None:
        values = [_number(row.get(field)) for row in rows]
        values = [value for value in values if value is not None]
        if transform:
            values = [value for value in (transform(value) for value in values) if value is not None]
        return round(mean(values), 1) if values else None

    spo2_raw = [row.get("spo2_average") for row in oura] + [row.get("spo2_avg") for row in fitbit]
    implausible = sum(1 for value in spo2_raw if _number(value) is not None and _plausible_spo2(value) is None)
    spo2 = [value for value in (_plausible_spo2(value) for value in spo2_raw) if value is not None]
    data = {
        "window_start": _iso(start),
        "window_end": _iso(end),
        "oura_days": len(oura),
        "fitbit_days": len(fitbit),
        "resting_hr_bpm": avg(fitbit, "resting_heart_rate") or avg(oura, "lowest_heart_rate"),
        "hrv_ms": avg(oura, "hrv") or avg(fitbit, "hrv"),
        "sleep_hours": avg(oura, "sleep_total_seconds", transform=lambda value: value / 3600 if value > 0 else None)
        or avg(fitbit, "sleep_minutes", transform=lambda value: value / 60 if value > 0 else None),
        "steps_per_day": avg(fitbit, "steps", transform=lambda value: value if value > 0 else None),
        "spo2_pct": round(mean(spo2), 1) if spo2 else None,
        "spo2_days": len(spo2),
        "spo2_implausible_days": implausible,
    }
    parts = []
    if data["resting_hr_bpm"] is not None:
        parts.append(f"resting HR {_fmt(data['resting_hr_bpm'], 0)} bpm")
    if data["hrv_ms"] is not None:
        parts.append(f"HRV {_fmt(data['hrv_ms'], 0)} ms")
    if data["sleep_hours"] is not None:
        parts.append(f"sleep {_fmt(data['sleep_hours'])} h/night")
    if data["steps_per_day"] is not None:
        parts.append(f"{_fmt(data['steps_per_day'], 0)} steps/day")
    if data["spo2_pct"] is not None:
        parts.append(f"SpO2 {data['spo2_pct']}% ({data['spo2_days']} days)")
    detail = "Averages: " + ", ".join(parts) + "." if parts else "No usable wearable averages."
    if implausible:
        detail += f" {implausible} SpO2 day values outside {SPO2_PLAUSIBLE[0]:g}–{SPO2_PLAUSIBLE[1]:g}% were excluded as device errors."
    return _summary_item("WearableSummary", "wearables", data, {
        "label": "Wearable summary",
        "value": f"{len(oura)} Oura days · {len(fitbit)} Fitbit days",
        "date": end.date().isoformat(),
        "detail": detail,
    })


def _cycle_summary(rows: list[dict[str, Any]], start: datetime, end: datetime) -> dict[str, Any] | None:
    phases = {str(row["date"])[:10]: str(row.get("phase") or "") for row in rows if row.get("date") and row.get("phase")}
    if not phases:
        return None
    onsets = []
    for day in sorted(phases):
        if phases[day] != "menstrual":
            continue
        previous = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
        if phases.get(previous) != "menstrual":
            onsets.append(day)
    lengths = [
        (date.fromisoformat(b) - date.fromisoformat(a)).days
        for a, b in zip(onsets, onsets[1:])
        if 15 <= (date.fromisoformat(b) - date.fromisoformat(a)).days <= 60
    ]
    inferred = sum(1 for row in rows if str(row.get("source") or "").endswith("inferred"))
    data = {
        "window_start": _iso(start),
        "window_end": _iso(end),
        "days_with_phase": len(phases),
        "inferred_days": inferred,
        "menstrual_onsets": onsets,
        "avg_cycle_length_days": round(mean(lengths), 1) if lengths else None,
    }
    detail = f"{len(phases)} days with a recorded phase ({inferred} inferred from wearable temperature)."
    if onsets:
        detail += f" Menstrual onsets: {', '.join(onsets)}."
    return _summary_item("CycleSummary", "cycle", data, {
        "label": "Cycle summary",
        "value": f"Average cycle {data['avg_cycle_length_days']} days" if lengths else f"{len(onsets)} menstrual onsets",
        "date": max(phases),
        "detail": detail,
    })


def _weight_trend(items: list[dict[str, Any]], start: datetime, end: datetime) -> dict[str, Any] | None:
    points = []
    for item in items:
        data = item.get("data") or {}
        day, kg = _day(data.get("date")), _number(data.get("weight_kg"))
        if day and kg and kg > 0:
            points.append((day, kg, item))
    if not points:
        return None
    points.sort(key=lambda point: (point[0], point[2]["id"]))
    latest = points[-1]
    peak = max(points, key=lambda point: (point[1], point[0]))
    window_points = [point for point in points if point[0] >= start.date().isoformat()]
    first_in_window = window_points[0] if window_points else None
    loss_kg = round(peak[1] - latest[1], 1)
    loss_pct = round(loss_kg / peak[1] * 100, 1) if peak[1] else 0.0
    data = {
        "window_start": _iso(start),
        "window_end": _iso(end),
        "latest_kg": latest[1],
        "latest_date": latest[0],
        "peak_kg": peak[1],
        "peak_date": peak[0],
        "lookback_days": WEIGHT_LOOKBACK_DAYS,
        "loss_from_peak_kg": loss_kg,
        "loss_from_peak_pct": loss_pct,
        "window_change_kg": round(latest[1] - first_in_window[1], 1) if first_in_window else None,
        "window_first_date": first_in_window[0] if first_in_window else None,
        "points": len(points),
        "threshold_pct": WEIGHT_LOSS_CONCERN_PCT,
    }

    def both(kg: float) -> str:
        return f"{_fmt(kg)} kg ({_fmt(kg / KG_PER_LB)} lb)"

    detail_parts = []
    if loss_kg > 0:
        detail_parts.append(
            f"Down {both(loss_kg)}, {loss_pct}%, from {both(peak[1])} on {peak[0]}, the highest value in the past {WEIGHT_LOOKBACK_DAYS} days"
        )
    if data["window_change_kg"] is not None and first_in_window[0] != latest[0]:
        change = data["window_change_kg"]
        direction = "down" if change < 0 else "up" if change > 0 else "unchanged"
        detail_parts.append(
            f"{direction} {both(abs(change))} since {first_in_window[0]}" if change else f"unchanged since {first_in_window[0]}"
        )
    detail = "; ".join(detail_parts) + f". {len(points)} weight entries." if detail_parts else f"{len(points)} weight entries."
    links = []
    for point in (peak, latest):
        for link in point[2].get("source_links") or []:
            if link not in links:
                links.append(link)
    return _summary_item("WeightTrend", "clinical", data, {
        "label": "Weight trend",
        "value": both(latest[1]),
        "date": latest[0],
        "detail": detail[:1].upper() + detail[1:],
    }, source_links=links)


# ---------------------------------------------------------------------------
# Selection


def _scope_queries(config: dict[str, Any], start: datetime, end: datetime) -> list[tuple[str, frozenset[str], EvidenceBundleQuery]]:
    queries = []
    mode_domains = set(config["domains"])
    for name, domains, types, intent, lookback in SCOPES:
        selected_domains = tuple(domain for domain in domains if domain in mode_domains)
        if not selected_domains:
            continue
        allowed = {
            entity_type for entity_type in types
            if entity_type in config["types"] or entity_type in CORE_RECORD_TYPES
        }
        if name == "signals":
            allowed = {
                entity_type for entity_type in types
                if (entity_type == "FingerstickReading" and EvidenceDomain.GLUCOSE in selected_domains)
                or (entity_type == "NightscoutProfile" and EvidenceDomain.INSULIN in selected_domains)
            }
        normalized = allowed - HIGH_FREQUENCY_RAW_TYPES - AUXILIARY_TYPES
        if not normalized:
            continue
        if lookback == "all":
            scope_start = STANDING_START
        elif lookback:
            scope_start = min(start, end - timedelta(days=lookback))
        else:
            scope_start = start
        budget = ANALYTICS_BUDGET if name == "analytics" else SIGNALS_BUDGET if name == "signals" else MAX_ITEM_BUDGET
        visible = frozenset(allowed | ({"ActivityPositionEffect", "ManagementBurdenSummary"} & config["types"] if name == "analytics" else set()))
        queries.append((name, visible, EvidenceBundleQuery(
            start=scope_start,
            end=end,
            domains=selected_domains,
            question_intent=intent or config["intent"],
            item_budget=budget,
            normalized_entity_types=tuple(sorted(normalized)),
        )))
    return queries


def _dedupe_key(item: dict[str, Any]) -> str:
    data = item.get("data") or {}
    supporting = data.get("supporting_data")
    if isinstance(supporting, str):
        try:
            supporting = json.loads(supporting)
        except ValueError:
            supporting = None
    if isinstance(supporting, dict) and supporting.get("kind") and not supporting.get("demo"):
        basis = [supporting.get(key) for key in ("kind", "x", "y", "best_phase", "worst_phase", "highest_phase", "lowest_phase")]
        return "insight:" + json.dumps(basis, default=str)
    if item.get("entity_type") == "Pattern" and data.get("pattern_type"):
        return f"pattern:{data.get('pattern_type')}:{data.get('time_of_day') or ''}"
    label = clinician_voice(data.get("title"), headline=True).lower()
    return "text:" + re.sub(r"[^a-z0-9]+", " ", label).strip()


def _patterns(items: list[dict[str, Any]], spo2_unreliable: bool, limitations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Active, deduplicated Pattern/Insight items; superseded generations are dropped."""
    groups: dict[str, list[dict[str, Any]]] = {}
    withheld_spo2 = 0
    for item in items:
        data = item.get("data") or {}
        if data.get("is_active") is False or data.get("is_dismissed") is True:
            continue
        text = " ".join(str(data.get(key) or "") for key in ("title", "description", "explanation", "supporting_data"))
        if spo2_unreliable and _SPO2_TEXT.search(text):
            withheld_spo2 += 1
            continue
        groups.setdefault(_dedupe_key(item), []).append(item)
    output = []
    for group in groups.values():
        group.sort(key=lambda entry: (str(entry.get("observed_at") or ""), entry["id"]), reverse=True)
        chosen = dict(group[0])
        if len(group) > 1:
            chosen["data"] = {**(chosen.get("data") or {}), "repeat_count": len(group)}
        output.append(chosen)
    if withheld_spo2:
        limitations.append({
            "code": "spo2_device_error",
            "domain": "wearables",
            "message": (
                f"{withheld_spo2} SpO2-based pattern(s) withheld: the SpO2 source contains "
                "physiologically implausible day values (for example 0%)."
            ),
        })
    output.sort(key=lambda entry: (str(entry.get("observed_at") or ""), entry["id"]), reverse=True)
    return output


def _labs(items: list[dict[str, Any]], start: datetime, excluded_records: set[str], config: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Most recent eligible value of each test, plus abnormal values in the window."""
    by_test: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        data = item.get("data") or {}
        if not data.get("test_name") or str(data.get("record_id") or "") in excluded_records:
            continue
        if not lab_summary_eligible(data) or not _specialty_relevant(item, config):
            continue
        # Synonyms ("HbA1c", "Hemoglobin A1c") are one test, listed once.
        key = canonical_lab_name(data["test_name"]).lower()
        by_test.setdefault(key, []).append(item)
    latest, abnormal = [], []
    window_start = start.date().isoformat()
    for results in by_test.values():
        results.sort(key=lambda entry: (str((entry.get("data") or {}).get("collected_date") or ""), entry["id"]))
        newest = results[-1]
        # The same draw extracted from two documents counts once.
        draws = {
            (
                str((entry.get("data") or {}).get("collected_date") or ""),
                str((entry.get("data") or {}).get("value")),
                str((entry.get("data") or {}).get("unit") or "").lower(),
            )
            for entry in results
        }
        newest = {**newest, "data": {**(newest.get("data") or {}), "result_count": len(draws)}}
        latest.append(newest)
        if _lab_flag(newest["data"]) in _ABNORMAL_FLAGS:
            abnormal.append(newest)
            continue
        earlier = [
            entry for entry in results[:-1]
            if _lab_flag(entry.get("data") or {}) in _ABNORMAL_FLAGS
            and str((entry.get("data") or {}).get("collected_date") or "") >= window_start
        ]
        if earlier:
            abnormal.append(earlier[-1])
    latest.sort(key=lambda entry: (
        _lab_flag(entry.get("data") or {}) not in _ABNORMAL_FLAGS,
        str((entry.get("data") or {}).get("category") or "~"),
        str((entry.get("data") or {}).get("test_name") or "").lower(),
    ))
    return latest, abnormal


_DOMAIN_LABELS = {
    "labs": "Labs", "glucose": "Glucose", "insulin": "Insulin", "clinical": "Clinical record",
    "wearables": "Wearables", "analytics": "Analytics", "records": "Documents", "cycle": "Cycle",
    "all": "All sources",
}


def _side(value: Any) -> str:
    """One side of a contradiction in a few readable words."""
    if isinstance(value, dict):
        parts = [
            f"{str(key).replace('_', ' ')} {_fmt(item, 2) if isinstance(item, (int, float)) else item}"
            for key, item in value.items()
            if isinstance(item, (str, int, float)) and not isinstance(item, bool)
            and not str(key).endswith("id") and len(str(item)) <= 60
        ]
        return ", ".join(parts[:4])
    return "" if value is None else str(value)[:120]


def _concern(item: dict[str, Any], kind: str, reason: str) -> dict[str, Any]:
    return {**item, "concern": {"kind": kind, "reason": reason}}


_FLAG_RANK = {"critical_high": 0, "critical_low": 0, "critical": 0, "hh": 0, "ll": 0}


def _lab_line(entry: dict[str, Any]) -> str:
    display = entry["display"]
    reference = f", ref {display['reference']}" if display["reference"] else ""
    return (
        f"{display['label']} {display['value']} {display['unit'] or ''}".strip()
        + f" ({display['flag'] or 'flagged'}{reference}, {display['date'] or 'undated'})"
    )


def _lab_group(entries: list[dict[str, Any]], window_start: str) -> tuple[dict[str, Any], str]:
    """Every abnormal lab as one concern item and its reason: critical results
    first, then the most recent. The full table stays in the Labs section."""
    # Most recent first, undated last; then critical results ahead (stable sort).
    entries = sorted(entries, key=lambda entry: entry["display"]["date"] or "", reverse=True)
    entries.sort(key=lambda entry: _FLAG_RANK.get(_lab_flag(entry.get("data") or {}), 1))
    listed = entries[:LAB_CONCERN_LISTED]
    more = len(entries) - len(listed)
    reason = "; ".join(_lab_line(entry) for entry in listed) + "."
    if more:
        reason += f" {more} more in the Labs section."
    links = []
    for entry in listed:
        for link in entry.get("source_links") or []:
            if link not in links:
                links.append(link)
    count = len(entries)
    item = _summary_item("LabResultGroup", "labs", {
        "count": count,
        "results": [
            {"evidence_item_id": entry["id"], **{key: entry["display"][key] for key in ("label", "value", "unit", "flag", "reference", "date")}}
            for entry in entries
        ],
        "window_start": window_start,
    }, {
        "label": "Abnormal lab results",
        "value": f"{count} abnormal result{'s' if count != 1 else ''}",
        "date": max((entry["display"]["date"] for entry in entries if entry["display"]["date"]), default=None),
        "detail": None,
    }, source_links=links)
    return item, reason


def build_brief(
    mode: str,
    days: int,
    *,
    end: datetime | None = None,
) -> dict[str, Any]:
    if mode not in MODE_CONFIG:
        mode = "clinician"
    config = MODE_CONFIG[mode]
    end = end or datetime.now(timezone.utc)
    start = end - timedelta(days=max(7, min(int(days), 365)))
    tz = _timezone()

    raw: dict[str, list[dict[str, Any]]] = {}
    seen: set[str] = set()
    bundles = []
    contradictions: dict[str, dict[str, Any]] = {}
    reassuring_entries, opposing_entries = [], []
    absence_caveat = None
    truncated_scopes = []
    for name, visible, query in _scope_queries(config, start, end):
        bundle = build_bundle(query)
        bundles.append((name, bundle))
        if (bundle.get("budget") or {}).get("truncated") and name not in {"signals", "analytics"}:
            truncated_scopes.append(name)
        for item in _items(bundle):
            if item.get("entity_type") not in visible or item["id"] in seen:
                continue
            seen.add(item["id"])
            raw.setdefault(item["entity_type"], []).append(item)
        for contradiction in bundle["contradictions"]:
            contradictions.setdefault(contradiction["id"], contradiction)
        reassuring_entries.extend(bundle["evidence"]["reassuring_evidence"])
        opposing_entries.extend(bundle["evidence"]["opposing_evidence"])
        for caveat in bundle["missing_data_caveats"]:
            if caveat.get("code") == "absence_is_not_proof":
                absence_caveat = caveat

    # Per-domain bundle caveats count only each scope's restricted types, so the
    # brief states missing sources per section instead.
    limitations: list[dict[str, Any]] = [absence_caveat] if absence_caveat else []
    for name in truncated_scopes:
        limitations.append({
            "code": "scope_truncated",
            "domain": name,
            "message": f"More {name.replace('_', ' ')} records exist than one brief lists; the most recent {MAX_ITEM_BUDGET} were considered.",
        })

    def take(entity_type: str) -> list[dict[str, Any]]:
        return [item for item in raw.get(entity_type, []) if _specialty_relevant(item, config)]

    # Documents: clinical document types only; insurance is a privacy exclusion.
    omitted_documents = {"insurance": 0, "non_clinical": 0}
    excluded_records: set[str] = set()
    documents = []
    for item in raw.get("MedicalRecord", []):
        reason = document_exclusion(item.get("data") or {})
        if reason:
            omitted_documents[reason] += 1
            if reason == "insurance":
                excluded_records.add(str(item.get("entity_id")))
            continue
        record_day = _day((item.get("data") or {}).get("record_date"))
        if record_day and record_day < start.date().isoformat():
            continue
        if _specialty_relevant(item, config):
            documents.append(item)
    documents.sort(key=lambda entry: (str(entry.get("observed_at") or ""), entry["id"]), reverse=True)

    latest_labs, abnormal_labs = _labs(raw.get("LabResult", []), start, excluded_records, config)
    if not latest_labs:
        limitations.append({
            "code": "labs_missing",
            "domain": "labs",
            "message": f"No lab results were recorded in the past {max(LAB_LOOKBACK_DAYS, days)} days.",
        })

    diagnoses, tentative = [], []
    for item in take("Diagnosis"):
        (tentative if _is_tentative_diagnosis(item.get("data") or {}) else diagnoses).append(item)
    diagnoses.sort(key=lambda entry: (
        str((entry.get("data") or {}).get("status") or "active") != "active",
        str((entry.get("data") or {}).get("name") or "").lower(),
    ))
    medications = [
        item for item in take("Medication")
        if str((item.get("data") or {}).get("status") or "active").lower() != "stopped"
    ]
    stopped = len(take("Medication")) - len(medications)
    medications.sort(key=lambda entry: (
        str((entry.get("data") or {}).get("kind") or "medication") == "supplement",
        str((entry.get("data") or {}).get("name") or "").lower(),
    ))
    if stopped:
        limitations.append({
            "code": "stopped_medications_omitted",
            "domain": "clinical",
            "message": f"{stopped} medication(s) recorded as stopped are not listed.",
        })
    allergies = take("Allergy")
    profile = take("HealthProfile")[:1]

    window_day = start.date().isoformat()
    history = [
        item for item in [*take("HistoryEntry"), *take("SymptomLog"), *take("HealthEpisode"), *take("MedicationExposure")]
        if not _day(item.get("observed_at")) or _day(item.get("observed_at")) >= window_day
    ]
    history.sort(key=lambda entry: (str(entry.get("observed_at") or ""), entry["id"]), reverse=True)

    weight = _weight_trend(take("WeightLog"), start, end)

    # Calculated summaries replace raw high-volume rows.
    glucose_summary = insulin_summary = wearable_summary = cycle_summary = None
    glucose_rows: list[dict[str, Any]] = []
    if "GlucoseReading" in config["types"]:
        glucose_rows = _source_rows("GlucoseReading", "timestamp", start, end)
        fingersticks = len(_source_rows("FingerstickReading", "timestamp", start, end)) if "FingerstickReading" in config["types"] else 0
        glucose_summary = _glucose_summary(glucose_rows, tz, start, end, fingersticks)
        if glucose_summary is None:
            limitations.append({"code": "cgm_missing", "domain": "glucose", "message": "No CGM readings were recorded in this window."})
    if "Treatment" in config["types"]:
        insulin_summary = _insulin_summary(_source_rows("Treatment", "timestamp", start, end), glucose_rows, tz, start, end)
        if insulin_summary is None:
            limitations.append({"code": "pump_missing", "domain": "insulin", "message": "No insulin or carbohydrate records were recorded in this window."})
    insight_items = [*take("Pattern"), *take("Insight")]
    spo2_unreliable = False
    if {"OuraDaily", "FitbitDaily"} & config["types"] or any(
        _SPO2_TEXT.search(json.dumps(item.get("data") or {}, default=str)) for item in insight_items
    ):
        oura = _source_rows("OuraDaily", "date", start, end, dated=True)
        fitbit = _source_rows("FitbitDaily", "date", start, end, dated=True)
        wearable_summary = _wearable_summary(oura, fitbit, start, end)
        spo2_unreliable = bool(wearable_summary and wearable_summary["data"]["spo2_implausible_days"])
        if not {"OuraDaily", "FitbitDaily"} & config["types"]:
            wearable_summary = None
    if "PeriodLog" in config["types"]:
        cycle_summary = _cycle_summary(_source_rows("PeriodLog", "date", start, end, dated=True), start, end)

    patterns = _patterns(
        [*insight_items, *take("ActivityPositionEffect")], spo2_unreliable, limitations
    )
    burden_items = take("ManagementBurdenSummary")
    narrative = [*take("DailySummary"), *take("WeeklySummary"), *take("HealthSummary")]

    public_cache: dict[str, dict[str, Any]] = {}

    def public(item: dict[str, Any]) -> dict[str, Any]:
        if item["id"] not in public_cache:
            public_cache[item["id"]] = _public_item(item)
        return public_cache[item["id"]]

    def listing(items: list[dict[str, Any] | None]) -> list[dict[str, Any]]:
        return [public(item) for item in items if item]

    # Concerns: explicit rules over the selected evidence, ranked. Weight
    # loss, hypoglycemia and reported symptoms lead; abnormal labs follow as
    # one grouped entry so a long panel cannot bury them.
    concerns = []
    if weight and weight["data"]["loss_from_peak_pct"] >= WEIGHT_LOSS_CONCERN_PCT:
        entry = public(weight)
        concerns.append(_concern(entry, "weight_loss", (
            f"Latest {entry['display']['value']} on {weight['data']['latest_date']}. {entry['display']['detail']} "
            f"Concern threshold: {WEIGHT_LOSS_CONCERN_PCT:g}% loss within {WEIGHT_LOOKBACK_DAYS} days."
        )))
    if glucose_summary:
        metrics = glucose_summary["data"]
        if metrics["tbr70_pct"] > TBR70_TARGET_PCT or metrics["tbr54_pct"] > TBR54_TARGET_PCT:
            concerns.append(_concern(public(glucose_summary), "hypoglycemia", (
                f"Time below 70 mg/dL {metrics['tbr70_pct']}% (target <{TBR70_TARGET_PCT:g}%), "
                f"below 54 mg/dL {metrics['tbr54_pct']}% (target <{TBR54_TARGET_PCT:g}%); "
                f"{metrics['episodes']} low episodes ≥15 min, {metrics['nocturnal_episodes']} overnight, "
                f"{metrics['level2_episodes']} below 54."
            )))
    symptom_concerns = []
    for item in history:  # newest first
        data = item.get("data") or {}
        if item["entity_type"] == "HistoryEntry" and _REPORTED_SYMPTOMS.search(str(data.get("title") or "")):
            entry = public(item)
            symptom_concerns.append(_concern(entry, "reported_symptoms", (
                f"{entry['display']['label']} ({entry['display']['date'] or 'undated'})."
                + (f" {entry['display']['detail']}" if entry["display"]["detail"] else "")
            )))
        elif item["entity_type"] == "SymptomLog" and str(data.get("severity") or "").lower() in {"moderate", "severe"}:
            entry = public(item)
            symptom_concerns.append(_concern(entry, "reported_symptoms", (
                f"{entry['display']['label']} — {data.get('severity')} ({entry['display']['date'] or 'undated'})."
            )))
    concerns.extend(symptom_concerns[:MAX_SYMPTOM_CONCERNS])
    if len(symptom_concerns) > MAX_SYMPTOM_CONCERNS:
        limitations.append({
            "code": "symptom_concerns_capped",
            "domain": "clinical",
            "message": (
                f"{len(symptom_concerns) - MAX_SYMPTOM_CONCERNS} older reported symptom(s) are listed under "
                "Symptoms & history rather than as concerns."
            ),
        })
    if abnormal_labs:
        group, reason = _lab_group([public(item) for item in abnormal_labs], window_day)
        concerns.append(_concern(public(group), "abnormal_labs", reason))

    hypotheses = _relevant_hypotheses(config)
    if not config.get("omit_unconfirmed_hypotheses"):
        for item in tentative:
            data = item.get("data") or {}
            hypotheses.append(_hypothesis_labels({
                "id": f"diagnosis-tentative:{item['entity_id']}",
                "title": data.get("name") or "Tentative diagnosis",
                "description": data.get("notes") or "",
                "origin_label": "Diagnosis entry worded as tentative",
                "status": "proposed",
                "evidence_state": "not_evaluated",
                "source_links": item.get("source_links") or [],
            }))
    elif tentative:
        limitations.append({
            "code": "unconfirmed_conditions_omitted",
            "domain": "clinical",
            "message": f"{len(tentative)} tentatively worded condition(s) are not listed in primary-care mode.",
        })

    sections_items = {
        "problems": listing(diagnoses),
        "medications": listing(medications),
        "allergies": listing(allergies),
        "profile": listing(profile),
        "weight": listing([weight]),
        "symptoms_history": listing(history),
        "objective_patterns": listing(patterns),
        "glucose_insulin": listing([glucose_summary, insulin_summary]),
        "wearables_cycle": listing([wearable_summary, cycle_summary]),
        "management_burden": listing(burden_items),
        "labs": listing(latest_labs),
        "documents": listing(documents),
        "narrative_summaries": listing(narrative),
    }
    public_items = list(public_cache.values())
    by_id = {item["id"]: item for item in public_items}

    # Every reassuring/opposing entry and limitation carries a display block
    # like any other brief item. A lab whose source label its own reference
    # range contradicts is evidence of neither.
    contradicted_labels = 0

    def stance_entry(evidence: dict[str, Any], reason: Any) -> dict[str, Any]:
        display = {key: evidence["display"].get(key) for key in ("label", "value", "unit", "flag", "reference", "date")}
        return {
            "id": evidence["id"],
            "evidence": evidence,
            "reason": reason,
            "display": {**display, "status": None, "detail": reason or None},
        }

    reassuring, seen_reassuring = [], set()
    for entry in reassuring_entries:
        evidence_id = entry.get("evidence_item_id")
        if evidence_id in by_id and evidence_id not in seen_reassuring:
            seen_reassuring.add(evidence_id)
            if by_id[evidence_id]["display"].get("flag_note"):
                contradicted_labels += 1
                continue
            reassuring.append(stance_entry(by_id[evidence_id], entry.get("reason")))
    opposing, seen_opposing = [], set()
    for entry in opposing_entries:
        evidence_id = entry.get("evidence_item_id")
        key = evidence_id or entry.get("contradiction_id")
        if key in seen_opposing:
            continue
        if evidence_id:
            if evidence_id not in by_id:
                continue
            seen_opposing.add(key)
            if by_id[evidence_id]["display"].get("flag_note"):
                contradicted_labels += 1
                continue
            opposing.append(stance_entry(by_id[evidence_id], entry.get("reason")))
        else:
            seen_opposing.add(key)
            contradiction = contradictions.get(entry.get("contradiction_id")) or {}
            severity = str(contradiction.get("severity") or "")
            sides = " vs ".join(side for side in (_side(entry.get("left")), _side(entry.get("right"))) if side)
            opposing.append({
                **entry,
                "id": entry.get("contradiction_id"),
                "display": {
                    "label": f"Unresolved {severity} contradiction" if severity else "Unresolved contradiction",
                    "value": sides or None, "unit": None, "flag": None, "reference": None, "date": None,
                    "status": contradiction.get("domain") or None,
                    "detail": entry.get("reason") or None,
                },
            })
    if contradicted_labels:
        limitations.append({
            "code": "source_flag_contradicted",
            "domain": "labs",
            "message": (
                f"{contradicted_labels} lab result(s) carry a source flag their own reference range contradicts; "
                "they are shown with the range-derived flag and not counted as reassuring or opposing evidence."
            ),
        })

    seen_messages = {item["message"] for item in limitations}
    for item in public_items:
        for limitation in (item["data"].get("limitations") or []):
            message = str(limitation)
            if message in seen_messages:
                continue
            seen_messages.add(message)
            limitations.append({"code": "item_limitation", "domain": item["domain"], "message": message, "evidence_item_id": item["id"]})

    def limitation_display(limitation: dict[str, Any]) -> dict[str, Any]:
        domain = str(limitation.get("domain") or "")
        return {
            "label": _DOMAIN_LABELS.get(domain) or (_words(domain).replace("_", " ") if domain else "Limitation"),
            "value": None, "unit": None, "flag": None, "reference": None, "date": None,
            "status": str(limitation.get("code") or "").replace("_", " ") or None,
            "detail": limitation.get("message") or None,
        }

    limitations = [{**limitation, "display": limitation_display(limitation)} for limitation in limitations]

    appendix = [
        {
            "id": item["id"],
            "entity_type": item["entity_type"],
            "entity_id": item["entity_id"],
            "title": item["title"],
            "display": item["display"],
            "observed_at": item["observed_at"],
            "evidence_strength": item["evidence_strength"],
            "source_links": item["source_links"],
        }
        for item in public_items
    ]
    bundle_refs = [
        {
            "scope": name,
            "id": bundle["bundle_id"],
            "data_version": bundle["data_version"]["input_hash"],
            "query": bundle["query"],
        }
        for name, bundle in bundles
    ]
    identity = {
        "version": BRIEF_VERSION,
        "mode": mode,
        "bundles": [(ref["scope"], ref["id"], ref["data_version"]) for ref in bundle_refs],
        "selected_ids": sorted(item["id"] for item in appendix),
        "hypothesis_ids": [item["id"] for item in hypotheses],
    }
    composite = _checksum([(ref["scope"], ref["id"]) for ref in bundle_refs]).removeprefix("sha256:")
    first = bundles[0][1] if bundles else {"bundle_version": None, "data_version": {}}
    return {
        "brief_id": "urn:glucopilot:clinician-brief:" + _checksum(identity).removeprefix("sha256:"),
        "brief_version": BRIEF_VERSION,
        "mode": mode,
        "mode_label": config["label"],
        "generated_at": end.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "window": {"start": start.isoformat().replace("+00:00", "Z"), "end": end.isoformat().replace("+00:00", "Z"), "days": days},
        "evidence_bundle": {
            "id": "urn:glucopilot:evidence-bundle-set:" + composite,
            "version": first["bundle_version"],
            "data_version": {
                **first["data_version"],
                "input_hash": _checksum([ref["data_version"] for ref in bundle_refs]),
            },
            "query": {"scopes": [ref["query"] for ref in bundle_refs]},
            "scopes": bundle_refs,
        },
        "sections": {
            "concerns": concerns,
            **sections_items,
            "reassuring_evidence": reassuring,
            "opposing_evidence": opposing,
            "contradictions": list(contradictions.values()),
            "limitations": limitations,
            "hypotheses": hypotheses,
            "questions": list(config["questions"]),
        },
        "appendix": appendix,
        "privacy": {
            "policy": "specialty_minimum_necessary/1.1.0",
            "selected_entity_types": sorted({item["entity_type"] for item in public_items}),
            "always_omitted_entity_types": ["InsuranceInfo"],
            "omitted_documents": omitted_documents,
            "note": (
                "Only specialty-allowlisted evidence is included. Insurance details and insurance "
                "documents, non-clinical uploads, and other PHI are omitted."
            ),
        },
        "language": {
            "hypotheses": "Unconfirmed hypotheses are tentative and are not diagnoses.",
            "associations": "Observed associations and calculations do not establish causation.",
            "clinical": "This brief supports clinician review and does not recommend treatment.",
        },
    }


@router.post("/api/briefs/clinician")
def clinician_brief(body: BriefRequest):
    try:
        return build_brief(body.mode, body.days)
    except EvidenceBundleError as error:
        raise HTTPException(status_code=413, detail=str(error)) from error
