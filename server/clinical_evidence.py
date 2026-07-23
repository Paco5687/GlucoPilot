"""Shared Evidence Bundle consumer for Overview and Visit Report dossiers.

Deterministic report metrics remain owned by their domain calculators. This
module supplies the common clinical evidence semantics used to ground generated
narrative: quality, data-through, contradictions, sources, and governed claims.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from .contradictions import refresh_current as refresh_contradictions
from .evidence_bundle import EvidenceBundleQuery, EvidenceDomain, build_bundle


CONTEXT_VERSION = "clinical-evidence-context/1.0.0"
CONTEXT_SCOPES = (
    (
        "claims",
        (EvidenceDomain.ANALYTICS,),
        "pattern insight analytics claim evidence",
        50,
    ),
    (
        "clinical",
        (EvidenceDomain.CLINICAL,),
        "diagnosis medication allergy symptom history profile clinical",
        50,
    ),
    (
        "labs_records",
        (EvidenceDomain.LABS, EvidenceDomain.RECORDS),
        "lab result medical record source document",
        150,
    ),
)
CONTEXT_DOMAINS = tuple(sorted({domain for _, domains, _, _ in CONTEXT_SCOPES for domain in domains}, key=str))
_NARRATIVE_EXCLUDED_TYPES = {
    "DailySummary",
    "HealthSummary",
    "InsuranceInfo",
    "WeeklySummary",
}
_QUALITY_FIELDS = (
    "version",
    "ai_eligible",
    "coverage_status",
    "freshness_status",
    "observed",
    "expected",
    "unit",
    "coverage_ratio",
    "data_through",
    "as_of",
    "limitations",
    "input_data_version",
)
_REASONING_DATA_FIELDS = {
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


def _window(days: int, as_of: date) -> tuple[datetime, datetime]:
    start_date = as_of - timedelta(days=days - 1)
    return (
        datetime.combine(start_date, time.min, tzinfo=timezone.utc),
        datetime.combine(as_of, time.max, tzinfo=timezone.utc),
    )


def _checksum(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def _quality_blocks(data_quality: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = []
    for domain in sorted(data_quality):
        envelope = data_quality[domain]
        if not isinstance(envelope, dict):
            continue
        blocks.append({
            "domain": domain,
            **{key: envelope.get(key) for key in _QUALITY_FIELDS if key in envelope},
        })
    return blocks


def _bundle_items(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = bundle["evidence"]
    return [
        *evidence["derived_metrics"],
        *evidence["documents"],
        *evidence["direct_observations"],
        *evidence["relationships"],
    ]


def _data_through(items: list[dict[str, Any]], quality: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, str] = {}
    for item in items:
        observed_at = item.get("observed_at")
        domain = item.get("domain")
        if domain and observed_at and str(observed_at) > latest.get(str(domain), ""):
            latest[str(domain)] = str(observed_at)
    for block in quality:
        observed_at = block.get("data_through")
        domain = block["domain"]
        if observed_at and str(observed_at) > latest.get(domain, ""):
            latest[domain] = str(observed_at)
    domains = sorted({
        *(domain.value for domain in CONTEXT_DOMAINS),
        *(str(item.get("domain")) for item in items if item.get("domain")),
        *latest,
    })
    return [{"domain": domain, "through": latest.get(domain)} for domain in domains]


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    title = data.get("title") or data.get("test_name") or data.get("name") or item.get("entity_type")
    return {
        "id": item["id"],
        "domain": item.get("domain"),
        "entity_type": item.get("entity_type"),
        "entity_id": item.get("entity_id"),
        "observed_at": item.get("observed_at"),
        "title": title,
        "confidence": item.get("confidence"),
        "source_links": item.get("source_links") or [],
        "claim": item.get("claim"),
    }


def _reasoning_data(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    return {
        key: data[key]
        for key in sorted(_REASONING_DATA_FIELDS)
        if key in data
    }


def _claims(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    claims = []
    for item in items:
        claim = item.get("claim")
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        if (
            not claim
            or claim.get("assertion_status") == "superseded"
            or data.get("is_active") is False
        ):
            continue
        claims.append({
            "claim_type": item["entity_type"],
            "claim_id": item["entity_id"],
            "title": data.get("title") or item["entity_type"],
            "assertion_kind": claim["assertion_kind"],
            "assertion_status": claim["assertion_status"],
            "version_number": claim["version_number"],
            "confidence": item.get("confidence"),
            "href": claim["href"],
        })
    return claims


def build_context(
    days: int,
    *,
    data_quality: dict[str, Any],
    as_of: date | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a compact public block and a bounded LLM-safe evidence dossier."""
    days = max(7, min(int(days), 365))
    as_of = as_of or datetime.now(timezone.utc).date()
    start, end = _window(days, as_of)
    refresh_contradictions()
    scoped_bundles = []
    items_by_id: dict[str, dict[str, Any]] = {}
    contradictions_by_id: dict[str, dict[str, Any]] = {}
    caveats_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for scope, domains, question_intent, item_budget in CONTEXT_SCOPES:
        bundle = build_bundle(EvidenceBundleQuery(
            start=start,
            end=end,
            domains=domains,
            question_intent=question_intent,
            item_budget=item_budget,
        ))
        scoped_bundles.append((scope, bundle))
        for item in _bundle_items(bundle):
            items_by_id.setdefault(item["id"], item)
        for contradiction in bundle["contradictions"]:
            contradictions_by_id[contradiction["id"]] = contradiction
        for caveat in bundle["missing_data_caveats"]:
            key = (caveat["code"], caveat["domain"], caveat["message"])
            caveats_by_key[key] = caveat
    items = list(items_by_id.values())
    narrative_items = [
        item for item in items if item.get("entity_type") not in _NARRATIVE_EXCLUDED_TYPES
    ]
    quality = _quality_blocks(data_quality)
    data_through = _data_through(narrative_items, quality)
    claims = _claims(narrative_items)
    evidence_items = [_public_item(item) for item in narrative_items]
    source_links: dict[tuple[str, str], dict[str, Any]] = {}
    for item in narrative_items:
        for link in item.get("source_links") or []:
            if link.get("kind") and link.get("href"):
                source_links[(str(link["kind"]), str(link["href"]))] = link
    public_sources = [source_links[key] for key in sorted(source_links)]
    bundle_refs = [
        {
            "scope": scope,
            "id": bundle["bundle_id"],
            "version": bundle["bundle_version"],
            "data_version": bundle["data_version"],
            "query": bundle["query"],
            "budget": bundle["budget"],
        }
        for scope, bundle in scoped_bundles
    ]
    portfolio_hash = _checksum([
        {"scope": item["scope"], "id": item["id"], "input_hash": item["data_version"]["input_hash"]}
        for item in bundle_refs
    ])
    portfolio_id = (
        "urn:glucopilot:clinical-evidence-context:"
        + portfolio_hash.removeprefix("sha256:")
    )
    contradictions = [contradictions_by_id[key] for key in sorted(contradictions_by_id)]
    caveats = [caveats_by_key[key] for key in sorted(caveats_by_key)]
    public = {
        "contract_version": CONTEXT_VERSION,
        "bundle": {
            "id": portfolio_id,
            "version": "2.0.0",
            "data_version": {
                "contract_name": CONTEXT_VERSION,
                "input_hash": portfolio_hash,
                "bundles": [item["data_version"]["input_hash"] for item in bundle_refs],
            },
            "query": {"days": days, "as_of": as_of.isoformat()},
        },
        "bundles": bundle_refs,
        "data_quality": quality,
        "data_through": data_through,
        "contradictions": contradictions,
        "sources": {
            "links": public_sources,
            "returned": len(public_sources),
            "truncated": any(bundle["budget"]["truncated"] for _, bundle in scoped_bundles),
        },
        "claims": claims,
        "evidence_items": evidence_items,
        "missing_data_caveats": caveats,
    }
    reasoning = {
        "contract_version": CONTEXT_VERSION,
        "bundle_id": portfolio_id,
        "data_version": public["bundle"]["data_version"],
        "data_quality": quality,
        "data_through": data_through,
        "contradictions": contradictions,
        "missing_data_caveats": caveats,
        "items": [
            {
                "id": item["id"],
                "domain": item.get("domain"),
                "entity_type": item.get("entity_type"),
                "entity_id": item.get("entity_id"),
                "observed_at": item.get("observed_at"),
                "data": _reasoning_data(item),
                "confidence": item.get("confidence"),
                "source_links": item.get("source_links") or [],
                "claim": item.get("claim"),
            }
            for item in narrative_items
        ],
    }
    return public, reasoning


def link_generated_narrative(
    narrative: dict[str, Any] | None,
    reasoning: dict[str, Any],
) -> dict[str, Any] | None:
    """Discard invented evidence IDs and attach only links from selected bundle items."""
    if not narrative:
        return narrative
    item_by_id = {item["id"]: item for item in reasoning.get("items", [])}
    supplied = narrative.get("evidence_item_ids")
    if not isinstance(supplied, list):
        supplied = []
    ids = []
    for item_id in supplied:
        if item_id in item_by_id and item_id not in ids:
            ids.append(item_id)
    links: dict[tuple[str, str], dict[str, Any]] = {}
    for item_id in ids:
        item = item_by_id[item_id]
        for link in item.get("source_links") or []:
            href = link.get("href")
            kind = link.get("kind")
            if href and kind:
                links[(str(kind), str(href))] = link
    return {
        **narrative,
        "evidence_item_ids": ids,
        "evidence_links": [links[key] for key in sorted(links)],
        "evidence_bundle_id": reasoning.get("bundle_id"),
    }
