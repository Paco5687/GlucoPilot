"""Care notes feed the Companion via the [C#] citation family.

A statement relaying what a clinician wrote must survive validation when it
cites the note, keep its attribution, and never be silently reclassified as a
data-derived finding — while an invented [C#] still gets stripped.
"""

from __future__ import annotations

import pytest

from server import companion, companion_evidence

pytestmark = pytest.mark.risk_critical

NOTES = [
    {
        "id": "note-basal",
        "kind": "protocol",
        "title": "Overnight basal protocol",
        "body": "Reduce basal 10% after evening exercise.",
        "author_name": "Bill",
        "updated_date": "2026-08-01T12:00:00Z",
        "pinned": True,
    },
    {
        "id": "note-labs",
        "kind": "instruction",
        "title": "Quarterly labs",
        "body": "Repeat TSH and cortisol panel every 3 months.",
        "author_name": "Bill",
        "updated_date": "2026-07-15T12:00:00Z",
    },
]

EMPTY_CONTEXT = {"evidence_items": [], "bundle": {"id": "b", "version": "2.0.0"}}


def _finalize(reply, notes=NOTES):
    return companion_evidence.finalize_reply(reply, EMPTY_CONTEXT, [], [], notes)


def test_note_backed_statement_survives_with_attribution():
    reply = "Bill's overnight basal protocol says to reduce basal 10% after evening exercise [C1]"

    sanitized, evidence = _finalize(reply)

    assert "reduce basal 10%" in sanitized
    assert "[C1]" in sanitized
    assert evidence["omissions"]["count"] == 0
    assert [note["id"] for note in evidence["care_notes"]] == ["note-basal"]
    statement = evidence["statements"][0]
    assert statement["classification"] == "care_instruction"
    assert statement["care_note_ids"] == ["note-basal"]
    # Relaying an instruction is not a personal-data claim.
    assert statement["personal_data_claim"] is False


def test_invented_note_alias_is_stripped_and_claim_omitted():
    reply = "Her glucose trend has clearly worsened this month [C9]"

    sanitized, evidence = _finalize(reply)

    # [C9] doesn't exist: the alias is removed and the personal claim, now
    # uncited, is omitted exactly like any other unsupported statement.
    assert "[C9]" not in sanitized
    assert "worsened" not in sanitized
    assert evidence["omissions"]["count"] == 1


def test_note_citation_does_not_launder_data_claims():
    """Citing a note plus making a data inference stays a data classification —
    the note alias must not downgrade the evidence bar for [E#] content."""
    context = {
        "evidence_items": [{
            "alias": "E1", "id": "entity:GlucoseReading:x", "domain": "glucose",
            "entity_type": "GlucoseReading", "confidence": {},
            "source_ids": [], "source_links": [],
        }],
        "bundle": {"id": "b", "version": "2.0.0"},
    }
    reply = "Following Bill's protocol [C1], her average glucose decreased compared to last week [E1]"
    sanitized, evidence = companion_evidence.finalize_reply(reply, context, [], [], NOTES)

    assert "[C1]" in sanitized and "[E1]" in sanitized
    assert evidence["statements"][0]["classification"] in {"calculation", "observation"}


def test_care_note_aliases_skip_empty_and_cap_text():
    notes = [
        {"id": "a", "title": "", "body": ""},
        {"id": "b", "title": "Real", "body": "x" * 5000, "author_name": "Bill"},
    ]
    aliases = companion_evidence.care_note_aliases(notes)
    assert [item["alias"] for item in aliases] == ["C1"]
    assert len(aliases[0]["body"]) == 1200


def test_prompt_carries_notes_with_aliases_and_instruction():
    prompt = companion._reply_prompt(
        "What did Bill say about my basal?",
        {"items": []},
        [],
        [],
        care_notes=NOTES,
    )

    assert "CARE TEAM NOTES" in prompt
    assert "[C1] [protocol] Overnight basal protocol — Bill (2026-08-01)" in prompt
    assert "Reduce basal 10% after evening exercise." in prompt
    assert "cite its exact [C#] alias" in prompt


def test_prompt_note_limit_and_zero_notes():
    limited = companion._reply_prompt(
        "q", {"items": []}, [], [], care_notes=NOTES, note_limit=1
    )
    assert "[C1]" in limited and "Quarterly labs" not in limited

    bare = companion._reply_prompt(
        "q", {"items": []}, [], [], care_notes=NOTES, note_limit=0
    )
    assert "CARE TEAM NOTES" not in bare


def test_budget_ladder_final_step_drops_notes():
    assert companion._PROMPT_BUDGET_STEPS[0]["note_limit"] == companion.PROMPT_NOTE_LIMIT
    assert companion._PROMPT_BUDGET_STEPS[-1]["note_limit"] == 0
