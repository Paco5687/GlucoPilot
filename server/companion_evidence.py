"""Bounded Evidence Bundle grounding and claim links for Companion replies."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from .contradictions import refresh_current as refresh_contradictions
from .evidence_bundle import EvidenceBundleQuery, EvidenceDomain, build_bundle
from .clinical_reviews import companion_context as clinical_review_context


CONTRACT_VERSION = "companion-evidence-context/1.0.0"
MAX_PROMPT_CONTEXT_CHARS = 48_000
_CITATION_RE = re.compile(r"\[([EMG])(\d+)\]", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
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
    "flag",
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
            "basal", "bolus", "carb", "cgm", "diabetes", "fingerstick",
            "glooko", "glucose", "glycemic", "hypoglycemia", "insulin",
            "pump", "sugar", "tdd", "tir",
        }),
    ),
    Scope(
        "wellness",
        (EvidenceDomain.WEARABLES, EvidenceDomain.CYCLE),
        14,
        12,
        12,
        frozenset({
            "activity", "cycle", "energy", "exercise", "fitbit", "heart", "hormone",
            "hrv", "menstrual", "oura", "period", "position", "sitting", "sleep",
            "spo2", "standing", "steps", "walking", "workout",
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
            "allergy", "condition", "diagnosis", "history", "medication", "medicine",
            "profile", "symptom", "treatment", "weight",
        }),
    ),
    Scope(
        "labs_records",
        (EvidenceDomain.LABS, EvidenceDomain.RECORDS),
        3650,
        24,
        150,
        frozenset({
            "antibody", "cortisol", "imaging", "inflammation", "lab", "marker",
            "record", "report", "result", "test", "thyroid", "tsh", "urine",
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


def _selected_scopes(question: str) -> tuple[Scope, ...]:
    lower = question.lower()
    tokens = set(_TOKEN_RE.findall(lower))
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
    return f"{question} {hints[scope.name]}"


def _bundle_items(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = bundle["evidence"]
    return [
        *evidence["derived_metrics"],
        *evidence["documents"],
        *evidence["direct_observations"],
        *evidence["relationships"],
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


def _reasoning_data(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    return {
        key: _bounded(data[key])
        for key in sorted(_REASONING_FIELDS)
        if key in data
    }


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
    return {
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
    }


def _reasoning_item(alias: str, item: dict[str, Any]) -> dict[str, Any]:
    public = _public_item(alias, item)
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
    intent = " ".join(str(question or "").split())[:500] or "whole person health context"
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

    raw_items = []
    retained_raw_ids: set[str] = set()
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
        "clinical_reviews": review_context,
        "budget": public["budget"],
    }
    while (
        len(json.dumps(reasoning, separators=(",", ":"), default=str))
        > MAX_PROMPT_CONTEXT_CHARS
        and reasoning["items"]
    ):
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
    if len(json.dumps(reasoning, separators=(",", ":"), default=str)) > MAX_PROMPT_CONTEXT_CHARS:
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


def prompt_context(reasoning: dict[str, Any]) -> str:
    return json.dumps(reasoning, separators=(",", ":"), default=str)


def _classification(text: str, kinds: set[str]) -> str:
    lower = text.lower()
    if re.search(r"\b(emergency|urgent|911|dose|start|stop|care team|doctor|clinician)\b", lower):
        return "safety_guidance"
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
    if classification in {"safety_guidance", "user_memory"}:
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


def _qualify_unverified(text: str) -> str:
    lower = text.lower()
    if any(
        phrase in lower
        for phrase in ("unverified", "machine-extracted", "not clinically verified", "not yet verified")
    ):
        return text
    match = re.match(r"^(\s*(?:[-*+]\s+|#{1,6}\s+)?)", text)
    prefix = match.group(1) if match else ""
    return f"{prefix}Unverified machine-extracted lab evidence: {text[len(prefix):]}"


def finalize_reply(
    reply: str,
    public_context: dict[str, Any],
    memories: list[dict[str, Any]],
    sources: list[dict[str, Any]],
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
    valid = {
        **evidence_by_alias,
        **memory_by_alias,
        **external_by_alias,
    }
    output_lines: list[str] = []
    statements: list[dict[str, Any]] = []
    used_evidence: dict[str, dict[str, Any]] = {}
    used_external: dict[str, dict[str, Any]] = {}
    used_memories: dict[str, dict[str, Any]] = {}

    for line in str(reply or "").splitlines():
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
        if not (kinds & {"E", "M"}) and _uncited_personal_claim(line, classification):
            line = "I don't have bounded personal evidence to support that statement."
            aliases = [alias for alias in aliases if alias.startswith("G")]
            kinds = {alias[0] for alias in aliases}
            classification = "general_information"
            evidence_items = []
        for item in evidence_items:
            used_evidence[item["alias"]] = item
        for alias in aliases:
            if alias in external_by_alias:
                used_external[alias] = external_by_alias[alias]
            if alias in memory_by_alias:
                used_memories[alias] = memory_by_alias[alias]
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
        })
        output_lines.append(line)

    sanitized = "\n".join(output_lines).strip()
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
        "opposing_evidence": public_context.get("opposing_evidence") or [],
        "contradictions": public_context.get("contradictions") or [],
        "missing_data_caveats": public_context.get("missing_data_caveats") or [],
        "budget": public_context.get("budget") or {},
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
