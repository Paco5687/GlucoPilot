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
    assert [scope["scope"] for scope in first_public["scopes"]] == ["labs_records", "key_labs"]
    assert first_public["budget"]["configured_items"] == 24 + companion_evidence.KEY_LABS.budget
    assert first_public["budget"]["prompt_items"] <= 24 + companion_evidence.KEY_LABS.budget
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


def test_long_question_stays_within_bundle_intent_limit(companion_database):
    question = (
        "My body seems like it is not matching my CGM readings lately, and I "
        "want to understand why my diagnosis, medication, symptom history and "
        "lab results might explain it. "
    ) * 4
    assert len(question) > 500

    public, _reasoning = companion_evidence.build_context(
        question,
        as_of=date(2026, 7, 20),
        refresh=False,
    )

    assert {"metabolic", "clinical", "labs_records"} <= {
        scope["scope"] for scope in public["scopes"]
    }
    for scope in companion_evidence.SCOPES:
        intent = companion_evidence._scope_intent(scope, question[:500])
        assert len(intent) <= companion_evidence.MAX_INTENT_CHARS


def test_companion_receives_source_staleness_as_operational_context(
    companion_database,
    monkeypatch,
):
    operational = {
        "contract_version": "platform-diagnostics/1.0.0",
        "semantics": {
            "category": "operational_diagnostics",
            "not_health_findings": True,
            "message": "Synthetic operational boundary.",
        },
        "sources": [{
            "source": "oura",
            "label": "Oura",
            "status": "stale",
            "last_successful_sync_at": "2026-06-01T12:00:00Z",
            "data_through": "2026-06-01T00:00:00Z",
            "freshness_days": 49.0,
            "issues": [],
        }],
        "caveats": [{
            "code": "source_data_stale",
            "severity": "warning",
            "message": "Oura source data are stale.",
            "category": "source_health",
            "scope": "oura",
            "domain": "source:oura",
        }],
    }
    monkeypatch.setattr(companion_evidence, "diagnostic_context", lambda **_kwargs: operational)

    public, reasoning = companion_evidence.build_context(
        "How is my sleep?",
        as_of=date(2026, 7, 20),
        refresh=False,
    )

    assert public["source_diagnostics"] == operational["sources"]
    assert reasoning["source_diagnostics"] == operational["sources"]
    assert public["operational_semantics"]["not_health_findings"] is True
    assert operational["caveats"][0] in public["missing_data_caveats"]
    assert operational["caveats"][0] in reasoning["missing_data_caveats"]


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
    assert "I don't have bounded personal evidence" not in reply
    assert reply.count("Some generated statements were omitted") == 1
    assert evidence["omissions"] == {
        "count": 1,
        "reason": "missing_personal_evidence_citation",
    }
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


def test_uncited_personal_claims_are_omitted_with_one_quiet_notice(
    companion_database,
):
    public, _reasoning = companion_evidence.build_context(
        "What do my thyroid labs show?",
        as_of=date(2026, 7, 20),
        refresh=False,
    )
    reply, evidence = companion_evidence.finalize_reply(
        "\n".join(
            [
                "Your thyroid result was high.",
                "Your sleep trend was low.",
                "Your glucose pattern increased.",
            ]
        ),
        public,
        [],
        [],
    )

    assert reply == (
        "Some generated statements were omitted because they could not be "
        "linked to your records."
    )
    assert evidence["omissions"]["count"] == 3
    assert evidence["statements"] == []


def test_parenthesized_and_spaced_alias_groups_count_as_citations(
    companion_database,
):
    public, _reasoning = companion_evidence.build_context(
        "What do my thyroid labs show?",
        as_of=date(2026, 7, 20),
        refresh=False,
    )
    alias = public["evidence_items"][0]["alias"]
    memories = [
        {"id": "m1", "content": "Reports brain fog.", "category": "symptom"},
        {"id": "m2", "content": "Reports poor sleep.", "category": "symptom"},
    ]
    reply, evidence = companion_evidence.finalize_reply(
        "\n".join(
            [
                f"Your thyroid result was high ({alias}).",
                f"Your sleep pattern was low [ {alias} ].",
                "Your symptom trend increased (M1, M2).",
                f"Your glucose pattern increased ({alias}/M1).",
                "Your thyroid is definitely failing (E999).",
            ]
        ),
        public,
        memories,
        [],
    )

    assert f"Your thyroid result was high [{alias}]." in reply
    assert f"Your sleep pattern was low [{alias}]." in reply
    assert "Your symptom trend increased [M1] [M2]." in reply
    assert f"Your glucose pattern increased [{alias}] [M1]." in reply
    assert "definitely failing" not in reply
    assert evidence["omissions"]["count"] == 1
    assert all(
        statement["evidence_aliases"] or statement["memory_ids"]
        for statement in evidence["statements"]
    )


def test_hormone_abbreviations_are_not_read_as_citations(companion_database):
    public, _reasoning = companion_evidence.build_context(
        "What do my thyroid labs show?",
        as_of=date(2026, 7, 20),
        refresh=False,
    )
    assert any(item["alias"] == "E2" for item in public["evidence_items"])
    reply, evidence = companion_evidence.finalize_reply(
        "Your estradiol (E2) level was low.",
        public,
        [],
        [],
    )

    assert "estradiol" not in reply
    assert evidence["omissions"]["count"] == 1
    assert evidence["statements"] == []


def test_companion_retries_once_and_keeps_the_better_grounded_reply(
    companion_database,
    monkeypatch,
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

    async def repaired_reply(*_args, **_kwargs):
        return f"Your thyroid result was high. [{lab['alias']}]"

    monkeypatch.setattr(companion, "invoke_llm", repaired_reply)
    reply, evidence = asyncio.run(
        companion._finalize_grounded_reply(
            "Your thyroid result was high.",
            public,
            [],
            [],
        )
    )

    assert reply.startswith("Unverified machine-extracted lab evidence:")
    assert "statements were omitted" not in reply
    assert evidence["grounding_retry"] == {
        "attempted": True,
        "improved": True,
        "initial_omissions": 1,
        "remaining_omissions": 0,
    }


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

    # Per-section caps still bound the prompt at assembly. The overall size is
    # enforced separately by _fitted_reply_prompt, which budgets in tokens the
    # serving model counts rather than characters — see test_prompt_budget.
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


def test_long_thread_sends_the_latest_turns_not_the_first(
    companion_database,
    monkeypatch,
):
    public, reasoning = companion_evidence.build_context(
        "What do my thyroid labs show?",
        as_of=date(2026, 7, 20),
        refresh=False,
    )
    monkeypatch.setattr(companion, "_grounding", lambda _text: (public, reasoning, {}))
    monkeypatch.setattr(companion, "_grounding_enabled", lambda: False)
    thread_id = companion._new_thread("Long chat")["id"]
    for index in range(companion.HISTORY_TURNS * 3):
        companion._entity("ChatMessage").create({
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"turn-marker-{index:02d}",
            "thread_id": thread_id,
            "actor": "owner",
            "created_date": f"2026-07-20T10:{index:02d}:00.000Z",
            "owner_email": companion.OWNER_EMAIL,
        })
    prompts: list[str] = []

    async def fake_stream(prompt, *_args, **_kwargs):
        prompts.append(prompt)
        yield "Thyroid hormones affect metabolism."

    async def no_memories(*_args, **_kwargs):
        return []

    monkeypatch.setattr(companion, "invoke_llm_stream", fake_stream)
    monkeypatch.setattr(companion, "_store_new_memories", no_memories)

    async def collect():
        return [
            json.loads(line)
            async for line in companion.stream_send("And the newest question?", thread_id=thread_id)
        ]

    asyncio.run(collect())
    last = companion.HISTORY_TURNS * 3 - 1
    assert f"turn-marker-{last:02d}" in prompts[0]
    assert "turn-marker-00" not in prompts[0]
    history = companion._thread_history(thread_id)
    assert [m["content"] for m in history[-2:]] == [
        "And the newest question?",
        "Thyroid hormones affect metabolism.",
    ]
    assert [m["created_date"] for m in history] == sorted(m["created_date"] for m in history)


def _stub_reply_pipeline(monkeypatch, public, reasoning, chunks, *, finish_reason=None):
    monkeypatch.setattr(companion, "_grounding", lambda _text: (public, reasoning, {}))
    monkeypatch.setattr(companion, "_grounding_enabled", lambda: False)
    calls: list[str] = []

    async def fake_stream(prompt, *_args, **kwargs):
        calls.append(prompt)
        if finish_reason and kwargs.get("meta") is not None:
            kwargs["meta"]["finish_reason"] = finish_reason
        for chunk in chunks:
            yield chunk

    monkeypatch.setattr(companion, "invoke_llm_stream", fake_stream)
    return calls


def _collect(text, **kwargs):
    async def run():
        return [json.loads(line) async for line in companion.stream_send(text, **kwargs)]

    return asyncio.run(run())


def test_crisis_message_gets_the_fixed_reply_without_the_model(companion_database, monkeypatch):
    public, reasoning = companion_evidence.build_context(
        "What do my thyroid labs show?", as_of=date(2026, 7, 20), refresh=False
    )
    calls = _stub_reply_pipeline(monkeypatch, public, reasoning, ["model output"])
    extracted: list[str] = []

    async def spy_extract(*args, **_kwargs):
        extracted.append(args[0])
        return []

    monkeypatch.setattr(companion, "_extract_memories", spy_extract)

    events = _collect("I don't want to be here anymore")

    assert calls == []
    assert extracted == []
    reply = "".join(event.get("delta", "") for event in events)
    assert reply == companion.companion_safety.CRISIS_REPLY
    assert events[-1] == {"done": True, "remembered": [], "thread_id": events[0]["thread"]["id"]}
    history = companion._thread_history(events[0]["thread"]["id"])
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[-1]["content"] == companion.companion_safety.CRISIS_REPLY


def test_provider_clinical_language_still_reaches_the_model(companion_database, monkeypatch):
    public, reasoning = companion_evidence.build_context(
        "What do my thyroid labs show?", as_of=date(2026, 7, 20), refresh=False
    )
    calls = _stub_reply_pipeline(
        monkeypatch, public, reasoning, ["Thyroid hormones affect mood and energy."]
    )

    events = _collect(
        "Has she reported suicidal ideation alongside the thyroid changes?",
        actor="provider:drsynthetic",
    )

    assert len(calls) == 1
    assert "Thyroid hormones affect mood" in "".join(e.get("delta", "") for e in events)


def test_treatment_change_lines_are_guarded_out_of_replies(companion_database, monkeypatch):
    public, reasoning = companion_evidence.build_context(
        "What do my thyroid labs show?", as_of=date(2026, 7, 20), refresh=False
    )
    lab = next(
        item for item in public["evidence_items"]
        if item["entity_id"] == companion_database["lab"]["id"]
    )
    _stub_reply_pipeline(
        monkeypatch,
        public,
        reasoning,
        [
            f"Your thyroid result was high. [{lab['alias']}]\n",
            "Try adding ashwagandha daily to support your thyroid.\n",
            "Thyroid hormones affect metabolism.",
        ],
    )

    async def no_memories(*_args, **_kwargs):
        return []

    monkeypatch.setattr(companion, "_store_new_memories", no_memories)
    events = _collect("What do my thyroid labs show?")
    reply = "".join(e.get("delta", "") for e in events)
    evidence = next(e["evidence"] for e in events if e.get("evidence"))

    assert "ashwagandha" not in reply
    assert "Thyroid hormones affect metabolism." in reply
    assert companion.companion_safety.CARE_TEAM_NOTE in reply
    assert evidence["safety_guard"] == {"guarded_lines": 1}


def test_reply_cut_off_at_the_token_cap_ends_on_a_full_sentence(companion_database, monkeypatch):
    public, reasoning = companion_evidence.build_context(
        "What do my thyroid labs show?", as_of=date(2026, 7, 20), refresh=False
    )
    _stub_reply_pipeline(
        monkeypatch,
        public,
        reasoning,
        ["Thyroid hormones affect metabolism. They also affect ", "energy and mo"],
        finish_reason="length",
    )

    async def no_memories(*_args, **_kwargs):
        return []

    monkeypatch.setattr(companion, "_store_new_memories", no_memories)
    events = _collect("What do my thyroid labs show?")

    assert "".join(e.get("delta", "") for e in events) == "Thyroid hormones affect metabolism."


def test_only_memories_grounded_in_her_message_are_stored(companion_database, monkeypatch):
    async def fake_extract(_text, _existing):
        return [
            {"content": "Reports puffy hands in the mornings.", "category": "symptom"},
            {"content": "Reports significant obesity.", "category": "nutrition_routine"},
            {"content": "Reports brutal work stress this month.", "category": "personal_hygiene/training"},
        ]

    monkeypatch.setattr(companion, "_extract_memories", fake_extract)
    message = "Work stress has been brutal this month and my hands are puffy in the mornings"

    stored = asyncio.run(companion._store_new_memories(message, []))

    assert stored == [
        "Reports puffy hands in the mornings.",
        "Reports brutal work stress this month.",
    ]
    categories = {m["content"]: m["category"] for m in companion._memories()}
    assert categories["Reports brutal work stress this month."] == "observation"
    assert asyncio.run(companion._store_new_memories("Please", [])) == []


def test_unverified_lab_headings_are_marked_at_the_end(companion_database):
    public, _reasoning = companion_evidence.build_context(
        "What do my thyroid labs show?", as_of=date(2026, 7, 20), refresh=False
    )
    lab = next(
        item for item in public["evidence_items"]
        if item["entity_id"] == companion_database["lab"]["id"]
    )
    reply, _evidence = companion_evidence.finalize_reply(
        f"**Your thyroid result was high [{lab['alias']}]**", public, [], []
    )

    assert reply == f"**Your thyroid result was high [{lab['alias']}]** (unverified lab)"


def test_lone_questions_are_not_mined_for_memories(companion_database, monkeypatch):
    async def fake_extract(_text, _existing):
        return [{"content": "Read MRI images and thinking about them.", "category": "observation"}]

    monkeypatch.setattr(companion, "_extract_memories", fake_extract)

    assert asyncio.run(
        companion._store_new_memories("What do you think about my MRI images?", [])
    ) == []


def test_labs_are_called_low_or_high_only_when_the_lab_flagged_them(companion_database):
    db.create_entity(
        "LabResult",
        {
            "owner_email": OWNER,
            "record_id": companion_database["lab"]["record_id"],
            "collected_date": "2026-07-10",
            "test_name": "Synthetic Free T3",
            "value": 2.4,
            "unit": "pg/mL",
            "flag": "",
            "parser_confidence": 0.99,
            "verification_status": "unverified",
            "validation_status": "valid",
        },
    )
    clear_bundle_cache()
    public, _reasoning = companion_evidence.build_context(
        "What do my thyroid labs show?", as_of=date(2026, 7, 20), refresh=False
    )
    by_title = {item["title"]: item["alias"] for item in public["evidence_items"]}
    free_t3, tsh = by_title["Synthetic Free T3"], by_title["Synthetic TSH"]

    reply, evidence = companion_evidence.finalize_reply(
        "\n".join(
            [
                f"Your free T3 of 2.4 is low. [{free_t3}]",
                f"Your free T3 was not low. [{free_t3}]",
                f"Your TSH was high. [{tsh}]",
            ]
        ),
        public,
        [],
        [],
    )

    assert "is low" not in reply
    assert "was not low" in reply
    assert "TSH was high" in reply
    assert evidence["omissions"]["count"] == 1


def test_messages_that_only_ask_questions_are_not_mined(companion_database, monkeypatch):
    async def fake_extract(_text, _existing):
        return [{"content": "Reports Armour dose being the issue.", "category": "treatment"}]

    monkeypatch.setattr(companion, "_extract_memories", fake_extract)

    assert asyncio.run(
        companion._store_new_memories("How is my thyroid doing? Is my Armour dose right?", [])
    ) == []


def test_glucose_claims_must_cite_glucose_evidence(companion_database):
    public, _reasoning = companion_evidence.build_context(
        "What do my glucose, thyroid lab and diagnosis history show?",
        as_of=date(2026, 7, 20),
        refresh=False,
    )
    alias_of = {item["entity_id"]: item["alias"] for item in public["evidence_items"]}
    glucose = alias_of[companion_database["glucose"]["id"]]
    diagnosis = alias_of[companion_database["diagnosis"]["id"]]
    lab = alias_of[companion_database["lab"]["id"]]

    reply, evidence = companion_evidence.finalize_reply(
        "\n".join(
            [
                f"Your bedtime glucose was above 150 on most nights. [{diagnosis}]",
                f"Your glucose was 123 at noon. [{glucose}]",
                f"Your thyroid result fits hypothyroidism. [{lab}]",
            ]
        ),
        public,
        [],
        [],
    )

    assert "above 150" not in reply
    assert "123 at noon" in reply
    assert "hypothyroidism" in reply
    assert evidence["omissions"]["count"] == 1


def test_scope_keywords_match_plurals_and_everyday_words():
    names = lambda question: [scope.name for scope in companion_evidence._selected_scopes(question)]
    assert "metabolic" in names("If I eat dinner late I'll be high at bedtime")
    assert "wellness" in names("If I eat dinner late I'll be high at bedtime")
    assert "metabolic" in names("why do my sugars keep rising")
    assert "labs_records" in names("Do i have elevated liver enzymes?")
    assert names("Is my free T4 okay on Armour?")[0] in {"clinical", "labs_records"}
    assert "metabolic" not in names("Is my ferritin low?")


def test_key_labs_ride_along_with_the_labs_scope(companion_database):
    public, _reasoning = companion_evidence.build_context(
        "Do my test results explain my nerves?",
        as_of=date(2026, 7, 20),
        refresh=False,
    )
    key_scope = next(scope for scope in public["scopes"] if scope["scope"] == "key_labs")
    assert key_scope["query"]["question_intent"] == companion_evidence.KEY_LAB_INTENT
    titles = [item["title"] for item in public["evidence_items"]]
    assert "Synthetic TSH" in titles


def test_short_follow_ups_retrieve_evidence_for_the_previous_question(
    companion_database, monkeypatch
):
    public, reasoning = companion_evidence.build_context(
        "What do my thyroid labs show?", as_of=date(2026, 7, 20), refresh=False
    )
    asked: list[str] = []

    def spy_grounding(text):
        asked.append(text)
        return public, reasoning, {}

    _stub_reply_pipeline(monkeypatch, public, reasoning, ["Thyroid hormones affect metabolism."])
    monkeypatch.setattr(companion, "_grounding", spy_grounding)

    async def no_memories(*_args, **_kwargs):
        return []

    monkeypatch.setattr(companion, "_store_new_memories", no_memories)
    first = _collect("What do my thyroid labs show over the last year or so?")
    thread_id = first[0]["thread"]["id"]
    prompts_before = len(asked)
    _collect("Please", thread_id=thread_id)

    assert asked[0] == "What do my thyroid labs show over the last year or so?"
    assert asked[prompts_before] == "Please What do my thyroid labs show over the last year or so?"


def test_claims_that_labs_are_missing_must_be_true(companion_database):
    public, _reasoning = companion_evidence.build_context(
        "What do my thyroid labs show?", as_of=date(2026, 7, 20), refresh=False
    )
    reply, evidence = companion_evidence.finalize_reply(
        "\n".join(
            [
                "We don't have updated thyroid labs to confirm if Armour is working.",
                "I don't see a ferritin result in what I was given.",
                "I don't see recent liver labs in what I have.",
                "We don't have any recent labs.",
                "No lab shows adrenal problems.",
            ]
        ),
        public,
        [],
        [],
    )
    assert "thyroid labs to confirm" not in reply
    assert "ferritin result" in reply
    assert "recent liver labs" in reply
    assert "any recent labs" not in reply
    assert "No lab shows adrenal problems." in reply
    assert evidence["omissions"]["count"] == 2

    glucose_only, _reasoning = companion_evidence.build_context(
        "How was my glucose overnight?", as_of=date(2026, 7, 20), refresh=False
    )
    reply, _evidence = companion_evidence.finalize_reply(
        "We don't see recent labs on cortisol, ferritin, or B12.", glucose_only, [], []
    )
    assert "cortisol" not in reply


def test_each_sentence_is_judged_by_its_own_citations(companion_database):
    db.create_entity(
        "LabResult",
        {
            "owner_email": OWNER,
            "record_id": companion_database["lab"]["record_id"],
            "collected_date": "2026-07-10",
            "test_name": "Synthetic Free T3",
            "value": 2.4,
            "unit": "pg/mL",
            "flag": "",
            "parser_confidence": 0.99,
            "verification_status": "unverified",
            "validation_status": "valid",
        },
    )
    clear_bundle_cache()
    public, _reasoning = companion_evidence.build_context(
        "What do my thyroid labs show?", as_of=date(2026, 7, 20), refresh=False
    )
    by_title = {item["title"]: item["alias"] for item in public["evidence_items"]}
    tsh, free_t3 = by_title["Synthetic TSH"], by_title["Synthetic Free T3"]

    reply, evidence = companion_evidence.finalize_reply(
        f"Your TSH was 7.1 (flagged high) [{tsh}]. "
        f"Your free T3 was 2.4 (flagged low) [{free_t3}]. "
        "Thyroid hormones affect energy. We don't have HRV data, and we don't have recent TSH labs.",
        public,
        [],
        [],
    )

    assert "TSH was 7.1 (flagged high)" in reply
    assert "free T3 was 2.4" not in reply
    assert "Thyroid hormones affect energy." in reply
    assert "recent TSH labs" not in reply
    assert evidence["omissions"]["count"] == 2


def test_curly_apostrophe_absence_claims_are_checked(companion_database):
    public, _reasoning = companion_evidence.build_context(
        "What do my thyroid labs show?", as_of=date(2026, 7, 20), refresh=False
    )
    reply, evidence = companion_evidence.finalize_reply(
        "Thyroid hormones affect energy. We don’t have recent T4 or TSH trends to confirm it.",
        public,
        [],
        [],
    )
    assert reply.startswith("Thyroid hormones affect energy.")
    assert "TSH trends" not in reply
    assert evidence["omissions"]["count"] == 1


def test_above_the_normal_range_counts_as_a_lab_judgement(companion_database):
    db.create_entity(
        "LabResult",
        {
            "owner_email": OWNER,
            "record_id": companion_database["lab"]["record_id"],
            "collected_date": "2026-07-10",
            "test_name": "Synthetic Free T4",
            "value": 1.0,
            "unit": "ng/dL",
            "flag": "",
            "parser_confidence": 0.99,
            "verification_status": "unverified",
            "validation_status": "valid",
        },
    )
    clear_bundle_cache()
    public, _reasoning = companion_evidence.build_context(
        "What do my thyroid labs show?", as_of=date(2026, 7, 20), refresh=False
    )
    free_t4 = next(i["alias"] for i in public["evidence_items"] if i["title"] == "Synthetic Free T4")
    reply, _evidence = companion_evidence.finalize_reply(
        f"Your free T4 was 1.0 [{free_t4}], which is above the normal range (0.8–0.9).", public, [], []
    )
    assert "above the normal range" not in reply


def test_glucose_stats_label_cv_stability():
    stats = companion._tir([100, 110, 120, 130, 90, 140])
    assert stats["cv"] <= 36
    assert stats["cv_stable"] is True


@pytest.mark.parametrize(
    "sentence",
    [
        "Your free T4 was 1.0 ng/dL in August, below the lab's reference range [{alias}].",
        "Your free T4 of 1.0 is considered low by some standards [{alias}].",
    ],
)
def test_lab_judgements_with_a_value_and_unit_in_between_are_checked(companion_database, sentence):
    db.create_entity(
        "LabResult",
        {
            "owner_email": OWNER,
            "record_id": companion_database["lab"]["record_id"],
            "collected_date": "2026-07-10",
            "test_name": "Synthetic Free T4",
            "value": 1.0,
            "unit": "ng/dL",
            "reference_low": 0.8,
            "reference_high": 1.8,
            "flag": "",
            "parser_confidence": 0.99,
            "verification_status": "unverified",
            "validation_status": "valid",
        },
    )
    clear_bundle_cache()
    public, _reasoning = companion_evidence.build_context(
        "What do my thyroid labs show?", as_of=date(2026, 7, 20), refresh=False
    )
    free_t4 = next(i["alias"] for i in public["evidence_items"] if i["title"] == "Synthetic Free T4")
    reply, evidence = companion_evidence.finalize_reply(sentence.format(alias=free_t4), public, [], [])
    assert "free T4" not in reply
    assert evidence["omissions"]["count"] == 1
