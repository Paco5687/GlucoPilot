from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date

import pytest

from server import companion, companion_evidence, db
from server.evidence_bundle import clear_bundle_cache
from server.migrations import run_migrations
from server.repositories import LegacyRepositoryCatalog


pytestmark = pytest.mark.risk_critical
OWNER = "owner@glucopilot.local"


@pytest.fixture
def companion_database(tmp_path, monkeypatch):
    database = tmp_path / "data" / "app.sqlite3"
    database.parent.mkdir()
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(
        companion_evidence,
        "refresh_contradictions",
        lambda: {"enabled": True, "refreshed": False},
    )
    monkeypatch.setenv("RELATIONSHIP_READS_ENABLED", "false")
    monkeypatch.setenv("EVIDENCE_SET_READS_ENABLED", "false")
    clear_bundle_cache()
    record = db.create_entity(
        "MedicalRecord",
        {
            "owner_email": OWNER,
            "record_date": "2026-07-10",
            "title": "Synthetic thyroid report",
            "filename": "synthetic.pdf",
            "stored_as": "private-path-must-not-leak.pdf",
        },
    )
    lab = db.create_entity(
        "LabResult",
        {
            "owner_email": OWNER,
            "record_id": record["id"],
            "collected_date": "2026-07-10",
            "test_name": "Synthetic TSH",
            "value": 7.1,
            "unit": "mIU/L",
            "flag": "high",
            "parser_confidence": 0.99,
            "verification_status": "unverified",
            "validation_status": "valid",
            "source_page": 2,
            "api_token": "secret-must-not-leak",
        },
    )
    diagnosis = db.create_entity(
        "Diagnosis",
        {
            "owner_email": OWNER,
            "name": "Synthetic thyroid condition",
            "diagnosed_date": "2024-01-01",
        },
    )
    glucose = db.create_entity(
        "GlucoseReading",
        {
            "owner_email": OWNER,
            "timestamp": "2026-07-19T12:00:00Z",
            "value": 123,
            "source": "synthetic-cgm",
        },
    )
    treatment = db.create_entity(
        "Treatment",
        {
            "owner_email": OWNER,
            "timestamp": "2026-07-19T12:05:00Z",
            "type": "insulin",
            "amount": 2.5,
        },
    )
    yield {
        "database": database,
        "record": record,
        "lab": lab,
        "diagnosis": diagnosis,
        "glucose": glucose,
        "treatment": treatment,
    }
    clear_bundle_cache()


def test_question_ranked_context_is_bounded_deterministic_and_secret_scrubbed(
    companion_database,
):
    first_public, first_reasoning = companion_evidence.build_context(
        "What do my thyroid labs show?",
        as_of=date(2026, 7, 20),
        refresh=False,
    )
    second_public, second_reasoning = companion_evidence.build_context(
        "What do my thyroid labs show?",
        as_of=date(2026, 7, 20),
        refresh=False,
    )

    assert first_public == second_public
    assert first_reasoning == second_reasoning
    assert first_public["contract_version"] == "companion-evidence-context/1.0.0"
    assert [scope["scope"] for scope in first_public["scopes"]] == ["labs_records"]
    assert first_public["budget"]["configured_items"] == 24
    assert first_public["budget"]["prompt_items"] <= 24
    assert len(companion_evidence.prompt_context(first_reasoning)) <= (
        companion_evidence.MAX_PROMPT_CONTEXT_CHARS
    )
    lab = next(
        item for item in first_public["evidence_items"]
        if item["entity_id"] == companion_database["lab"]["id"]
    )
    assert lab["confidence"]["label"] == "unverified"
    assert lab["confidence"]["clinically_verified"] is False
    assert lab["source_ids"]
    encoded = json.dumps(first_reasoning, sort_keys=True)
    assert "secret-must-not-leak" not in encoded
    assert "private-path-must-not-leak.pdf" not in encoded


def test_scope_balancing_keeps_question_relevant_source_types(companion_database):
    whole, _reasoning = companion_evidence.build_context(
        "whole person health context",
        as_of=date(2026, 7, 20),
        refresh=False,
    )
    whole_types = {item["entity_type"] for item in whole["evidence_items"]}
    assert {"GlucoseReading", "LabResult", "MedicalRecord", "Diagnosis"} <= whole_types

    glucose, _reasoning = companion_evidence.build_context(
        "Compare my time in range this week with last week.",
        as_of=date(2026, 7, 20),
        refresh=False,
    )
    assert [scope["scope"] for scope in glucose["scopes"]] == [
        "metabolic", "analytics"
    ]
    assert "GlucoseReading" in {
        item["entity_type"] for item in glucose["evidence_items"]
    }

    insulin, _reasoning = companion_evidence.build_context(
        "How has my insulin pump TDD changed?",
        as_of=date(2026, 7, 20),
        refresh=False,
    )
    assert "Treatment" in {
        item["entity_type"] for item in insulin["evidence_items"]
    }


def test_companion_context_distinguishes_clinician_confirmation_and_dispute(
    companion_database,
):
    now = "2026-07-20T12:00:00.000Z"
    with db.connect() as connection:
        connection.execute(
            """
            INSERT INTO clinical_review_threads (
                id,owner_id,target_kind,target_type,target_id,target_label,
                current_action,provider_status,owner_status,current_text,
                evidence_bundle_id,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()),
                "urn:glucopilot:owner:self",
                "hypothesis",
                "HealthHypothesis",
                "synthetic-reviewed-hypothesis",
                "Synthetic reviewed hypothesis",
                "hypothesis_confirm",
                "hypothesis_confirmed",
                "accepted",
                "Synthetic clinician confirmation.",
                None,
                now,
                now,
            ),
        )

    public, reasoning = companion_evidence.build_context(
        "What has my clinician confirmed?",
        as_of=date(2026, 7, 20),
        refresh=False,
    )
    assert public["clinical_reviews"]["clinician_confirmed_facts"][0][
        "semantic_class"
    ] == "clinician_confirmation"
    assert reasoning["clinical_reviews"]["provider_annotations"] == []
    assert "only entries under clinician_confirmed_facts" in companion.SYSTEM


def test_reply_claims_require_valid_links_and_unverified_labs_are_qualified(
    companion_database,
):
    public, _reasoning = companion_evidence.build_context(
        "What do my thyroid labs show?",
        as_of=date(2026, 7, 20),
        refresh=False,
    )
    lab = next(
        item for item in public["evidence_items"]
        if item["entity_id"] == companion_database["lab"]["id"]
    )
    sources = [{
        "title": "Synthetic general reference",
        "source": "Synthetic authority",
        "url": "https://example.test/reference",
        "snippet": "General thyroid background.",
    }]
    reply, evidence = companion_evidence.finalize_reply(
        (
            f"Your thyroid result was high. [{lab['alias']}]\n"
            "Your thyroid is definitely failing. [E999]\n"
            "Thyroid hormones affect metabolism. [G1]"
        ),
        public,
        [],
        sources,
    )

    assert "Unverified machine-extracted lab evidence:" in reply
    assert "[E999]" not in reply
    assert "definitely failing" not in reply
    assert "I don't have bounded personal evidence" in reply
    personal = [
        statement for statement in evidence["statements"]
        if statement["personal_data_claim"]
    ]
    assert len(personal) == 1
    assert personal[0]["classification"] == "observation"
    assert personal[0]["evidence_item_ids"] == [lab["id"]]
    assert personal[0]["source_ids"]
    general = next(
        statement for statement in evidence["statements"]
        if "Thyroid hormones" in statement["text"]
    )
    assert general["classification"] == "general_information"
    assert general["evidence_item_ids"] == []
    assert general["external_source_ids"]


def test_local_model_prompt_bounds_memories_history_and_evidence(companion_database):
    _public, reasoning = companion_evidence.build_context(
        "whole person health context",
        as_of=date(2026, 7, 20),
        refresh=False,
    )
    memories = [
        {
            "id": f"memory_{index}",
            "category": "observation",
            "content": f"Synthetic memory {index} " + "x" * 1_000,
        }
        for index in range(150)
    ]
    history = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": "y" * 4_000}
        for index in range(40)
    ]

    prompt = companion._reply_prompt(
        "What changed?",
        reasoning,
        memories,
        history,
        [],
        {},
    )

    assert len(prompt) <= companion.MAX_REPLY_PROMPT_CHARS
    assert "[M40]" in prompt
    assert "[M41]" not in prompt
    assert prompt.count("Emily: " + "y" * 800) <= companion.HISTORY_TURNS


def test_stream_persists_claim_links_and_evidence_commands(
    companion_database,
    monkeypatch,
):
    public, reasoning = companion_evidence.build_context(
        "What do my thyroid labs show?",
        as_of=date(2026, 7, 20),
        refresh=False,
    )
    lab = next(
        item for item in public["evidence_items"]
        if item["entity_id"] == companion_database["lab"]["id"]
    )
    monkeypatch.setattr(
        companion,
        "_grounding",
        lambda _text: (public, reasoning, {}),
    )
    monkeypatch.setattr(companion, "_grounding_enabled", lambda: False)

    async def fake_stream(*_args, **_kwargs):
        yield f"Your thyroid result was high. [{lab['alias']}]"

    async def no_memories(*_args, **_kwargs):
        return []

    monkeypatch.setattr(companion, "invoke_llm_stream", fake_stream)
    monkeypatch.setattr(companion, "_store_new_memories", no_memories)

    async def collect():
        return [
            json.loads(line)
            async for line in companion.stream_send("What do my thyroid labs show?")
        ]

    events = asyncio.run(collect())
    evidence_event = next(event for event in events if event.get("evidence"))
    message = companion._entity("ChatMessage").get(evidence_event["message_id"])
    assert message["evidence"]["contract_version"] == companion_evidence.CONTRACT_VERSION
    assert message["evidence"]["statements"][0]["evidence_item_ids"] == [lab["id"]]
    assert message["content"].startswith("Unverified machine-extracted lab evidence:")
    assert events[-1]["done"] is True
    references = LegacyRepositoryCatalog().evidence.for_claim(
        OWNER,
        "ChatMessage",
        message["id"],
    )
    assert [reference.evidence_kind for reference in references] == [
        "evidence_bundle_claim"
    ]
    monkeypatch.setenv("EVIDENCE_SET_READS_ENABLED", "true")
    compatibility_references = companion.get_repositories().evidence.for_claim(
        OWNER,
        "ChatMessage",
        message["id"],
    )
    assert [reference.evidence_kind for reference in compatibility_references] == [
        "evidence_bundle_claim"
    ]

    shown = asyncio.run(companion.handle({
        "action": "evidence_command",
        "command": "show",
        "message_id": message["id"],
    }))
    assert shown["bundle"]["id"] == public["bundle"]["id"]
    assert shown["statements"][0]["source_ids"]

    opposing = asyncio.run(companion.handle({
        "action": "evidence_command",
        "command": "opposing",
        "message_id": message["id"],
    }))
    assert opposing["command"] == "opposing"

    monkeypatch.setattr(
        companion_evidence,
        "build_context",
        lambda *_args, **_kwargs: (
            {**public, "bundle": {**public["bundle"], "input_hash": "sha256:" + "f" * 64}},
            reasoning,
        ),
    )
    changes = asyncio.run(companion.handle({
        "action": "evidence_command",
        "command": "changes",
        "message_id": message["id"],
    }))
    assert changes["changed"] is True
    assert changes["changed_scopes"] == []

    foreign = db.create_entity(
        "ChatMessage",
        {
            "owner_email": "foreign@example.test",
            "role": "assistant",
            "content": "Foreign",
            "evidence": message["evidence"],
        },
    )
    denied = asyncio.run(companion.handle({
        "action": "evidence_command",
        "command": "show",
        "message_id": foreign["id"],
    }))
    assert denied["_status"] == 404
