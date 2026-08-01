"""The prompt-facing evidence payload carries reasoning material, not plumbing.

Measured on a live 48-item bundle, identity fields the model never reads —
`id`, `entity_id`, `source_ids`, `source_links`, all-null confidence objects —
were ~20k of 42.6k chars, enough that a routine question overflowed the fast
model's whole 16,384-token window (#183). The model cites aliases; ids live
server-side where citation validation and the UI resolve them.
"""

import json

from server import clinical_evidence, companion_evidence


def _bundle_item(**overrides):
    item = {
        "alias": "E1",
        "id": "entity:GlucoseReading:d832a88d82374161bdbbdc093b01931f",
        "domain": "glucose",
        "entity_type": "GlucoseReading",
        "entity_id": "d832a88d82374161bdbbdc093b01931f",
        "observed_at": "2026-08-01T17:32:00.772Z",
        "title": "GlucoseReading",
        "confidence": {"label": "not_assessed", "score": None, "method": None},
        "source_ids": ["entity:GlucoseReading:d832a88d82374161bdbbdc093b01931f"],
        "source_links": [
            {
                "kind": "normalized_entity",
                "entity_type": "GlucoseReading",
                "entity_id": "d832a88d82374161bdbbdc093b01931f",
                "href": "/api/entities/GlucoseReading/d832a88d82374161bdbbdc093b01931f",
            }
        ],
        "claim": None,
        "data": {"value": 132, "trend": "flat"},
    }
    item.update(overrides)
    return item


def _reasoning(items):
    return {
        "contract_version": "companion-evidence-context/1.0.0",
        "bundle_id": "urn:glucopilot:companion-evidence-context:" + "a" * 64,
        "input_hash": "sha256:" + "a" * 64,
        "question_intent": "how did last night look?",
        "as_of": "2026-08-01",
        "items": items,
        "opposing_evidence": [
            {"evidence_item_id": items[0]["id"], "evidence_alias": "E1", "summary": "counterpoint"}
        ],
        "contradictions": [],
        "missing_data_caveats": [],
        "clinical_reviews": {"labs": [{"target_id": items[0]["id"], "evidence_alias": "E1", "status": "approved"}]},
        "budget": {"prompt_items": len(items), "truncated": False},
    }


def test_prompt_omits_identity_plumbing_but_keeps_reasoning_material():
    prompt = companion_evidence.prompt_context(_reasoning([_bundle_item()]))

    # What the model reasons with survives...
    assert '"alias":"E1"' in prompt
    assert '"value":132' in prompt
    assert '"domain":"glucose"' in prompt
    # ...while the hash appears nowhere in any of its four guises.
    assert "d832a88d82374161bdbbdc093b01931f" not in prompt
    assert "sha256" not in prompt
    assert "source_links" not in prompt
    assert "not_assessed" not in prompt


def test_prompt_keeps_assessed_confidence_and_meaningful_titles():
    item = _bundle_item(
        title="Cortisol AM",
        confidence={"label": "high", "score": 0.92, "method": "parser"},
        claim="cortisol elevated",
    )
    prompt = companion_evidence.prompt_context(_reasoning([item]))

    assert '"title":"Cortisol AM"' in prompt
    assert '"label":"high"' in prompt
    assert '"claim":"cortisol elevated"' in prompt


def test_title_matching_entity_type_is_not_repeated():
    prompt = companion_evidence.prompt_context(_reasoning([_bundle_item()]))
    # entity_type appears once; a title that merely repeats it is dropped.
    assert prompt.count("GlucoseReading") == 1


def test_prompt_timestamps_keep_minutes_not_milliseconds():
    prompt = companion_evidence.prompt_context(_reasoning([_bundle_item()]))
    assert '"observed_at":"2026-08-01T17:32Z"' in prompt


def test_server_side_reasoning_is_not_mutated():
    reasoning = _reasoning([_bundle_item()])
    before = json.dumps(reasoning, sort_keys=True, default=str)

    companion_evidence.prompt_context(reasoning)

    assert json.dumps(reasoning, sort_keys=True, default=str) == before


def test_clinical_prompt_view_aliases_ids_and_translates_back():
    reasoning = {
        "contract_version": "clinical-evidence-context/1.0.0",
        "bundle_id": "urn:glucopilot:clinical-evidence-context:" + "b" * 64,
        "data_version": {"input_hash": "sha256:" + "b" * 64},
        "items": [
            {
                "id": "entity:LabResult:aaaa1111",
                "domain": "labs",
                "entity_type": "LabResult",
                "entity_id": "aaaa1111",
                "observed_at": "2026-07-30T10:00:00.000Z",
                "data": {"test_name": "TSH", "value": 2.1},
                "confidence": {"label": "not_assessed"},
                "source_links": [{"kind": "normalized_entity", "href": "/x"}],
                "claim": None,
            },
            {
                "id": "entity:GlucoseReading:bbbb2222",
                "domain": "glucose",
                "entity_type": "GlucoseReading",
                "entity_id": "bbbb2222",
                "observed_at": "2026-07-31T08:00:00.000Z",
                "data": {"value": 145},
                "confidence": {"label": "high", "score": 0.9},
                "source_links": [],
                "claim": "elevated fasting glucose",
            },
        ],
    }

    slim, alias_to_id = clinical_evidence.prompt_view(reasoning)

    dumped = json.dumps(slim, default=str)
    assert '"id": "N1"' in dumped and '"id": "N2"' in dumped
    assert "aaaa1111" not in dumped and "bbbb2222" not in dumped
    assert "source_links" not in dumped
    assert alias_to_id == {
        "N1": "entity:LabResult:aaaa1111",
        "N2": "entity:GlucoseReading:bbbb2222",
    }

    narrative = {
        "headline": "…",
        "evidence_item_ids": ["N2", "N1", "N2", "N9", "entity:LabResult:aaaa1111"],
        "observations": [
            {"title": "obs", "detail": "…", "evidence_item_ids": ["N1", "invented"]}
        ],
    }
    resolved = clinical_evidence.resolve_prompt_aliases(narrative, alias_to_id)

    # Echoed aliases resolve in order, deduped; invented or raw ids drop —
    # the same discipline link_generated_narrative applied to full ids.
    assert resolved["evidence_item_ids"] == [
        "entity:GlucoseReading:bbbb2222",
        "entity:LabResult:aaaa1111",
    ]
    assert resolved["observations"][0]["evidence_item_ids"] == ["entity:LabResult:aaaa1111"]


def test_resolve_prompt_aliases_passes_none_through():
    assert clinical_evidence.resolve_prompt_aliases(None, {"N1": "x"}) is None
