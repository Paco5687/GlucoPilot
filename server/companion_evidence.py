"""Bounded Evidence Bundle grounding and claim links for Companion replies."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from . import analysis_evidence, companion_safety, correction_response
from .contradictions import refresh_current as refresh_contradictions
from .diagnostics import reasoning_context as diagnostic_context
from .evidence_bundle import EvidenceBundleQuery, EvidenceDomain, build_bundle
from .clinical_reviews import companion_context as clinical_review_context


CONTRACT_VERSION = "companion-evidence-context/1.0.0"
MAX_PROMPT_CONTEXT_CHARS = 48_000
MAX_INTENT_CHARS = 500  # EvidenceBundleQuery.question_intent max_length
_CITATION_RE = re.compile(r"\[([EMGC])(\d+)\]", re.IGNORECASE)
# The local model often cites as "(E3)", "(M2, M5)" or "[ E3 ]" instead of the
# exact "[E3]" it was asked for. A bracket or paren group made only of aliases is
# unambiguous, so it is rewritten to canonical form before validation. Ranges
# ("E3–E9") and prose that merely contains an alias are left alone and still
# have to pass the citation check on their own.
_ALIAS_GROUP_RE = re.compile(
    r"[\(\[]\s*([EMGC]\d+(?:(?:\s*[,/;&]\s*|\s+and\s+|\s+)[EMGC]\d+)*)\s*[\)\]]",
    re.IGNORECASE,
)
_ALIAS_IN_GROUP_RE = re.compile(r"[EMGC]\d+", re.IGNORECASE)
# "estradiol (E2)" names the hormone, not evidence item E2.
_ALIAS_LOOKALIKE_RE = re.compile(
    r"\b(?:estradiol|estrone|estriol|estetrol|complement)\s*$", re.IGNORECASE
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_NORMAL_FLAGS = {"", "n", "normal", "none"}
_EXCLUDED_TYPES = {"DailySummary", "HealthSummary", "InsuranceInfo", "WeeklySummary"}
_REASONING_FIELDS = {
    "allergen",
    "analytics_confidence",
    "assertion_status",
    "category",
    "collected_date",
    "date",
    "details",
    "diagnosed_date",
    "doc_type",
    "dose",
    "entry_date",
    "explanation",
    "findings",
    "flag",
    "flag_note",
    "frequency",
    "kind",
    "language",
    "metric",
    "name",
    "narrative",
    "notes",
    "pattern_type",
    "record_date",
    "reference_high",
    "reference_low",
    "severity",
    "source_page",
    "status",
    "state",
    "sample_count",
    "summary",
    "supporting_data",
    "symptom",
    "test_name",
    "title",
    "type",
    "unit",
    "validation_status",
    "value",
    "verification_status",
    "window",
}


@dataclass(frozen=True)
class Scope:
    name: str
    domains: tuple[EvidenceDomain, ...]
    days: int
    budget: int
    retrieval_budget: int
    keywords: frozenset[str]


SCOPES = (
    Scope(
        "metabolic",
        (EvidenceDomain.GLUCOSE, EvidenceDomain.INSULIN),
        30,
        16,
        16,
        frozenset({
            "basal", "bedtime", "bg", "bolus", "breakfast", "carb", "cgm", "correction",
            "dexcom", "diabetes", "dinner", "eat", "eating", "fingerstick", "food",
            "glooko", "glucose", "glycemic", "highs", "hypo", "hypoglycemia", "insulin",
            "isf", "ketone", "lows", "lunch", "meal", "omnipod", "pump", "ratio", "spike",
            "sugar", "tdd", "tir",
        }),
    ),
    Scope(
        "wellness",
        (EvidenceDomain.WEARABLES, EvidenceDomain.CYCLE),
        14,
        12,
        12,
        frozenset({
            "activity", "bedtime", "cycle", "energy", "exercise", "exhausted", "fatigue",
            "fitbit", "foggy", "heart", "hormone", "hrv", "menstrual", "nap", "oura",
            "period", "position", "sit", "sitting", "sleep", "spo2", "standing", "steps",
            "tired", "walk", "walking", "workout",
        }),
    ),
    Scope(
        "analytics",
        (EvidenceDomain.ANALYTICS,),
        365,
        12,
        12,
        frozenset({
            "association", "change", "compare", "correlation", "insight", "pattern",
            "relationship", "trend", "week", "month",
        }),
    ),
    Scope(
        "clinical",
        (EvidenceDomain.CLINICAL,),
        3650,
        12,
        12,
        frozenset({
            "allergy", "armor", "armour", "condition", "diagnosed", "diagnosis", "disease",
            "dose", "history", "med", "medication", "medicine", "profile", "rash", "skin",
            "supplement", "symptom", "treatment", "weight",
        }),
    ),
    Scope(
        "labs_records",
        (EvidenceDomain.LABS, EvidenceDomain.RECORDS),
        3650,
        24,
        150,
        frozenset({
            "a1c", "acth", "antibody", "armor", "armour", "b12", "cholesterol", "cortisol",
            "enzyme", "estradiol", "ferritin", "fsh", "imaging", "inflammation", "iron",
            "lab", "level", "liver", "marker", "mri", "panel", "prolactin", "record",
            "report", "result", "scan", "t3", "t4", "test", "thyroid", "tsh", "urine",
            "vitamin",
        }),
    ),
)


class CompanionEvidenceError(RuntimeError):
    """Raised instead of silently dropping protected safety context."""


def _checksum(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _window(days: int, as_of: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(as_of - timedelta(days=days - 1), time.min, tzinfo=timezone.utc),
        datetime.combine(as_of, time.max, tzinfo=timezone.utc),
    )


# The latest result of a few tests the model otherwise claims "we don't have".
# Ranked retrieval can miss them when the question names no lab (an autonomic
# question still turns on thyroid status), so they ride along with the labs scope.
KEY_LABS = Scope("key_labs", (EvidenceDomain.LABS,), 3650, 12, 60, frozenset())
KEY_LAB_INTENT = "TSH free T4 free T3 hemoglobin A1c ferritin vitamin D"
_KEY_LAB_TESTS = (
    ("tsh", re.compile(r"^tsh\b|thyroid[- ]stimulating", re.IGNORECASE)),
    ("free_t4", re.compile(r"free\s*t4|t4,?\s*free|\bft4\b|free\s+thyroxine", re.IGNORECASE)),
    ("free_t3", re.compile(r"free\s*t3|t3,?\s*free|\bft3\b|triiodothyronine.*free", re.IGNORECASE)),
    ("a1c", re.compile(r"a1c|glycated|glycohemoglobin", re.IGNORECASE)),
    ("ferritin", re.compile(r"ferritin", re.IGNORECASE)),
    ("vitamin_d", re.compile(r"vitamin\s*d\b(?!.*,\s*d[23]\b)|25-?hydroxyvitamin", re.IGNORECASE)),
)
# Same-draw duplicates are already collapsed (lab_dedupe); two rows of one test
# on one day are two different results, so both are shown.
_KEY_LAB_ROWS_PER_TEST = 2


def _key_lab_items(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    latest: dict[str, list[dict[str, Any]]] = {}
    for item in _bundle_items(bundle):
        if item.get("entity_type") != "LabResult":
            continue
        name = str((item.get("data") or {}).get("test_name") or "")
        day = str(item.get("observed_at") or "")[:10]
        for key, pattern in _KEY_LAB_TESTS:
            if not pattern.search(name):
                continue
            rows = latest.get(key) or []
            current = str(rows[0].get("observed_at") or "")[:10] if rows else ""
            if day > current:
                latest[key] = [item]
            elif day == current and len(rows) < _KEY_LAB_ROWS_PER_TEST:
                rows.append(item)
            break
    return [item for key, _pattern in _KEY_LAB_TESTS for item in latest.get(key, [])]


# Precomputed analyses of the questions she asks most: does glucose climb while
# she sits, does a temp basal really need two hours, does a late or high
# bedtime cost her sleep. A matching question gets the analysis itself, over
# months of data, instead of the ~16 raw readings the model would otherwise
# improvise from. They lead the evidence list so they take the first aliases.
ANALYSES = Scope("analyses", (EvidenceDomain.GLUCOSE,), 120, 3, 3, frozenset())
_ANALYSIS_TRIGGERS = (
    (
        analysis_evidence.WALKING,
        re.compile(
            r"\b(sit|sits|sitting|sat|seated|desk|couch|sofa|walk\w*|stroll\w*|steps?|"
            r"standing|movement|exercis\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        analysis_evidence.CORRECTION,
        re.compile(
            r"\b(temp(?:orary)?\s*basals?|temps|corrections?|correcting|delay\w*|kicks?\s+in|"
            r"start(?:s|ed)?\s+working|takes?\s+(?:so\s+)?long|(?:2|two)[-\s]?(?:hours?|hrs?))\b",
            re.IGNORECASE,
        ),
    ),
    (
        analysis_evidence.BEDTIME,
        re.compile(
            r"\b(bed|bedtime|sleep\w*|slept|asleep|insomnia|nights?|overnight|tonight|dinner|"
            r"supper|evening)\b",
            re.IGNORECASE,
        ),
    ),
)


def _selected_analyses(question: str) -> tuple[str, ...]:
    return tuple(
        sorted(entity_type for entity_type, pattern in _ANALYSIS_TRIGGERS if pattern.search(question))
    )


def _selected_scopes(question: str) -> tuple[Scope, ...]:
    lower = question.lower()
    tokens = set(_TOKEN_RE.findall(lower))
    # "sugars", "enzymes", "levels": match the singular keyword too.
    tokens |= {token[:-1] for token in tokens if len(token) > 3 and token.endswith("s")}
    selected = [scope for scope in SCOPES if tokens & scope.keywords]
    if any(phrase in lower for phrase in ("time in range", "blood sugar")):
        metabolic = next(scope for scope in SCOPES if scope.name == "metabolic")
        if metabolic not in selected:
            selected.append(metabolic)
    if not selected:
        return SCOPES
    if any(token in tokens for token in {"association", "change", "compare", "correlation", "pattern", "trend"}):
        analytics = next(scope for scope in SCOPES if scope.name == "analytics")
        if analytics not in selected:
            selected.append(analytics)
    return tuple(scope for scope in SCOPES if scope in selected)


def _scope_intent(scope: Scope, question: str) -> str:
    """Append the scope's retrieval hint, trimming the question so the result
    stays within EvidenceBundleQuery's question_intent limit."""
    hints = {
        "metabolic": "glucose insulin treatment",
        "wellness": "wearable daily cycle period",
        "analytics": "pattern insight analytics",
        "clinical": "diagnosis medication allergy symptom history profile",
        "labs_records": "lab result",
    }
    lower = question.lower()
    if scope.name == "metabolic":
        if any(
            token in lower
            for token in ("basal", "bolus", "insulin", "pump", "tdd", "treatment")
        ):
            hints["metabolic"] = "insulin treatment bolus basal pump"
        else:
            hints["metabolic"] = "glucose reading fingerstick cgm"
    if scope.name == "labs_records" and any(
        token in lower for token in ("document", "imaging", "record", "report")
    ):
        hints["labs_records"] = "medical record source document"
    hint = hints[scope.name]
    return f"{question[:MAX_INTENT_CHARS - len(hint) - 1].rstrip()} {hint}"


def _bundle_items(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Ranked items, with each blood draw once: a second report of the same
    draw is represented by the kept row, which links both documents."""
    evidence = bundle["evidence"]
    return [
        item
        for item in (
            *evidence["derived_metrics"],
            *evidence["documents"],
            *evidence["direct_observations"],
            *evidence["relationships"],
        )
        if not (item.get("entity_type") == "LabResult" and (item.get("data") or {}).get("duplicate_of"))
    ]


def _scope_items(
    scope: Scope,
    bundle: dict[str, Any],
    question: str,
) -> list[dict[str, Any]]:
    items = [
        item for item in _bundle_items(bundle)
        if item.get("entity_type") not in _EXCLUDED_TYPES
        and (
            item.get("entity_type") != "ActivityPositionEffect"
            or bool((item.get("data") or {}).get("qualifies_for_companion"))
        )
    ]
    if scope.name != "labs_records":
        return items[:scope.budget]
    record_focused = any(
        token in question.lower()
        for token in ("document", "imaging", "record", "report")
    )
    lab_limit, record_limit = (8, 16) if record_focused else (16, 8)
    labs = [item for item in items if item.get("entity_type") == "LabResult"][:lab_limit]
    records = [
        item for item in items
        if item.get("entity_type") == "MedicalRecord"
    ][:record_limit]
    other = [
        item for item in items
        if item.get("entity_type") not in {"LabResult", "MedicalRecord"}
    ]
    primary, secondary = (records, labs) if record_focused else (labs, records)
    balanced: list[dict[str, Any]] = []
    primary_index = secondary_index = 0
    while primary_index < len(primary) or secondary_index < len(secondary):
        for _ in range(2):
            if primary_index < len(primary):
                balanced.append(primary[primary_index])
                primary_index += 1
        if secondary_index < len(secondary):
            balanced.append(secondary[secondary_index])
            secondary_index += 1
    balanced.extend(item for item in other if item not in balanced)
    return balanced[:scope.budget]


def _bounded(value: Any, *, depth: int = 0) -> Any:
    if depth >= 5:
        return "[nested value omitted]"
    if isinstance(value, dict):
        return {
            str(key): _bounded(item, depth=depth + 1)
            for key, item in list(sorted(value.items(), key=lambda pair: str(pair[0])))[:30]
        }
    if isinstance(value, (list, tuple)):
        return [_bounded(item, depth=depth + 1) for item in value[:12]]
    if isinstance(value, str):
        return value[:700]
    return value


def _range_text(low: Any, high: Any) -> str:
    if low not in (None, "") and high not in (None, ""):
        return f"{low}-{high}"
    if low not in (None, ""):
        return f"above {low}"
    if high not in (None, ""):
        return f"below {high}"
    return "not stated"


def _nonstandard_flag(item: dict[str, Any]) -> str | None:
    """The "optimal range" judgement attached to a lab row: its own, when the
    row comes from such a report, or a same-draw twin's."""
    if item.get("entity_type") != "LabResult":
        return None
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    if data.get("reference_basis") == "nonstandard":
        flag = str(data.get("flag") or "").strip().lower()
        return None if flag in _NORMAL_FLAGS else flag
    for twin in data.get("nonstandard_flags") or []:
        flag = str(twin.get("flag") or "").strip().lower()
        if flag not in _NORMAL_FLAGS:
            return flag
    return None


def _flag_note(data: dict[str, Any]) -> str | None:
    if data.get("reference_basis") == "nonstandard":
        flag = str(data.get("flag") or "").strip().lower()
        judged = f"rates this {flag}" if flag not in _NORMAL_FLAGS else "does not flag this"
        return (
            "From a functional-medicine report that uses narrower 'optimal' ranges, not a lab reference "
            f"range: it {judged} against its optimal range "
            f"({_range_text(data.get('reference_low'), data.get('reference_high'))}). "
            "This is not a lab high/low flag."
        )
    twins = [
        twin for twin in data.get("nonstandard_flags") or []
        if str(twin.get("flag") or "").strip().lower() not in _NORMAL_FLAGS
    ]
    if twins and str(data.get("flag") or "").strip().lower() in _NORMAL_FLAGS:
        twin = twins[0]
        return (
            "A functional-medicine report of this same draw rates it "
            f"{str(twin.get('flag')).lower()} against its narrower 'optimal' range "
            f"({_range_text(twin.get('reference_low'), twin.get('reference_high'))}); "
            "the lab's own reference range did not flag it."
        )
    return None


def _reasoning_data(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    output = {
        key: _bounded(data[key])
        for key in sorted(_REASONING_FIELDS)
        if key in data
    }
    if item.get("entity_type") == "LabResult":
        if data.get("reference_basis") == "nonstandard":
            # The optimal range and its flag move into the note, so the model
            # cannot read them as the lab's reference range or flag.
            for key in ("flag", "reference_low", "reference_high"):
                output.pop(key, None)
        if note := _flag_note(data):
            output["flag_note"] = note
    return output


def _source_ids(item: dict[str, Any]) -> list[str]:
    values = [f"entity:{item.get('entity_type')}:{item.get('entity_id')}"]
    for reference in item.get("provenance") or []:
        for key in ("source_record_ref", "source_file_ref", "sync_run_ref"):
            value = reference.get(key)
            if value:
                values.append(f"{key}:{value}")
    return list(dict.fromkeys(values))


def _public_item(alias: str, item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    public = {
        "alias": alias,
        "id": item["id"],
        "domain": item.get("domain"),
        "entity_type": item.get("entity_type"),
        "entity_id": item.get("entity_id"),
        "observed_at": item.get("observed_at"),
        "title": (
            data.get("title")
            or data.get("test_name")
            or data.get("name")
            or item.get("entity_type")
        ),
        "confidence": item.get("confidence") or {},
        "source_ids": _source_ids(item),
        "source_links": item.get("source_links") or [],
        "claim": item.get("claim"),
        "flag": (
            str(data.get("flag") or "").strip().lower() or None
            if item.get("entity_type") == "LabResult"
            and data.get("reference_basis") != "nonstandard"
            else None
        ),
        "nonstandard_flag": _nonstandard_flag(item),
    }
    if item.get("entity_type") in analysis_evidence.ENTITY_TYPES:
        # What the model was shown, so a reply that restates an analysis can be
        # held to its numbers.
        public["analysis"] = {
            key: _bounded(data[key])
            for key in ("summary", "findings", "supporting_data")
            if key in data
        }
    return public


def _reasoning_item(alias: str, item: dict[str, Any]) -> dict[str, Any]:
    public = _public_item(alias, item)
    public.pop("analysis", None)  # the same values travel in data
    return {
        **public,
        "data": _reasoning_data(item),
    }


def _compact_reasoning(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in items:
        candidate = [*compact, item]
        if len(json.dumps(candidate, separators=(",", ":"), default=str)) > MAX_PROMPT_CONTEXT_CHARS:
            break
        compact.append(item)
    return compact


def build_context(
    question: str,
    *,
    as_of: date | None = None,
    refresh: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a content-addressed, question-ranked portfolio for one reply."""
    intent = " ".join(str(question or "").split())[:MAX_INTENT_CHARS] or "whole person health context"
    as_of = as_of or datetime.now(timezone.utc).date()
    if refresh:
        refresh_contradictions()
    bundles: list[tuple[Scope, dict[str, Any]]] = []
    items_by_id: dict[str, dict[str, Any]] = {}
    items_by_scope: dict[str, list[dict[str, Any]]] = {}
    contradictions_by_id: dict[str, dict[str, Any]] = {}
    caveats: dict[tuple[str, str, str], dict[str, Any]] = {}
    opposing: list[dict[str, Any]] = []
    for scope in _selected_scopes(intent):
        start, end = _window(scope.days, as_of)
        bundle = build_bundle(EvidenceBundleQuery(
            start=start,
            end=end,
            domains=scope.domains,
            question_intent=_scope_intent(scope, intent),
            item_budget=scope.retrieval_budget,
        ))
        bundles.append((scope, bundle))
        items_by_scope[scope.name] = _scope_items(scope, bundle, intent)
        for item in items_by_scope[scope.name]:
            items_by_id.setdefault(item["id"], item)
        for contradiction in bundle["contradictions"]:
            contradictions_by_id[contradiction["id"]] = contradiction
        for caveat in bundle["missing_data_caveats"]:
            caveats[(caveat["code"], caveat["domain"], caveat["message"])] = caveat
        opposing.extend(bundle["evidence"]["opposing_evidence"])
    if analyses := _selected_analyses(intent):
        start, end = _window(ANALYSES.days, as_of)
        analysis_bundle = build_bundle(EvidenceBundleQuery(
            start=start,
            end=end,
            domains=ANALYSES.domains,
            question_intent=intent,
            item_budget=ANALYSES.retrieval_budget,
            normalized_entity_types=analyses,
        ))
        bundles.append((ANALYSES, analysis_bundle))
        items_by_scope[ANALYSES.name] = [
            item for item in _bundle_items(analysis_bundle)
            if item.get("entity_type") in analysis_evidence.ENTITY_TYPES
        ][:ANALYSES.budget]
        for item in items_by_scope[ANALYSES.name]:
            items_by_id.setdefault(item["id"], item)
        # The analyses rest on glucose rows, so unresolved glucose contradictions
        # travel with them even when no metabolic scope was selected.
        for contradiction in analysis_bundle["contradictions"]:
            contradictions_by_id[contradiction["id"]] = contradiction
    if any(scope.name == "labs_records" for scope, _bundle in bundles):
        start, end = _window(KEY_LABS.days, as_of)
        key_bundle = build_bundle(EvidenceBundleQuery(
            start=start,
            end=end,
            domains=KEY_LABS.domains,
            question_intent=KEY_LAB_INTENT,
            item_budget=KEY_LABS.retrieval_budget,
        ))
        bundles.append((KEY_LABS, key_bundle))
        items_by_scope[KEY_LABS.name] = _key_lab_items(key_bundle)[:KEY_LABS.budget]
        for item in items_by_scope[KEY_LABS.name]:
            items_by_id.setdefault(item["id"], item)

    raw_items = list(items_by_scope.get(ANALYSES.name, []))
    retained_raw_ids: set[str] = {item["id"] for item in raw_items}
    max_scope_items = max((len(items) for items in items_by_scope.values()), default=0)
    for index in range(max_scope_items):
        for scope, _bundle in bundles:
            scoped = items_by_scope[scope.name]
            if index >= len(scoped):
                continue
            item = scoped[index]
            if item["id"] not in retained_raw_ids:
                raw_items.append(item)
                retained_raw_ids.add(item["id"])
    reasoning_items = _compact_reasoning([
        _reasoning_item(f"E{index}", item)
        for index, item in enumerate(raw_items, 1)
    ])
    retained_ids = {item["id"] for item in reasoning_items}
    retained_aliases = {item["id"]: item["alias"] for item in reasoning_items}
    review_context = {
        key: [
            {
                **item,
                "evidence_alias": retained_aliases.get(item.get("target_id")),
            }
            for item in values
        ]
        for key, values in clinical_review_context(limit=12).items()
    }
    operational = diagnostic_context(as_of=as_of)
    for caveat in operational["caveats"]:
        caveats[(caveat["code"], caveat["domain"], caveat["message"])] = caveat
    public_items = [
        _public_item(item["alias"], items_by_id[item["id"]])
        for item in reasoning_items
    ]
    public_opposing = []
    for item in opposing:
        evidence_id = item.get("evidence_item_id")
        if evidence_id and evidence_id not in retained_ids:
            continue
        public_opposing.append({
            **item,
            "evidence_alias": retained_aliases.get(evidence_id),
        })
    contradictions = [contradictions_by_id[key] for key in sorted(contradictions_by_id)]
    bundle_refs = [
        {
            "scope": scope.name,
            "id": bundle["bundle_id"],
            "version": bundle["bundle_version"],
            "input_hash": bundle["data_version"]["input_hash"],
            "query": bundle["query"],
            "budget": bundle["budget"],
        }
        for scope, bundle in bundles
    ]
    portfolio_hash = _checksum({
        "bundles": [
            {"scope": ref["scope"], "id": ref["id"], "input_hash": ref["input_hash"]}
            for ref in bundle_refs
        ],
        "clinical_reviews": review_context,
        "operational_diagnostics": operational,
    })
    portfolio_id = (
        "urn:glucopilot:companion-evidence-context:"
        + portfolio_hash.removeprefix("sha256:")
    )
    public = {
        "contract_version": CONTRACT_VERSION,
        "bundle": {
            "id": portfolio_id,
            "version": "2.0.0",
            "input_hash": portfolio_hash,
        },
        "question_intent": intent,
        "as_of": as_of.isoformat(),
        "scopes": bundle_refs,
        "evidence_items": public_items,
        "opposing_evidence": public_opposing,
        "contradictions": contradictions,
        "missing_data_caveats": [caveats[key] for key in sorted(caveats)],
        "source_diagnostics": operational["sources"],
        "operational_semantics": operational["semantics"],
        "clinical_reviews": review_context,
        "budget": {
            "configured_items": sum(scope.budget for scope, _ in bundles),
            "prompt_items": len(public_items),
            "prompt_character_limit": MAX_PROMPT_CONTEXT_CHARS,
            "truncated": (
                len(reasoning_items) < len(raw_items)
                or any(bundle["budget"]["truncated"] for _, bundle in bundles)
            ),
        },
    }
    reasoning = {
        "contract_version": CONTRACT_VERSION,
        "bundle_id": portfolio_id,
        "input_hash": portfolio_hash,
        "question_intent": intent,
        "as_of": as_of.isoformat(),
        "items": reasoning_items,
        "opposing_evidence": public_opposing,
        "contradictions": contradictions,
        "missing_data_caveats": public["missing_data_caveats"],
        "source_diagnostics": operational["sources"],
        "operational_semantics": operational["semantics"],
        "clinical_reviews": review_context,
        "budget": public["budget"],
    }
    # Bound what the model actually receives — the slimmed prompt serialization —
    # not the fuller server-side record, so the cap sheds no item needlessly.
    while len(prompt_context(reasoning)) > MAX_PROMPT_CONTEXT_CHARS and reasoning["items"]:
        removed = reasoning["items"].pop()
        public["evidence_items"] = [
            item for item in public["evidence_items"]
            if item["id"] != removed["id"]
        ]
        reasoning["opposing_evidence"] = [
            item for item in reasoning["opposing_evidence"]
            if item.get("evidence_item_id") != removed["id"]
        ]
        public["opposing_evidence"] = [
            item for item in public["opposing_evidence"]
            if item.get("evidence_item_id") != removed["id"]
        ]
        public["budget"]["prompt_items"] = len(reasoning["items"])
        public["budget"]["truncated"] = True
    if len(prompt_context(reasoning)) > MAX_PROMPT_CONTEXT_CHARS:
        raise CompanionEvidenceError(
            "protected contradiction and limitation context exceeds the Companion prompt bound"
        )
    return public, reasoning


def memory_aliases(memories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "alias": f"M{index}",
            "id": memory.get("id"),
            "category": memory.get("category") or "note",
            "content": str(memory.get("content") or "")[:500],
        }
        for index, memory in enumerate(memories, 1)
        if memory.get("content")
    ]


def care_note_aliases(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """[C#] aliases for care-team notes — provider-authored routines, protocols,
    and prescription instructions the Companion may quote and cite. Aliases are
    dense over the non-empty notes so prompt and validation always agree."""
    substantive = [note for note in notes if note.get("title") or note.get("body")]
    return [
        {
            "alias": f"C{index}",
            "id": note.get("id"),
            "kind": note.get("kind") or "note",
            "author": note.get("author_name") or "care team",
            "title": str(note.get("title") or "")[:120],
            "body": str(note.get("body") or "")[:1200],
            "updated": str(note.get("updated_date") or "")[:10],
        }
        for index, note in enumerate(substantive, 1)
    ]


def external_aliases(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "alias": f"G{index}",
            "source_id": _checksum({
                "source": source.get("source"),
                "title": source.get("title"),
                "url": source.get("url"),
            }),
            "title": str(source.get("title") or "")[:300],
            "source": str(source.get("source") or "")[:120],
            "url": str(source.get("url") or "")[:1000],
            "snippet": str(source.get("snippet") or "")[:1000],
        }
        for index, source in enumerate(sources, 1)
    ]


def _confidence_assessed(confidence: Any) -> bool:
    return bool(
        isinstance(confidence, dict)
        and (confidence.get("label") or "not_assessed") != "not_assessed"
    )


def _prompt_timestamp(value: Any) -> Any:
    # Millisecond precision is provenance detail; recency judgements need minutes.
    text = str(value or "")
    return text[:16] + "Z" if len(text) > 17 and text.endswith("Z") else value


def _prompt_item(item: dict[str, Any]) -> dict[str, Any]:
    """The model reads this; it cites [E#] aliases and never echoes an id.

    Identity plumbing — `id`, `entity_id`, `source_ids`, `source_links` — repeats
    one 32-char hash four times per item and stays server-side, where citation
    validation and the UI resolve aliases from the public context. Measured on a
    live 48-item bundle this halves the evidence payload.
    """
    slim: dict[str, Any] = {
        "alias": item.get("alias"),
        "domain": item.get("domain"),
        "entity_type": item.get("entity_type"),
        "observed_at": _prompt_timestamp(item.get("observed_at")),
        "data": item.get("data"),
    }
    title = item.get("title")
    if title and title != item.get("entity_type"):
        slim["title"] = title
    if _confidence_assessed(item.get("confidence")):
        slim["confidence"] = item["confidence"]
    if item.get("claim"):
        slim["claim"] = item["claim"]
    return slim


_PROMPT_TOP_LEVEL_SKIP = {"contract_version", "bundle_id", "input_hash"}


def _prompt_reasoning(reasoning: dict[str, Any]) -> dict[str, Any]:
    slim = {
        key: value
        for key, value in reasoning.items()
        if key not in _PROMPT_TOP_LEVEL_SKIP
    }
    slim["items"] = [_prompt_item(item) for item in reasoning.get("items") or []]
    slim["opposing_evidence"] = [
        {key: value for key, value in item.items() if key != "evidence_item_id"}
        for item in reasoning.get("opposing_evidence") or []
    ]
    slim["clinical_reviews"] = {
        key: [
            {k: v for k, v in item.items() if k != "target_id"}
            for item in values
        ]
        for key, values in (reasoning.get("clinical_reviews") or {}).items()
    }
    return slim


def prompt_context(reasoning: dict[str, Any]) -> str:
    return json.dumps(_prompt_reasoning(reasoning), separators=(",", ":"), default=str)


def _classification(text: str, kinds: set[str]) -> str:
    lower = text.lower()
    if re.search(r"\b(emergency|urgent|911|dose|start|stop|care team|doctor|clinician)\b", lower):
        return "safety_guidance"
    if "C" in kinds and "E" not in kinds:
        # Relaying what a clinician wrote is reporting an instruction, not
        # inferring from data.
        return "care_instruction"
    if "M" in kinds and "E" not in kinds:
        return "user_memory"
    if "E" in kinds:
        if re.search(r"\b(hypothesis|could|may|might|consistent with|possible explanation)\b", lower):
            return "hypothesis"
        if re.search(r"\b(correlat|associated|relationship|lines? up|moves? with)\b", lower):
            return "correlation"
        if re.search(r"\b(average|calculated|percent|%|time in range|compared|difference)\b", lower):
            return "calculation"
        return "observation"
    return "general_information"


def _uncited_personal_claim(text: str, classification: str) -> bool:
    if classification in {"safety_guidance", "user_memory", "care_instruction"}:
        return False
    lower = text.lower()
    personal = re.search(
        r"\b(your|you've|you have|emily|her (?:data|lab|glucose|result|level))\b",
        lower,
    )
    claim = re.search(
        r"\b(data|lab|result|glucose|insulin|sleep|cycle|heart|symptom|diagnosis|"
        r"medication|thyroid|tsh|average|level|trend|pattern|increased|decreased|"
        r"failing|high|low)\b",
        lower,
    )
    definite = re.search(
        r"\b(the|this|that) (?:lab|result|reading|measurement|trend|pattern|level)\b",
        lower,
    )
    numeric = re.search(r"\b\d+(?:\.\d+)?(?:%| mg/dl| mmol/l| miu/l)?\b", lower)
    numeric_personal = numeric and re.search(
        r"\b(was|measured|averaged|came back|recorded)\b",
        lower,
    )
    return bool(claim and (personal or definite or numeric_personal))


# Up to six tokens may sit between the verb and the judgement, so a value and
# its unit and date ("was 1.0 ng/dL in August, below the lab's reference range")
# cannot slip a false judgement past the check. Sentences are judged one at a
# time, so the gap never spans two of them.
_LAB_JUDGEMENT = re.compile(
    r"\b(is|are|was|were|both|came\s+back|looks?|reads?|remains?|remained|stays?|stayed|flagged|"
    r"considered|deemed|rated|rates|marked|marks|notes|calls|labels|(?:it|that|which)['’]s)\s+"
    r"(?:\S+\s+){0,6}?(low|high|elevated|deficient|abnormal|(?:above|below|outside)\s+(?:of\s+)?"
    r"(?:the\s+|your\s+|their\s+|its\s+)?(?:lab'?s?\s+)?(?:normal|reference|optimal)(?:\s+range)?)\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(r"\b(not|never|no)\b|n't", re.IGNORECASE)
_ATTRIBUTED = re.compile(r"\b(functional|optimal)\b", re.IGNORECASE)


def _unflagged_lab_judgement(line: str, evidence_items: list[dict[str, Any]]) -> bool:
    """A line that calls labs low or high while citing only labs the lab itself
    did not flag. The small model reads low-normal values as "low". Saying what
    a functional-medicine report's optimal range makes of a value is allowed
    when the sentence names that report or range and a cited row carries it."""
    labs = [item for item in evidence_items if item.get("entity_type") == "LabResult"]
    if not labs or len(labs) != len(evidence_items):
        return False
    if not any(
        not _NEGATION.search(match.group(0)) for match in _LAB_JUDGEMENT.finditer(line)
    ):
        return False
    if _ATTRIBUTED.search(line) and any(item.get("nonstandard_flag") for item in labs):
        return False
    return all((item.get("flag") or "").strip().lower() in _NORMAL_FLAGS for item in labs)


_GLUCOSE_CLAIM = re.compile(
    r"\b(glucose|blood\s+sugars?|sugars?|bg|cgm|time[-\s]in[-\s]range|tir|gmi|hypoglycemi\w*|hyperglycemi\w*)\b",
    re.IGNORECASE,
)
# Labs count: a fasting glucose is a lab result.
_GLUCOSE_DOMAINS = {"glucose", "insulin", "analytics", "labs"}


def _glucose_claim_without_glucose_evidence(
    line: str, evidence_items: list[dict[str, Any]], kinds: set[str]
) -> bool:
    """A glucose statement whose only citations are wearable, cycle, clinical or
    record items. The alias is real but cannot support the claim; the model wrote
    "bedtime glucose above 150 [E2]" citing a Fitbit day with no glucose."""
    if not evidence_items or kinds & {"M", "C"} or not _GLUCOSE_CLAIM.search(line):
        return False
    return not any(item.get("domain") in _GLUCOSE_DOMAINS for item in evidence_items)


_ABSENCE = re.compile(
    r"\b(?:don'?t|do\s+not|doesn'?t|does\s+not)\s+(?:have|see|show)\b|\black(?:ing)?\b|\bwithout\b|\bmissing\b|"
    r"\bno\s+(?:updated|recent|current|confirmed)\b|\bnot\s+(?:on\s+file|available)\b",
    re.IGNORECASE,
)
_LAB_FAMILIES = (
    ("thyroid", re.compile(r"\b(thyroid|tsh|t3|t4|ft3|ft4)\b", re.IGNORECASE),
     re.compile(r"\btsh\b|thyroid[- ]stimulating|free\s*t[34]|t[34],?\s*free|\bft[34]\b|triiodothyronine|thyroxine", re.IGNORECASE)),
    ("a1c", re.compile(r"\ba1c\b", re.IGNORECASE), re.compile(r"a1c|glycated|glycohemoglobin", re.IGNORECASE)),
    ("ferritin", re.compile(r"\b(ferritin|iron)\b", re.IGNORECASE), re.compile(r"ferritin|\biron\b", re.IGNORECASE)),
    ("b12", re.compile(r"\bb12\b", re.IGNORECASE), re.compile(r"\bb12\b|cobalamin", re.IGNORECASE)),
    ("vitamin_d", re.compile(r"\bvitamin\s*d\b", re.IGNORECASE), re.compile(r"vitamin\s*d|25-?hydroxy", re.IGNORECASE)),
    ("cortisol", re.compile(r"\bcortisol\b", re.IGNORECASE), re.compile(r"cortisol", re.IGNORECASE)),
)
# "We don't have recent labs": labs in general, not a named test. A named test
# ("recent liver labs") is judged by its own family or left alone.
_GENERIC_LABS = re.compile(
    r"^\W*(?:(?:any|recent|updated|current|new|more|the)\s+)*lab(?:s|\s+(?:data|results?|values?))?\b",
    re.IGNORECASE,
)


def _unsupported_lab_absence(line: str, public_context: dict[str, Any]) -> bool:
    """"We don't have thyroid labs" is false when they are in the evidence, and
    unsupported when no labs were retrieved for this question; it was said
    with TSH 2.17 and free T4 1.0 sitting in the evidence."""
    match = _ABSENCE.search(line)
    if not match:
        return False
    window = line[match.end():match.end() + 60]
    families = [
        title_pattern for _name, mention, title_pattern in _LAB_FAMILIES if mention.search(window)
    ]
    if not families and _GENERIC_LABS.search(window):
        families = [re.compile(r".")]
    if not families:
        return False
    scopes = {scope.get("scope") for scope in public_context.get("scopes") or []}
    if not scopes & {"labs_records", "key_labs"}:
        return True
    titles = [
        str(item.get("title") or "")
        for item in public_context.get("evidence_items") or []
        if item.get("entity_type") == "LabResult"
    ]
    return any(pattern.search(title) for pattern in families for title in titles)


# An analysis states its numbers in plain findings and the model restates them,
# sometimes in the wrong role: "the median time to reach that drop was 75
# minutes" when half got there by 25 minutes and 75 was how long the temp basal
# ran. A sentence that cites only analyses is held to them: each number with a
# unit is one they state, a change at a time point is their change at that
# point, a time for glucose to fall, peak or bottom out is their time for that
# event, and walking is not said to prevent lows when lows were more common
# after walking.
_NUMBER = r"\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
_NUMBER_WORDS = {
    word: value
    for value, word in enumerate(
        ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"), 1
    )
}
_HOURS = r"h(?:ou)?rs?\b"
_MINUTES = r"min(?:ute)?s?\b"
_UNIT = rf"mg\s*/\s*dl\b|points?\b|{_MINUTES}|{_HOURS}|%|percent\b|per\s+cent\b"
_QUANTITY = re.compile(
    r"(?<![\w.:/-])[-+−]?(?:"
    rf"(?P<hours>{_NUMBER})[\s-]*{_HOURS}\s*(?:and\s+)?(?P<minutes>{_NUMBER})[\s-]*{_MINUTES}"
    rf"|(?P<and_half>{_NUMBER}|an?)\s+(?:and\s+a\s+half\s+{_HOURS}|{_HOURS}\s+and\s+a\s+half\b)"
    r"|(?P<half_hour>half\s+an\s+hour\b|a\s+half[\s-]hour\b|half[\s-]hour\b)"
    rf"|between\s+(?P<between_low>{_NUMBER})\s+and\s+(?P<between_high>{_NUMBER})[\s-]*(?P<between_unit>{_UNIT})"
    rf"|(?P<low>{_NUMBER})\s*(?:-|–|—|to)\s*(?P<high>{_NUMBER})[\s-]*(?P<range_unit>{_UNIT})"
    r"|(?P<one_hour>an?|one|the\s+first)\s+hour\b"
    rf"|(?P<value>{_NUMBER})[\s-]*(?P<unit>{_UNIT})"
    r")",
    re.IGNORECASE,
)
_BARE_NUMBER = re.compile(r"(?<![\w.:/])[-+−]?(\d+(?:\.\d+)?)(?![\w:])")
_HEDGE_BEFORE = re.compile(
    r"(?:\babout|\baround|\broughly|\bapproximately|\bnearly|\balmost|\bclose\s+to|\bjust\s+(?:under|over)|~|"
    r"\bsome|\bunder|\bover|\b(?:more|less|fewer)\s+than|\bat\s+(?:least|most)|\bup\s+to|"
    r"\bas\s+(?:much|little|long|soon)\s+as)\s*$",
    re.IGNORECASE,
)
_HEDGE_AFTER = re.compile(r"^\s*(?:or\s+(?:so|less|more|sooner|longer|fewer)\b|-?ish\b)", re.IGNORECASE)
_UPPER_BOUND_BEFORE = re.compile(
    r"\b(?:within|by|under|before|inside(?:\s+of)?|less\s+than)\s+(?:about\s+|around\s+|roughly\s+|just\s+|only\s+)?$",
    re.IGNORECASE,
)
_UPPER_BOUND_AFTER = re.compile(r"^\s*or\s+(?:less|sooner|fewer)\b", re.IGNORECASE)
_FREQUENCY_BEFORE = re.compile(r"\b(?:every|each)\s+$", re.IGNORECASE)  # "check every 15 minutes" is advice
# "A difference of 0.6 hours", "a half-hour drop": the gap between two groups'
# values of one measure (sleep after lower and higher bedtimes).
_DIFFERENCE_BEFORE = re.compile(
    r"\b(?:(?:difference|gap)\s+of|los[et]|losing|gain(?:ed)?)\s+(?:about\s+|around\s+|roughly\s+|only\s+|just\s+)?$",
    re.IGNORECASE,
)
_DIFFERENCE_AFTER = re.compile(
    r"^\s*(?:less|more|shorter|longer|lower|higher|fewer|drop|difference|gap|apart)\b", re.IGNORECASE
)
# Standard glucose thresholds a reply may name without the analysis stating them.
_STANDARD_MG_DL = (54, 70, 180, 250)
_ANALYSIS_EVENTS = {
    "fall": re.compile(
        r"\b(?:fall|falls|fell|falling|drop|drops|dropped|dropping|dip|dips|dipped|dipping|"
        r"(?:come|comes|came|coming|go|goes|went|going)\s+down|bring\w*\s+(?:\S+\s+){0,2}?down|"
        r"brought\s+(?:\S+\s+){0,2}?down|lower(?:s|ed|ing)?\s+(?:you|your|her|me|my|it|glucose|blood\s+sugar)|"
        r"start(?:s|ed)?\s+(?:to\s+)?(?:work\w*|lower\w*|bring\w*|fall\w*|drop\w*|dip\w*)|kick(?:s|ed)?\s+in|"
        r"take(?:s)?\s+effect|took\s+effect|g[eo]t(?:s)?\s+there|"
        r"(?:reach|hit)\w*\s+(?:that|the|its|their|a)\s+(?:drop|target)|(?:mg\s*/\s*dl|points?)\s+(?:lower|down)\b|"
        r"below\s+(?:where\s+it\s+started|(?:its|the|your|her|their)\s+start\w*|start\w*))",
        re.IGNORECASE,
    ),
    "peak": re.compile(r"\b(?:peak\w*|top(?:s|ped)?\s+out|highest\s+point)\b", re.IGNORECASE),
    "nadir": re.compile(r"\b(?:lowest|nadir|bottom\w*|low\s+point)\b", re.IGNORECASE),
}
# A change at a time point ("fell 16 mg/dL by 1 hour", "+21 mg/dL at 2 hours")
# is read against the analysis's time course at that point.
_TIME_POINT_BEFORE = re.compile(r"\b(?:at|by|after|in|within|over)\s+$", re.IGNORECASE)
_THRESHOLD_BEFORE = re.compile(
    r"\b(?:from|between|below|above|under|over|than|at|starting|started|start|near|like)\s+$", re.IGNORECASE
)
_THRESHOLD_AFTER = re.compile(r"^\s*(?:below|under|lower|above|over|higher)\b", re.IGNORECASE)
_NOT_A_TIME_COURSE = re.compile(
    r"\b(?:lowest|nadir|bottom\w*|low\s+point|peak\w*|highest|per\s+(?:extra\s+)?units?|"
    r"as\s+much\s+as|up\s+to|at\s+most|max\w*)\b",
    re.IGNORECASE,
)
_TIME_COURSE_KEY = re.compile(r"delta_(\d+)")
_CLAUSE_BREAK = re.compile(r"[,;:()]|\s[-–—]\s|[–—]|\b(?:but|then|while|whereas)\b", re.IGNORECASE)
_WALK = r"\b(?:walk(?:s|ed|ing)?|stroll\w*)\b"
# "Lows", not "low": walking helping with low energy is not about glucose.
_LOWS = (
    r"(?:lows\b|hypos?\b|hypoglycemi\w*|going\s+low\b|go(?:ing)?\s+(?:under|below)\s+70\b|"
    r"dropping\s+(?:too\s+)?low\b|low\s+(?:blood\s+sugars?|glucose)\b)"
)
_LOWS_OBJECT = (
    r"(?:(?:the|your|her|my)\s+)?(?:(?:chances?|risk|odds|likelihood|number|frequency)\s+(?:of\s+)?)?"
    rf"(?:(?:having|getting)\s+)?(?:a\s+low\b|{_LOWS})"
)
_WALKING_PREVENTS_LOWS = re.compile(
    rf"{_WALK}[^.;,!?]{{0,40}}?\b(?:prevent\w*|reduc\w*|lower(?:s|ed|ing)?|decreas\w*|cut(?:s|ting)?|"
    r"protect\w*\s+(?:\w+\s+)?(?:against|from)|guard\w*\s+against|"
    r"help\w*\s+(?:to\s+)?(?:with|prevent|avoid|stop|reduce|lower|against)|"
    rf"keep\w*\s+(?:you|me|her|it|glucose|blood\s+sugar)\s+from|avoid\w*|stop\w*|ward\w*\s+off)\s+{_LOWS_OBJECT}"
    rf"|{_WALK}[^.;,!?]{{0,30}}?\bmake\w*\s+{_LOWS_OBJECT}\s+less\s+(?:likely|common|frequent)"
    rf"|{_LOWS}[^.;,!?]{{0,25}}?\b(?:less|rarer|fewer)(?:\s+(?:common|likely|frequent|often))?\s+"
    rf"(?:after|with|when|while|during|if)\s+(?:you\s+)?(?:go\s+for\s+a\s+)?{_WALK}"
    rf"|\bfewer\s+{_LOWS}[^.;,!?]{{0,20}}?\b(?:after|with|when|while|during|if)\s+(?:you\s+)?{_WALK}",
    re.IGNORECASE,
)
_QUESTION_END = re.compile(r"\?\s*(?:\[[EMGC]\d+\]\s*)*$", re.IGNORECASE)
# "Whether walking prevents lows" asks; "stop walking to avoid lows" says the opposite.
_NOT_WALKING_BEFORE = re.compile(
    r"\b(?:whether|if|stop(?:ping)?|avoid(?:ing)?|skip(?:ping)?|instead\s+of|rather\s+than)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _Quantity:
    dimension: str  # "mg" (mg/dL), "min" (durations, in minutes) or "%"
    low: float
    high: float
    precision: float  # the rounding step it was stated at: 60 for "3 hours", 6 for "8.9 hours"
    start: int
    end: int


def _number(text: str) -> tuple[float, int]:
    word = text.lower()
    if word in _NUMBER_WORDS:
        return float(_NUMBER_WORDS[word]), 0
    return float(word), len(word.partition(".")[2])


def _unit(text: str) -> tuple[str, float]:
    unit = text.lower()
    if unit.startswith(("mg", "point")):
        return "mg", 1
    if unit.startswith("min"):
        return "min", 1
    if unit.startswith("h"):
        return "min", 60
    return "%", 1


def _quantities(text: str) -> list[_Quantity]:
    found = []
    for match in _QUANTITY.finditer(text):
        group = match.groupdict()
        span = match.start(), match.end()
        if group["hours"]:
            hours, _ = _number(group["hours"])
            minutes, _ = _number(group["minutes"])
            found.append(_Quantity("min", hours * 60 + minutes, hours * 60 + minutes, 1, *span))
        elif group["and_half"]:
            whole = 1.0 if group["and_half"].lower() in {"a", "an"} else _number(group["and_half"])[0]
            found.append(_Quantity("min", (whole + 0.5) * 60, (whole + 0.5) * 60, 30, *span))
        elif group["half_hour"]:
            found.append(_Quantity("min", 30, 30, 30, *span))
        elif group["one_hour"]:
            found.append(_Quantity("min", 60, 60, 60, *span))
        else:
            low, high, unit = (
                (group["between_low"], group["between_high"], group["between_unit"])
                if group["between_low"]
                else (group["low"], group["high"], group["range_unit"])
                if group["low"]
                else (group["value"], group["value"], group["unit"])
            )
            dimension, scale = _unit(unit)
            (low_value, low_decimals), (high_value, high_decimals) = _number(low), _number(high)
            precision = 10 ** -max(low_decimals, high_decimals) * scale
            found.append(_Quantity(dimension, low_value * scale, high_value * scale, precision, *span))
    return found


def _hedged(text: str, quantity: _Quantity) -> bool:
    return bool(
        _HEDGE_BEFORE.search(text[max(0, quantity.start - 30):quantity.start])
        or _HEDGE_AFTER.search(text[quantity.end:])
    )


def _tolerance(quantity: _Quantity, hedged: bool) -> float:
    """Rounding at the stated precision ("3 hours" for 170 minutes), a round
    number of minutes or percent ("30 minutes" for 27, "20%" for 19), and more
    for a hedge ("about"). mg/dL values are many and close together, so an
    unhedged one must be exact."""
    exact = quantity.precision / 2
    if quantity.dimension in {"min", "%"} and quantity.precision == 1 and quantity.high % 5 == 0:
        exact = max(exact, 0.1 * quantity.high)
    if not hedged:
        return exact
    return max(exact, {"mg": 2, "%": 2, "min": 5}[quantity.dimension], 0.2 * quantity.high)


def _stated(quantity: _Quantity, values: set[float], hedged: bool) -> bool:
    tolerance = _tolerance(quantity, hedged)
    if quantity.low == quantity.high:
        return any(abs(value - quantity.low) <= tolerance for value in values)
    return all(
        any(abs(value - end) <= tolerance for value in values) for end in (quantity.low, quantity.high)
    ) or any(quantity.low - tolerance <= value <= quantity.high + tolerance for value in values)


def _key_dimension(key: str) -> tuple[str, float] | None:
    """The unit of a supporting_data value, from its key."""
    key = key.lower()
    if "pct" in key or "efficiency" in key:
        return "%", 1
    if "minutes" in key:
        return "min", 1
    if "hours" in key:
        return "min", 60
    if any(word in key for word in ("glucose", "delta", "rise", "drop")):
        return "mg", 1
    return None


@dataclass
class _AnalysisSupport:
    values: dict[str, set[float]]  # by dimension
    differences: dict[str, set[float]]  # between groups' values of one measure
    events: dict[str, set[float]]  # minutes to fall, peak and nadir
    course: dict[int, set[float]]  # mg/dL change by minutes after the start, any group
    lowest: set[float]  # mg/dL from the start to the lowest point in the follow window


def _analysis_support(items: list[dict[str, Any]]) -> _AnalysisSupport:
    values: dict[str, set[float]] = {"mg": set(map(float, _STANDARD_MG_DL)), "min": set(), "%": set()}
    measures: dict[tuple[str, str], list[float]] = {}
    events: dict[str, set[float]] = {event: set() for event in _ANALYSIS_EVENTS}
    course: dict[int, set[float]] = {}
    lowest: set[float] = set()

    def visit(value: Any, key: str) -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if dimension := _key_dimension(key):
                values[dimension[0]].add(abs(value) * dimension[1])
                measures.setdefault((dimension[0], key), []).append(value * dimension[1])
            for event in events:
                if key.endswith(f"minutes_to_{event}"):
                    events[event].add(float(value))
            if offset := _TIME_COURSE_KEY.fullmatch(key):
                course.setdefault(int(offset.group(1)), set()).add(abs(value))
                values["min"].add(float(offset.group(1)))
            if key == "nadir_delta":
                lowest.add(abs(value))

    for item in items:
        analysis = item.get("analysis") or {}
        text = " ".join(str(part) for part in [analysis.get("summary") or "", *(analysis.get("findings") or [])])
        quantities = _quantities(text)
        for quantity in quantities:
            values[quantity.dimension].update({quantity.low, quantity.high})
        for match in _BARE_NUMBER.finditer(text):
            # Unitless numbers in findings are counts and mg/dL values ("30
            # minutes -7", "below 70"); a reply may give them in mg/dL.
            if not any(quantity.start <= match.start() < quantity.end for quantity in quantities):
                values["mg"].add(float(match.group(1)))
        visit(analysis.get("supporting_data"), "")
    differences: dict[str, set[float]] = {dimension: set() for dimension in values}
    for (dimension, _key), group in measures.items():
        differences[dimension].update(
            abs(first - second) for index, first in enumerate(group) for second in group[index + 1:]
        )
    return _AnalysisSupport(values, differences, events, course, lowest)


def _event_of(clause: str, quantity: _Quantity) -> str | None:
    """The event a stated time belongs to: the nearest event named before it in
    its clause ("peaked about 17 minutes"), else the first one after ("takes 2
    hours to start working")."""
    before = [
        (match.end(), event)
        for event, pattern in _ANALYSIS_EVENTS.items()
        for match in pattern.finditer(clause[:quantity.start])
    ]
    if before:
        return max(before)[1]
    after = [
        (match.start(), event)
        for event, pattern in _ANALYSIS_EVENTS.items()
        for match in pattern.finditer(clause, quantity.end)
    ]
    return min(after)[1] if after else None


def _misplaced_event_time(sentence: str, events: dict[str, set[float]]) -> bool:
    """A time to fall, peak or bottom out that is not the analysis's time for
    that event. A clause that pairs a time with a percentage or a change in
    mg/dL ("88% within 2 hours", "16 mg/dL by 1 hour") reads the time course at
    that point, not the time to an event, and is left to the value check."""
    for clause in _CLAUSE_BREAK.split(sentence):
        if not clause or _NEGATION.search(clause):
            continue
        quantities = _quantities(clause)
        if any(
            quantity.dimension == "%"
            or (quantity.dimension == "mg" and quantity.low != correction_response.FALL_MG_DL)
            for quantity in quantities
        ):
            continue
        for quantity in quantities:
            event = _event_of(clause, quantity) if quantity.dimension == "min" else None
            if not event or not events[event]:
                continue
            hedged = _hedged(clause, quantity)
            if _UPPER_BOUND_BEFORE.search(clause[:quantity.start]) or _UPPER_BOUND_AFTER.search(
                clause[quantity.end:]
            ):
                # "within 30 minutes", "by 28 minutes": true when the event came sooner.
                if not any(value <= quantity.high + _tolerance(quantity, hedged) for value in events[event]):
                    return True
            elif not _stated(quantity, events[event], hedged):
                return True
    return False


def _misread_time_course(sentence: str, support: _AnalysisSupport) -> bool:
    """A change in mg/dL given at a time point that is not the analysis's change
    at that point for any group: "fell a median of 30 mg/dL in the first hour"
    when the hour's change was 16 (30 was the label of the 30-minute point)."""
    if not support.course:
        return False
    for clause in _CLAUSE_BREAK.split(sentence):
        if not clause or _NOT_A_TIME_COURSE.search(clause):
            continue
        quantities = _quantities(clause)
        if any(quantity.dimension == "%" for quantity in quantities):
            continue
        changes = [
            quantity for quantity in quantities
            if quantity.dimension == "mg" and quantity.low == quantity.high
            and not _THRESHOLD_BEFORE.search(clause[:quantity.start])
            and not _THRESHOLD_AFTER.search(clause[quantity.end:])
        ]
        points = [
            quantity for quantity in quantities
            if quantity.dimension == "min" and quantity.low == quantity.high
            and _TIME_POINT_BEFORE.search(clause[:quantity.start])
        ]
        if not changes or len(changes) != len(points):
            continue
        for change, point in zip(changes, points):
            stated = support.course.get(round(point.low))
            if stated is None:
                continue
            if clause[:point.start].rstrip().lower().endswith("within"):
                stated = stated | support.lowest  # "dropped 16 mg/dL within 2 hours"
            if not _stated(change, stated, _hedged(clause, change)):
                return True
    return False


def _walking_prevents_lows(sentence: str, items: list[dict[str, Any]]) -> bool:
    """"Walking lowers the chance of going under 70" when no starting band had
    lows less common after walking than after sitting."""
    if _QUESTION_END.search(sentence):
        return False
    claims = [
        match for match in _WALKING_PREVENTS_LOWS.finditer(sentence)
        if not _NEGATION.search(match.group(0))
        and not _NOT_WALKING_BEFORE.search(sentence[:match.start()])
    ]
    if not claims:
        return False
    rates = [
        (band["walking"].get("went_low_pct"), band["sitting"].get("went_low_pct"))
        for item in items
        if item.get("entity_type") == analysis_evidence.WALKING
        for band in ((item.get("analysis") or {}).get("supporting_data") or {}).get("bands", {}).values()
        if isinstance(band, dict) and isinstance(band.get("walking"), dict) and isinstance(band.get("sitting"), dict)
    ]
    rates = [(walked, sat) for walked, sat in rates if walked is not None and sat is not None]
    return bool(rates) and not any(walked < sat for walked, sat in rates)


def _misstated_analysis(sentence: str, items: list[dict[str, Any]], kinds: set[str]) -> bool:
    """A sentence citing only analyses that gives a number they do not state,
    puts one of their times on the wrong event, or reverses walking and lows.
    A memory, care note or other evidence cited alongside may be the source
    of a number, so those sentences are left alone."""
    if (
        not items
        or kinds - {"E"}
        or any(item.get("entity_type") not in analysis_evidence.ENTITY_TYPES for item in items)
    ):
        return False
    if _walking_prevents_lows(sentence, items):
        return True
    quantities = _quantities(sentence)
    if not quantities:
        return False
    support = _analysis_support(items)
    for quantity in quantities:
        hedged = _hedged(sentence, quantity)
        if _FREQUENCY_BEFORE.search(sentence[:quantity.start]) or _stated(
            quantity, support.values[quantity.dimension], hedged
        ):
            continue
        framed_as_difference = _DIFFERENCE_BEFORE.search(
            sentence[max(0, quantity.start - 30):quantity.start]
        ) or _DIFFERENCE_AFTER.search(sentence[quantity.end:])
        if framed_as_difference and _stated(quantity, support.differences[quantity.dimension], hedged):
            continue
        return True
    return _misplaced_event_time(sentence, support.events) or _misread_time_course(sentence, support)


def _drop_unsupported_sentences(
    line: str,
    public_context: dict[str, Any],
    evidence_by_alias: dict[str, dict[str, Any]],
) -> tuple[str, int]:
    """Judge each sentence by its own citations. A paragraph that cites flagged
    antibodies in one sentence must not carry "TSH 2.17 (flagged high)" in the
    next, and one false sentence should not cost the rest of the paragraph."""
    kept: list[str] = []
    dropped = 0
    for sentence in companion_safety.split_sentences(line):
        aliases = {f"{kind.upper()}{int(number)}" for kind, number in _CITATION_RE.findall(sentence)}
        items = [evidence_by_alias[alias] for alias in aliases if alias in evidence_by_alias]
        kinds = {alias[0] for alias in aliases}
        if (
            _unflagged_lab_judgement(sentence, items)
            or _glucose_claim_without_glucose_evidence(sentence, items, kinds)
            or _unsupported_lab_absence(sentence, public_context)
            or _misstated_analysis(sentence, items, kinds)
        ):
            dropped += 1
            continue
        kept.append(sentence)
    return " ".join(kept), dropped


def _qualify_unverified(text: str) -> str:
    lower = text.lower()
    if any(
        phrase in lower
        for phrase in ("unverified", "machine-extracted", "not clinically verified", "not yet verified")
    ):
        return text
    # A heading or bold-only line reads badly with a prefix; mark it at the end.
    if re.match(r"^\s*(?:[-*+]\s+)?(?:#{1,6}\s+.*|\*\*[^*]+\*\*(?:\s*\[[EMGC]\d+\])*)\s*$", text):
        return f"{text.rstrip()} (unverified lab)"
    match = re.match(r"^(\s*(?:[-*+]\s+|#{1,6}\s+)?)", text)
    prefix = match.group(1) if match else ""
    return f"{prefix}Unverified machine-extracted lab evidence: {text[len(prefix):]}"


def _normalize_citations(line: str) -> str:
    def canonical(match: re.Match[str]) -> str:
        if _ALIAS_LOOKALIKE_RE.search(line[:match.start()]):
            return match.group(0)
        return " ".join(
            f"[{alias.upper()}]" for alias in _ALIAS_IN_GROUP_RE.findall(match.group(1))
        )

    return _ALIAS_GROUP_RE.sub(canonical, line)


def finalize_reply(
    reply: str,
    public_context: dict[str, Any],
    memories: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    care_notes: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Validate aliases, qualify labs, and persist bounded claim/source links."""
    evidence_by_alias = {
        item["alias"].upper(): item
        for item in public_context.get("evidence_items", [])
    }
    memory_by_alias = {
        item["alias"].upper(): item
        for item in memory_aliases(memories)
    }
    external_by_alias = {
        item["alias"].upper(): item
        for item in external_aliases(sources)
    }
    note_by_alias = {
        item["alias"].upper(): item
        for item in care_note_aliases(care_notes or [])
    }
    valid = {
        **evidence_by_alias,
        **memory_by_alias,
        **external_by_alias,
        **note_by_alias,
    }
    output_lines: list[str] = []
    statements: list[dict[str, Any]] = []
    used_evidence: dict[str, dict[str, Any]] = {}
    used_external: dict[str, dict[str, Any]] = {}
    used_memories: dict[str, dict[str, Any]] = {}
    used_notes: dict[str, dict[str, Any]] = {}
    omitted_claims = 0

    for line in companion_safety.plain(reply).splitlines():
        line = _normalize_citations(line)
        if line.strip():
            line, dropped = _drop_unsupported_sentences(line, public_context, evidence_by_alias)
            omitted_claims += dropped
            if not line.strip():
                continue
        aliases = []
        for kind, number in _CITATION_RE.findall(line):
            alias = f"{kind.upper()}{int(number)}"
            if alias in valid and alias not in aliases:
                aliases.append(alias)
        line = _CITATION_RE.sub(
            lambda match: (
                f"[{match.group(1).upper()}{int(match.group(2))}]"
                if f"{match.group(1).upper()}{int(match.group(2))}" in valid
                else ""
            ),
            line,
        )
        if not line.strip():
            output_lines.append(line)
            continue
        kinds = {alias[0] for alias in aliases}
        classification = _classification(line, kinds)
        evidence_items = [evidence_by_alias[alias] for alias in aliases if alias in evidence_by_alias]
        if any(
            item.get("entity_type") == "LabResult"
            and not item.get("confidence", {}).get("clinically_verified")
            for item in evidence_items
        ):
            line = _qualify_unverified(line)
        if not (kinds & {"E", "M", "C"}) and _uncited_personal_claim(line, classification):
            # Fail closed without turning every unsupported sentence into a
            # repeated, alarming chat message. The omission is represented once
            # in the response footer and structurally in the evidence metadata.
            omitted_claims += 1
            continue
        for item in evidence_items:
            used_evidence[item["alias"]] = item
        for alias in aliases:
            if alias in external_by_alias:
                used_external[alias] = external_by_alias[alias]
            if alias in memory_by_alias:
                used_memories[alias] = memory_by_alias[alias]
            if alias in note_by_alias:
                used_notes[alias] = note_by_alias[alias]
        source_ids = list(dict.fromkeys(
            source_id
            for item in evidence_items
            for source_id in item.get("source_ids") or []
        ))
        source_links: dict[tuple[str, str], dict[str, Any]] = {}
        for item in evidence_items:
            for link in item.get("source_links") or []:
                if link.get("kind") and link.get("href"):
                    source_links[(str(link["kind"]), str(link["href"]))] = link
        statements.append({
            "ordinal": len(statements),
            "text": line.strip()[:2_000],
            "classification": classification,
            "personal_data_claim": classification in {
                "observation", "calculation", "correlation", "hypothesis",
            },
            "evidence_aliases": [item["alias"] for item in evidence_items],
            "evidence_item_ids": [item["id"] for item in evidence_items],
            "source_ids": source_ids,
            "source_links": [source_links[key] for key in sorted(source_links)],
            "memory_ids": [
                memory_by_alias[alias].get("id")
                for alias in aliases
                if alias in memory_by_alias and memory_by_alias[alias].get("id")
            ],
            "external_source_ids": [
                external_by_alias[alias]["source_id"]
                for alias in aliases
                if alias in external_by_alias
            ],
            "care_note_ids": [
                note_by_alias[alias].get("id")
                for alias in aliases
                if alias in note_by_alias and note_by_alias[alias].get("id")
            ],
        })
        output_lines.append(line)

    sanitized = "\n".join(output_lines).strip()
    if omitted_claims:
        omission_note = (
            "Some generated statements were omitted because they could not be "
            "linked to your records."
        )
        sanitized = f"{sanitized}\n\n{omission_note}".strip()
    evidence = {
        "contract_version": CONTRACT_VERSION,
        "bundle": public_context.get("bundle"),
        "question_intent": public_context.get("question_intent"),
        "as_of": public_context.get("as_of"),
        "scopes": public_context.get("scopes") or [],
        "statements": statements,
        "evidence_items": [used_evidence[key] for key in sorted(used_evidence)],
        "memories": [used_memories[key] for key in sorted(used_memories)],
        "external_sources": [used_external[key] for key in sorted(used_external)],
        "care_notes": [used_notes[key] for key in sorted(used_notes)],
        "opposing_evidence": public_context.get("opposing_evidence") or [],
        "contradictions": public_context.get("contradictions") or [],
        "missing_data_caveats": public_context.get("missing_data_caveats") or [],
        "budget": public_context.get("budget") or {},
        "omissions": {
            "count": omitted_claims,
            "reason": "missing_personal_evidence_citation" if omitted_claims else None,
        },
    }
    return sanitized, evidence


def compare_contexts(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    previous_scopes = {
        item["scope"]: item.get("input_hash")
        for item in previous.get("scopes") or []
    }
    current_scopes = {
        item["scope"]: item.get("input_hash")
        for item in current.get("scopes") or []
    }
    names = sorted(set(previous_scopes) | set(current_scopes))
    changed_scopes = [
        name for name in names
        if previous_scopes.get(name) != current_scopes.get(name)
    ]
    previous_bundle = previous.get("bundle") or {}
    current_bundle = current.get("bundle") or {}
    return {
        "changed": previous_bundle.get("input_hash") != current_bundle.get("input_hash"),
        "previous_bundle_id": previous_bundle.get("id"),
        "current_bundle_id": current_bundle.get("id"),
        "changed_scopes": changed_scopes,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
