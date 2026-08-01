"""Prompt budgeting against the serving model's real context window.

The Companion broke in production with a 502 from the model: 15,185 input +
1,200 output tokens against a 16,384 window. A 96,000-character guard was in
place and never fired, because a character count cannot predict a token count —
prose runs about 5 chars/token while the dense JSON of an evidence bundle runs
under 2, so one ratio misjudges a mixed prompt by more than 2x.

These tests pin the behaviour that replaced it: ask the model how many tokens a
prompt actually is, and shed optional context until it fits.
"""

import asyncio

import pytest

from server import companion, health_summary


@pytest.fixture
def fake_model(monkeypatch):
    """Stand in for the serving model's /v1/models and /tokenize endpoints."""

    state = {"limit": 16384, "chars_per_token": 2.0, "counted": []}

    async def limit(*_args, **_kwargs):
        return state["limit"]

    async def count(text, *_args, **_kwargs):
        tokens = int(len(text) / state["chars_per_token"])
        state["counted"].append(tokens)
        return tokens

    for module in (companion, health_summary):
        monkeypatch.setattr(module, "context_limit", limit)
        monkeypatch.setattr(module, "count_tokens", count)
    return state


def _prompt_args(memories=60, history_turns=40, sources=6):
    return {
        "user_msg": "What changed this week?",
        "evidence_context": {"items": [], "contract_version": "test/1.0.0"},
        "memories": [
            {"id": f"m{index}", "category": "observation", "content": "m" * 300}
            for index in range(memories)
        ],
        "history": [
            {"role": "user" if index % 2 == 0 else "assistant", "content": "h" * 800}
            for index in range(history_turns)
        ],
        "sources": [
            {
                "title": f"Source {index}",
                "url": f"https://medlineplus.gov/{index}",
                "source": "MedlinePlus",
                "snippet": "s" * 900,
            }
            for index in range(sources)
        ],
        "metrics": {"glucose": {"tir": 68}},
    }


def _fit(**overrides):
    args = _prompt_args(**overrides.pop("prompt_args", {}))
    args.update(overrides)
    return asyncio.run(companion._fitted_reply_prompt(
        args["user_msg"],
        args["evidence_context"],
        args["memories"],
        args["history"],
        args["sources"],
        args["metrics"],
    ))


def test_prompt_is_trimmed_until_it_fits(fake_model):
    prompt = _fit()
    used = int(len(prompt) / fake_model["chars_per_token"])

    budget = fake_model["limit"] - companion.REPLY_MAX_TOKENS - companion.CONTEXT_SAFETY_MARGIN
    assert used <= budget


def test_the_question_survives_every_trim(fake_model):
    """Whatever else is shed, the thing Emily actually asked must remain."""
    fake_model["limit"] = 4096  # force the most aggressive step

    prompt = _fit()

    assert "What changed this week?" in prompt
    assert prompt.rstrip().endswith("Companion:")


def test_web_sources_are_dropped_before_conversation_history(fake_model):
    """Sources are general reference; history is the thread of the conversation."""
    # A budget that fits everything except the web-source block.
    full = companion._reply_prompt(**_prompt_args())
    without_sources = companion._reply_prompt(**_prompt_args(), keep_sources=False)
    fake_model["limit"] = (
        int(len(without_sources) / fake_model["chars_per_token"])
        + companion.REPLY_MAX_TOKENS
        + companion.CONTEXT_SAFETY_MARGIN
    )
    assert len(without_sources) < len(full)

    prompt = _fit()

    assert "TRUSTED GENERAL MEDICAL SOURCES" not in prompt
    assert "RECENT CONVERSATION" in prompt
    assert "h" * 800 in prompt  # history text still present


def test_unbudgetable_server_sends_the_prompt_unchanged(monkeypatch):
    """Ollama exposes no max_model_len; guessing a limit would trim needlessly."""

    async def no_limit(*_args, **_kwargs):
        return None

    async def unused_count(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("must not tokenize when no limit is advertised")

    monkeypatch.setattr(companion, "context_limit", no_limit)
    monkeypatch.setattr(companion, "count_tokens", unused_count)

    prompt = _fit()

    assert "TRUSTED GENERAL MEDICAL SOURCES" in prompt


def test_missing_tokenizer_does_not_fabricate_a_count(fake_model, monkeypatch):
    async def no_count(*_args, **_kwargs):
        return None

    monkeypatch.setattr(companion, "count_tokens", no_count)

    prompt = _fit()

    assert "What changed this week?" in prompt


def test_irreducible_evidence_raises_instead_of_a_502(fake_model):
    """Better a clear error than a request the model is certain to reject."""
    fake_model["limit"] = 2048
    huge = {"items": [{"id": f"e{index}", "blob": "x" * 4_000} for index in range(40)]}

    with pytest.raises(Exception) as excinfo:
        _fit(evidence_context=huge)

    assert "context window" in str(excinfo.value)


def test_health_summary_sheds_evidence_items_to_fit(fake_model):
    context = {
        "window_days": 90,
        "glucose": {"tir": 68},
        "shared_evidence_context": {
            "contract_version": "test/1.0.0",
            "items": [{"id": f"e{index}", "blob": "x" * 900} for index in range(184)],
        },
    }

    prompt = asyncio.run(health_summary._fitted_prompt(context))
    used = int(len(prompt) / fake_model["chars_per_token"])
    budget = (
        fake_model["limit"]
        - health_summary.SUMMARY_MAX_TOKENS
        - health_summary.CONTEXT_SAFETY_MARGIN
    )

    assert used <= budget
    # The trim is disclosed in the payload rather than silently applied.
    assert "items_truncated" in prompt


def test_health_summary_keeps_the_most_relevant_items(fake_model):
    """Items arrive most-relevant-first, so the tail is what may be dropped."""
    context = {
        "shared_evidence_context": {
            "items": [{"id": f"e{index}", "blob": "x" * 900} for index in range(184)],
        },
    }

    prompt = asyncio.run(health_summary._fitted_prompt(context))

    assert '"e0"' in prompt
    assert '"e183"' not in prompt
