"""Health Companion grounded in bounded Evidence Bundles and lived-experience memory.

Each turn builds a content-addressed, question-ranked Evidence Bundle portfolio,
adds only relevant deterministic metrics and bounded recent conversation, and
requires personal-data statements to cite selected evidence. Durable facts the
person shares remain separate HealthMemory assertions.

Not a doctor: it offers observations and questions for her care team, never
diagnoses or dosing.
"""

import json
import logging
import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from . import care_notes as care_notes_module
from . import companion_evidence, companion_safety, insulin, research
from .auth import require_login, session_actor
from .config import APP_TIMEZONE, OWNER_EMAIL
from .db import config_value
from .data_quality import assess_cgm, cgm_points
from .llm import (
    CONTEXT_SAFETY_MARGIN,
    context_limit,
    count_tokens,
    invoke_llm,
    invoke_llm_stream,
)
from .repositories import EntityRepository, get_repositories
from .unit_of_work import unit_of_work

log = logging.getLogger("glucopilot.companion")

# Providers get their own actor-scoped chats; writes to Emily's memories and
# threads are enforced per-action below, not by locking out the whole router.
router = APIRouter(dependencies=[Depends(require_login)])

MAX_MEMORIES = 150
PROMPT_MEMORY_LIMIT = 40
PROMPT_NOTE_LIMIT = 20  # care-team notes shown to the model, pinned first
HISTORY_TURNS = 8  # exchanges of prior context sent each turn
REPLY_MAX_TOKENS = 1200  # enough for a substantive answer without truncating mid-thought

# The small model likes to sign replies like a letter ("— Emily's Health Companion").
# Stop generation before a dash-led sign-off line (em/en dash only — hyphens are
# bullet lists), and strip any that slips through as a backstop.
SIGNOFF_STOP = ["\n\n—", "\n\n–"]
_SIGNOFF_RE = re.compile(r"\n+\s*[—–-][^\n]*\bCompanion\b\s*$", re.IGNORECASE)


def _strip_signoff(text: str) -> str:
    return _SIGNOFF_RE.sub("", text).rstrip()

SYSTEM = (
    "You are Emily's personal health companion: warm, steady, and honest. Emily lives with Type 1 diabetes, and her "
    "thyroid, hormones and cycle, immune markers, gut, sleep, energy, mood, medications, and daily life all matter as "
    "much as glucose. Follow her actual question wherever it leads instead of steering back to diabetes.\n"
    "HOW TO ANSWER: Answer her question directly in the first one or two sentences. Then give the few facts from her "
    "data that bear on it, with numbers and dates. When an evidence item has findings, it is an analysis of months "
    "of her data: answer from those findings first and cite that item. Keep the whole reply under 250 words: short paragraphs, at most "
    "five bullets, no headings, and never repeat yourself. Write as a natural chat message with no sign-off.\n"
    "CITATIONS: Every sentence that states something about her own data MUST end with one or more exact [E#] aliases "
    "copied from the supplied evidence items. Write every alias in its own square brackets, like [E3] [M2], never in "
    "parentheses like (E3). Never invent or alter an alias. Cite things she told you only with their exact [M#] alias, "
    "care-team notes with their exact [C#] alias (and name the author), and general references only with supplied "
    "[G#] aliases. Machine-extracted lab evidence with clinically_verified=false must always be called unverified.\n"
    "BE CALIBRATED: Separate what her data shows from what it cannot show. Call a lab value high or low only when "
    "its flag or reference range in the evidence says so. If you offer a possible explanation, give "
    "at most two, call them possibilities, and say what test or record would check each one. Never call something "
    "likely, classic, or unmistakable, and never present a diagnosis; diagnoses belong to her care team. If a lab or "
    "record is not in the evidence you were given, say you don't see it in the records you were given, rather than "
    "claiming it was never done. When the evidence holds an unresolved contradiction, show both sides and call it unresolved. "
    "The clinical_reviews block is authoritative: only entries under clinician_confirmed_facts may be described as "
    "clinician-confirmed; provider annotations are attributed notes, and owner_disputed_reviews stay disputed.\n"
    "BE STEADYING: She is often frightened about her health. Use calm, plain language and no alarming metaphors "
    "(nothing like under siege, hijacked, broken, toxic, or damaged). When she predicts something bad will "
    "happen, check it against her data before agreeing; if the data shows it has not happened, say so with the "
    "numbers. Do not build explanations on contested ideas such as leaky gut, adrenal fatigue, or toxins; if her "
    "records contain such a label you may mention that it is in her records.\n"
    "NO TREATMENT CHANGES: Never suggest starting, stopping, or changing a medication, insulin dose, temp basal, "
    "supplement, herb, or diet, and never recommend a practitioner outside her care team. You can explain how her "
    "medications may be affecting her and suggest questions or tests to raise with her care team.\n"
    "SAFETY: If she says she wants to die, hurt herself, or sees no point in going on, stop analyzing: respond with "
    "care and urge her to call or text 988, contact her therapist, or call 911 if she might act on it. 988 is only "
    "for those thoughts, never for blood sugar. If she describes a low happening now, tell her to treat it first and "
    "recheck; a low she cannot treat herself is a 911 emergency. If she describes vomiting, high ketones, or trouble "
    "breathing, tell her to get urgent care."
)


MEMORY_SCHEMA = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "One concise, lasting fact about Emily, in the third person (e.g. 'Reports migraines clustering in the days before her period')."},
                    "category": {"type": "string", "description": "symptom | life_context | treatment | goal | observation | preference"},
                },
                "required": ["content"],
            },
        }
    },
    "required": ["memories"],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _entity(entity_type: str) -> EntityRepository:
    return get_repositories().entity(entity_type)


def _care_notes() -> list[dict[str, Any]]:
    """Care-team notes for the prompt — shared context for every actor, since a
    provider's protocol is exactly what both Emily and other clinicians ask about."""
    try:
        return care_notes_module._list()
    except Exception:
        log.warning("care notes unavailable for companion context", exc_info=True)
        return []


def _memories() -> list[dict[str, Any]]:
    return _entity("HealthMemory").query(
        {"owner_email": OWNER_EMAIL}, "-created_date", MAX_MEMORIES
    )


def _tir(vals: list[float]) -> dict[str, Any]:
    n = len(vals)
    if not n:
        return {}
    mean = sum(vals) / n
    return {
        "n": n,
        "avg": round(mean),
        "tir_70_180": round(100 * sum(1 for v in vals if 70 <= v <= 180) / n),
        "below_70": round(100 * sum(1 for v in vals if v < 70) / n),
        "above_180": round(100 * sum(1 for v in vals if v > 180) / n),
        "cv": (cv := round(100 * statistics.pstdev(vals) / mean) if mean and n > 1 else None),
        # Consensus target: a CV at or under 36% is stable glucose. The model
        # otherwise called 24% "high".
        "cv_stable": cv <= 36 if cv is not None else None,
        "gmi": round(3.31 + 0.02392 * mean, 1),
    }


def _glucose_detail() -> dict[str, Any] | None:
    """Deep, quantitative glucose picture (ported from the old Analyst): multi-
    timeframe stats plus per-day and weekly breakdowns, so the companion can do
    real period-over-period comparisons with actual numbers."""
    tz = ZoneInfo(config_value("app_timezone", APP_TIMEZONE))
    now = datetime.now(timezone.utc)
    since90 = (now - timedelta(days=90)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    rows = get_repositories().glucose.query(
        {"owner_email": OWNER_EMAIL, "timestamp": {"$gte": since90}},
        "-timestamp",
        40000,
    )
    pts = sorted(cgm_points(rows, tz, start=now - timedelta(days=90), end=now), reverse=True)
    if not pts:
        return None
    quality = assess_cgm(
        rows, tz, start=now - timedelta(days=90), end=now, as_of=now.astimezone(tz).date()
    )
    if not quality["ai_eligible"]:
        return {"quality": quality, "excluded_from_ai": True}

    def window(days: int) -> dict[str, Any]:
        cut = now - timedelta(days=days)
        return _tir([v for t, v in pts if t >= cut])

    # per-day (last 14 days) and per-ISO-week (90 days) in the user's timezone
    daily: dict[str, list[float]] = {}
    weekly: dict[str, list[float]] = {}
    day_cut = now - timedelta(days=14)
    for t, v in pts:
        local = t.astimezone(tz)
        if t >= day_cut:
            daily.setdefault(local.date().isoformat(), []).append(v)
        iso = local.isocalendar()
        weekly.setdefault(f"{iso[0]}-W{iso[1]:02d}", []).append(v)

    daily_rows = [
        {"date": d, "avg": round(sum(v) / len(v)), "tir": _tir(v)["tir_70_180"],
         "min": round(min(v)), "max": round(max(v)), "n": len(v)}
        for d, v in sorted(daily.items())
    ]
    weekly_rows = [
        {"week": w, "avg": round(sum(v) / len(v)), "tir": _tir(v)["tir_70_180"], "n": len(v)}
        for w, v in sorted(weekly.items())
    ]
    return {
        "current": {"value": round(pts[0][1]), "at": pts[0][0].astimezone(tz).isoformat(timespec="minutes")},
        "last_24h": window(1), "last_7d": window(7), "last_30d": window(30), "last_90d": window(90),
        "daily_last_14d": daily_rows,
        "weekly_last_90d": weekly_rows,
        "quality": quality,
    }


def _record_actor(record: dict[str, Any]) -> str:
    # Rows created before provider chats existed carry no actor: all owner's.
    return record.get("actor") or "owner"


def _actor_display(actor: str) -> str:
    return actor.split(":", 1)[1] if actor.startswith("provider:") else actor


def _threads(actor: str = "owner") -> list[dict[str, Any]]:
    if actor == "owner":
        _ensure_thread_migration()
    return [
        thread
        for thread in _entity("CompanionThread").query(
            {"owner_email": OWNER_EMAIL}, "-updated_date", 200
        )
        if _record_actor(thread) == actor
    ][:100]


def _thread_for(thread_id: str | None, actor: str) -> dict[str, Any] | None:
    """The thread, only if this actor owns it — the one gate every thread-scoped
    action funnels through."""
    if not thread_id:
        return None
    thread = _entity("CompanionThread").get(thread_id)
    if not thread or _record_actor(thread) != actor:
        return None
    return thread


def _thread_history(thread_id: str, limit: int = HISTORY_TURNS * 2) -> list[dict[str, Any]]:
    """The newest `limit` messages of a thread, oldest first."""
    newest = _entity("ChatMessage").query(
        {"owner_email": OWNER_EMAIL, "thread_id": thread_id},
        "-created_date",
        limit,
    )
    return list(reversed(newest))


def _new_thread(first_msg: str, actor: str = "owner") -> dict[str, Any]:
    title = (first_msg or "").strip().replace("\n", " ")[:60] or "New chat"
    return _entity("CompanionThread").create(
        {
            "title": title,
            "actor": actor,
            "created_date": _now(),
            "updated_date": _now(),
            "owner_email": OWNER_EMAIL,
        }
    )


def _ensure_thread_migration() -> None:
    """One-time: fold any pre-threads ChatMessages into a single legacy thread."""
    orphans = [
        m
        for m in _entity("ChatMessage").query(
            {"owner_email": OWNER_EMAIL}, "created_date", 10000
        )
        if not m.get("thread_id")
    ]
    if not orphans:
        return
    with unit_of_work() as work:
        thread_repository = work.repositories.entity("CompanionThread")
        message_repository = work.repositories.entity("ChatMessage")
        t = thread_repository.create(
            {
                "title": "Earlier conversation",
                "created_date": orphans[0].get("created_date") or _now(),
                "updated_date": orphans[-1].get("created_date") or _now(),
                "owner_email": OWNER_EMAIL,
            }
        )
        for message in orphans:
            message_repository.update(message["id"], {"thread_id": t["id"]})
        work.commit()


def _deterministic_metrics(scope_names: set[str]) -> dict[str, Any]:
    """Keep calculations separate from the selected source-evidence portfolio."""
    if "metabolic" not in scope_names:
        return {}
    ins = insulin.estimate()
    absn = insulin.absorption()
    absorption_for_ai = absn.get("quality", {}).get("ai_eligible")
    return {
        "glucose": _glucose_detail(),
        "insulin": ({
            "resistance_estimate": ins.get("category"), "tdd_per_kg": ins.get("tdd_per_kg"),
            "complete_data_through": ins.get("data_through"), "current": ins.get("current"),
            "data_age_days": ins.get("data_age_days"),
            "pump_reported_avg_tdd": ins.get("reconciliation", {}).get("pump_reported_avg_tdd"),
            "calculated_avg_tdd": ins.get("reconciliation", {}).get("calculated_avg_tdd"),
            "incomplete_days": ins.get("reconciliation", {}).get("incomplete_days"),
            "limitations": ins.get("reconciliation", {}).get("limitations"),
            "response_consistency": absn.get("consistency") if absorption_for_ai else None,
            "response_variability_cv_pct": absn.get("cv_pct") if absorption_for_ai else None,
            "absorption_quality": absn.get("quality"),
            "quality": ins.get("quality"),
        } if ins.get("quality", {}).get("ai_eligible") else {
            "quality": ins.get("quality"), "excluded_from_ai": True,
        }) if ins.get("available") else None,
    }


def _audience_note(actor: str) -> str:
    if actor == "owner":
        return ""
    return (
        f"\n\nAUDIENCE: You are speaking with {_actor_display(actor)}, a clinician on Emily's "
        "care team — not Emily herself. Address the clinician professionally, refer to Emily "
        "in the third person, and keep every citation rule exactly as stated."
    )


def _reply_prompt(
    user_msg: str,
    evidence_context: dict,
    memories: list,
    history: list,
    sources: list | None = None,
    metrics: dict | None = None,
    *,
    memory_limit: int | None = None,
    history_turns: int | None = None,
    keep_sources: bool = True,
    audience: str = "",
    care_notes: list | None = None,
    note_limit: int | None = None,
) -> str:
    memory_items = companion_evidence.memory_aliases(
        memories[: memory_limit if memory_limit is not None else PROMPT_MEMORY_LIMIT]
    )
    mem_txt = "\n".join(
        f"[{item['alias']}] [{item['category']}] {item['content'][:300]}"
        for item in memory_items
    ) or "(nothing remembered yet)"
    turns = history_turns if history_turns is not None else HISTORY_TURNS
    hist_txt = "\n".join(
        f"{'Emily' if message['role'] == 'user' else 'Companion'}: "
        f"{str(message.get('content') or '')[:800]}"
        for message in (history[-turns * 2:] if turns else [])
    ) or "(start of conversation)"
    note_txt = ""
    kept_notes = (care_notes or [])[: note_limit if note_limit is not None else PROMPT_NOTE_LIMIT]
    if kept_notes:
        blocks = [
            f"[{item['alias']}] [{item['kind']}] {item['title']} — {item['author']}"
            + (f" ({item['updated']})" if item.get("updated") else "")
            + f"\n{item['body']}"
            for item in companion_evidence.care_note_aliases(kept_notes)
        ]
        note_txt = (
            "\n\n=== CARE TEAM NOTES (standing instructions from her providers and herself) ===\n"
            + "\n\n".join(blocks)
            + "\n\nWhen you reference a routine, protocol, or instruction from these notes, cite its exact [C#] alias "
              "and name its author. These are the care team's words — report them faithfully; do not reinterpret them "
              "as your own advice or as data-derived findings."
        )
    src_txt = ""
    if sources and keep_sources:
        blocks = [
            f"[{item['alias']}] {item['title']} ({item['source']}) — {item['url']}\n"
            f"{item['snippet']}"
            for item in companion_evidence.external_aliases(sources)
        ]
        src_txt = (
            "\n\n=== TRUSTED GENERAL MEDICAL SOURCES ===\n"
            + "\n\n".join(blocks)
            + "\n\nFor general medical facts, cite only these aliases such as [G1]. If they do not answer something, "
              "say so rather than guessing. Never use a general source as evidence for a personal-data claim."
        )
    return (
        f"{SYSTEM}{audience}\n\n"
        "=== BOUNDED PERSONAL EVIDENCE ===\n"
        f"{companion_evidence.prompt_context(evidence_context)}\n\n"
        "=== DETERMINISTIC METRICS (cite the Evidence Bundle sources that support any personal statement) ===\n"
        f"{json.dumps(metrics or {}, indent=1, default=str)}\n\n"
        f"=== WHAT YOU REMEMBER ABOUT HER LIVED EXPERIENCE ===\n{mem_txt}\n\n"
        f"=== RECENT CONVERSATION ===\n{hist_txt}"
        f"{note_txt}"
        f"{src_txt}\n\n"
        f"Emily: {user_msg}\nCompanion:"
    )


# Ordered least-to-most costly to lose. Web sources go first because they are
# general reference the model can say it lacks; conversation history and
# memories degrade gradually. The evidence bundle is preserved through every
# step here — every personal claim must cite it — and only when a bare prompt
# still overflows does _EVIDENCE_TRIM_STEPS shed its least-relevant tail.
_PROMPT_BUDGET_STEPS: tuple[dict[str, Any], ...] = (
    {"history_turns": HISTORY_TURNS, "memory_limit": PROMPT_MEMORY_LIMIT, "keep_sources": True, "note_limit": PROMPT_NOTE_LIMIT},
    {"history_turns": HISTORY_TURNS, "memory_limit": PROMPT_MEMORY_LIMIT, "keep_sources": False, "note_limit": PROMPT_NOTE_LIMIT},
    {"history_turns": 4, "memory_limit": 24, "keep_sources": False, "note_limit": 12},
    {"history_turns": 2, "memory_limit": 12, "keep_sources": False, "note_limit": 8},
    {"history_turns": 1, "memory_limit": 6, "keep_sources": False, "note_limit": 4},
    {"history_turns": 0, "memory_limit": 0, "keep_sources": False, "note_limit": 0},
)

# Evidence items are ranked most-relevant-first, so a partial dossier beats a
# refusal: 48 items answer "how did last night look?" no better than the best 16.
_EVIDENCE_TRIM_STEPS = (32, 20, 12, 6)


def _trimmed_evidence(evidence_context: dict, keep: int) -> dict:
    items = list(evidence_context.get("items") or [])
    if keep >= len(items):
        return evidence_context
    kept = items[:keep]
    aliases = {item.get("alias") for item in kept}
    trimmed = dict(evidence_context)
    trimmed["items"] = kept
    # The model must not see an alias whose content was dropped — an opposing
    # note citing [E40] would let it "cite" evidence it never read.
    trimmed["opposing_evidence"] = [
        entry for entry in evidence_context.get("opposing_evidence") or []
        if not entry.get("evidence_alias") or entry.get("evidence_alias") in aliases
    ]
    trimmed["evidence_truncated"] = {"kept": keep, "available": len(items)}
    return trimmed


async def _fitted_reply_prompt(
    user_msg: str,
    evidence_context: dict,
    memories: list,
    history: list,
    sources: list | None = None,
    metrics: dict | None = None,
    *,
    tier: str = "default",
    audience: str = "",
    care_notes: list | None = None,
) -> str:
    """Build the reply prompt, shedding optional context until it fits the window.

    The model counts the tokens — see llm.count_tokens for why a character
    estimate is not good enough here. When the server exposes neither a limit
    nor a tokenizer (Ollama), the prompt is returned unbudgeted, which is the
    behaviour that held before this guard existed.
    """
    url_override = model_override = None
    if tier == "quality":
        quality_url = config_value("quality_llm_url")
        quality_model = config_value("quality_llm_model")
        if quality_url and quality_model:
            url_override, model_override = quality_url, quality_model

    def build(step: dict[str, Any], evidence: dict | None = None) -> str:
        return _reply_prompt(
            user_msg, evidence if evidence is not None else evidence_context,
            memories, history, sources, metrics,
            memory_limit=step["memory_limit"],
            history_turns=step["history_turns"],
            keep_sources=step["keep_sources"],
            audience=audience,
            care_notes=care_notes,
            note_limit=step.get("note_limit"),
        )

    limit = await context_limit(url_override, model_override)
    if not limit:
        return build(_PROMPT_BUDGET_STEPS[0])
    budget = limit - REPLY_MAX_TOKENS - CONTEXT_SAFETY_MARGIN

    prompt = build(_PROMPT_BUDGET_STEPS[0])
    for index, step in enumerate(_PROMPT_BUDGET_STEPS):
        candidate = prompt if index == 0 else build(step)
        used = await count_tokens(candidate, url_override, model_override)
        if used is None:
            return candidate  # no tokenizer: cannot budget, so do not pretend to
        if used <= budget:
            if index:
                log.info(
                    "companion prompt trimmed to fit context: %s tokens (budget %s, step %s)",
                    used, budget, index,
                )
            return candidate
        prompt = candidate

    # A bare prompt still overflows: the evidence bundle itself is the weight,
    # so shed its least-relevant tail rather than refusing to answer.
    bare = _PROMPT_BUDGET_STEPS[-1]
    for keep in _EVIDENCE_TRIM_STEPS:
        candidate = build(bare, _trimmed_evidence(evidence_context, keep))
        used = await count_tokens(candidate, url_override, model_override)
        if used is None:
            return candidate
        if used <= budget:
            log.warning(
                "companion evidence trimmed to fit context: kept %s of %s items (%s tokens, budget %s)",
                keep, len(evidence_context.get("items") or []), used, budget,
            )
            return candidate

    # Even a minimal dossier overflows. Surfacing this beats sending a request
    # the model will reject with a 502.
    raise companion_evidence.CompanionEvidenceError(
        "the bounded evidence for this question exceeds the local model's context window"
    )


async def _reply(
    user_msg: str,
    evidence_context: dict,
    memories: list,
    history: list,
    *,
    metrics: dict | None = None,
    sources: list | None = None,
    tier: str = "default",
    audience: str = "",
    care_notes: list | None = None,
) -> str:
    return _strip_signoff(await invoke_llm(
        await _fitted_reply_prompt(
            user_msg,
            evidence_context,
            memories,
            history,
            sources,
            metrics,
            tier=tier,
            audience=audience,
            care_notes=care_notes,
        ),
        max_tokens=REPLY_MAX_TOKENS,
        tier=tier,
        site="companion_reply",
    ))


async def _finalize_grounded_reply(
    raw_reply: str,
    public_evidence: dict[str, Any],
    memories: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    *,
    tier: str = "default",
    care_notes: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Validate a reply and make one bounded repair attempt when citations fail."""
    reply, evidence = companion_evidence.finalize_reply(
        raw_reply, public_evidence, memories, sources, care_notes
    )
    initial_omissions = int((evidence.get("omissions") or {}).get("count") or 0)
    if not initial_omissions:
        evidence["grounding_retry"] = {"attempted": False}
        return reply, evidence

    repair_prompt = (
        "Rewrite the draft below as a concise natural chat response. Remove every "
        "personal-health statement that does not already have a valid [E#], [M#], or [C#] "
        "citation on the same line. Preserve valid citation aliases exactly, each in "
        "its own square brackets like [E3]; never invent, renumber, or add one.General information may retain an existing "
        "[G#] citation. Do not mention citation validation, unsupported claims, or "
        "that text was removed. Return only the repaired response.\n\n"
        f"DRAFT:\n{str(raw_reply)[:12_000]}"
    )
    try:
        repaired_raw = _strip_signoff(
            await invoke_llm(
                repair_prompt,
                max_tokens=REPLY_MAX_TOKENS,
                tier=tier,
                site="companion_repair",
            )
        )
        repaired_reply, repaired_evidence = companion_evidence.finalize_reply(
            repaired_raw, public_evidence, memories, sources, care_notes
        )
        repaired_omissions = int(
            (repaired_evidence.get("omissions") or {}).get("count") or 0
        )
        if repaired_reply.strip() and repaired_omissions < initial_omissions:
            repaired_evidence["grounding_retry"] = {
                "attempted": True,
                "improved": True,
                "initial_omissions": initial_omissions,
                "remaining_omissions": repaired_omissions,
            }
            return repaired_reply, repaired_evidence
    except Exception:
        log.warning("companion grounding repair failed", exc_info=True)

    evidence["grounding_retry"] = {
        "attempted": True,
        "improved": False,
        "initial_omissions": initial_omissions,
        "remaining_omissions": initial_omissions,
    }
    return reply, evidence


async def _safe_grounded_reply(
    raw_reply: str,
    public_evidence: dict[str, Any],
    memories: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    *,
    tier: str = "default",
    care_notes: list[dict[str, Any]] | None = None,
    user_msg: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Ground the reply, with the treatment-change guard applied before and
    after so neither the draft nor its grounding repair can slip one through."""
    raw_reply, guarded = companion_safety.guard_reply(raw_reply, user_msg)
    reply, evidence = await _finalize_grounded_reply(
        raw_reply,
        public_evidence,
        memories,
        sources,
        tier=tier,
        care_notes=care_notes,
    )
    reply, guarded_after = companion_safety.guard_reply(reply, user_msg)
    evidence["safety_guard"] = {"guarded_lines": guarded + guarded_after}
    return reply, evidence


def _save_assistant_message(
    thread_id: str,
    actor: str,
    content: str,
    *,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with unit_of_work() as work:
        message = work.repositories.entity("ChatMessage").create(
            {
                "role": "assistant",
                "content": content,
                "thread_id": thread_id,
                "actor": actor,
                "evidence": evidence,
                "created_date": _now(),
                "owner_email": OWNER_EMAIL,
            }
        )
        work.repositories.entity("CompanionThread").update(
            thread_id, {"updated_date": _now()}
        )
        work.commit()
    return message


FOLLOW_UP_MAX_WORDS = 8


def _retrieval_question(text: str, history: list[dict[str, Any]]) -> str:
    """Short follow-ups ("Please", "Respond to last question") carry no topic of
    their own; retrieve evidence for them together with her previous question.
    `history` ends with the message being answered."""
    if len(text.split()) > FOLLOW_UP_MAX_WORDS:
        return text
    previous = next(
        (
            str(message.get("content") or "")
            for message in reversed(history[:-1])
            if message.get("role") == "user" and message.get("content")
        ),
        "",
    )
    return f"{text} {previous}".strip()


def _grounding(
    user_msg: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    public, reasoning = companion_evidence.build_context(user_msg)
    scope_names = {item["scope"] for item in public.get("scopes", [])}
    return public, reasoning, _deterministic_metrics(scope_names)


def _companion_message(message_id: str | None) -> dict[str, Any] | None:
    if not message_id:
        return None
    message = _entity("ChatMessage").get(message_id)
    if (
        not message
        or message.get("owner_email") != OWNER_EMAIL
        or message.get("role") != "assistant"
        or not isinstance(message.get("evidence"), dict)
    ):
        return None
    return message


def _grounding_enabled() -> bool:
    return (config_value("companion_web_grounding", "") or "").strip().lower() in ("1", "true", "yes", "on")


async def _distill_query(user_msg: str) -> str | None:
    """Decide if a general medical lookup would help, and if so produce a clean,
    privacy-safe search query (concepts only — no personal details/numbers)."""
    prompt = (
        "You help a health assistant decide whether to look up authoritative medical facts.\n"
        "From the user's message, output a SHORT search query (2-5 words) of general medical concepts — a "
        "condition, lab test, symptom, medication, or mechanism — with NO personal details, names, numbers, or "
        "possessives. If the message is small talk or only about the person's own data/trends (not a general "
        "medical fact), output exactly: NONE\n\n"
        "Message: what does high leukocyte esterase in my urine mean?\nQuery: leukocyte esterase urine\n"
        "Message: compare my time in range this week vs last\nQuery: NONE\n"
        "Message: could my joint pain be from my hashimotos?\nQuery: hashimoto thyroiditis joint pain\n"
        "Message: is my morning cortisol of 20 too high?\nQuery: morning cortisol elevated\n\n"
        f"Message: {user_msg}\nQuery:"
    )
    try:
        res = await invoke_llm(prompt, max_tokens=24, site="companion_distill")
    except Exception:
        return None
    q = (res or "").strip().strip('"').splitlines()[0].strip().rstrip(".")
    if not q or "NONE" in q.upper() or len(q) > 100:
        return None
    return q


async def _extract_memories(user_msg: str, existing: list) -> list[dict]:
    """Facts come only from Emily's own words. The Companion's reply is left out
    on purpose: the small extractor kept turning the reply's speculation into
    "Reports ..." memories she never said."""
    known = "\n".join(f"- {m.get('content', '')}" for m in existing) or "(none)"
    prompt = (
        "You maintain a long-term memory about Emily. From her message below, extract any NEW, durable facts about "
        "her LIVED EXPERIENCE that she states herself: symptoms, how she's feeling, life events or stressors, "
        "treatment or routine changes, goals, preferences. Use her own words as closely as possible, in the third "
        "person. Never add a diagnosis, cause, lab value, or detail she did not say.\n"
        "Emily often shares how she's feeling WHILE asking a question (e.g. 'I've been run down and my joints ache. "
        "Does my data explain it?'). Record the fact she reported, not the question itself.\n"
        "Skip questions with no new fact, small talk, numeric lab or glucose data, and anything already known. "
        "An empty list is the right answer for most messages.\n\n"
        "EXAMPLE\n"
        "Emily said: Work has been brutal this month and I'm barely sleeping. Also my hands feel puffy in the "
        "mornings. Is that related to anything?\n"
        'Output: {"memories": [{"content": "Reports brutal work stress this month and barely sleeping.", "category": "life_context"}, '
        '{"content": "Reports puffy hands in the mornings.", "category": "symptom"}]}\n\n'
        f"ALREADY KNOWN:\n{known}\n\n"
        f"Emily said: {user_msg}\n\nReturn the new memories (empty list if none)."
    )
    res = await invoke_llm(prompt, response_json_schema=MEMORY_SCHEMA, max_tokens=300, site="companion_memory_extract")
    return (res or {}).get("memories", []) if isinstance(res, dict) else []


async def handle(body: dict[str, Any], actor: str = "owner") -> dict[str, Any]:
    action = body.get("action", "threads")

    # Memories are Emily's private, chat-derived record. Providers get their own
    # threads but never read or write hers.
    if actor != "owner" and action in ("memories", "add_memory", "delete_memory"):
        return {"error": "Memories belong to the account owner.", "_status": 403}

    if action == "threads":
        return {
            "threads": _threads(actor),
            # The Fast/Deep toggle only makes sense when a second model is
            # actually configured; with none, the UI hides it entirely.
            "quality_tier_available": bool(
                config_value("quality_llm_url") and config_value("quality_llm_model")
            ),
        }

    if action == "history":
        if not _thread_for(body.get("thread_id"), actor):
            return {"messages": []}
        return {
            "messages": _entity("ChatMessage").query(
                {"owner_email": OWNER_EMAIL, "thread_id": body["thread_id"]},
                "created_date",
                1000,
            )
        }

    if action == "rename_thread":
        tid, title = body.get("thread_id"), (body.get("title") or "").strip()
        if title and _thread_for(tid, actor):
            _entity("CompanionThread").update(tid, {"title": title[:60]})
        return {"ok": True, "threads": _threads(actor)}

    if action in ("delete_thread", "clear"):  # "clear" kept for shim compatibility
        tid = body.get("thread_id")
        if _thread_for(tid, actor):
            with unit_of_work() as work:
                messages = work.repositories.entity("ChatMessage")
                for message in messages.query(
                    {"owner_email": OWNER_EMAIL, "thread_id": tid},
                    "-created_date",
                    10000,
                ):
                    messages.delete(message["id"])
                work.repositories.entity("CompanionThread").delete(tid)
                work.commit()
        return {"ok": True, "threads": _threads(actor)}

    if action == "memories":
        return {"memories": _memories()}

    if action == "add_memory":
        content = (body.get("content") or "").strip()
        if content:
            _entity("HealthMemory").create(
                {
                    "content": content,
                    "category": body.get("category") or "note",
                    "source": "manual",
                    "created_date": _now(),
                    "owner_email": OWNER_EMAIL,
                }
            )
        return {"ok": True, "memories": _memories()}

    if action == "delete_memory":
        if body.get("id"):
            _entity("HealthMemory").delete(body["id"])
        return {"ok": True}

    if action == "evidence_command":
        message = _companion_message(body.get("message_id"))
        if not message or not _thread_for(message.get("thread_id"), actor):
            return {"error": "Companion evidence not found.", "_status": 404}
        evidence = message["evidence"]
        command = str(body.get("command") or "show").strip().lower()
        if command == "show":
            return {
                "command": "show",
                "contract_version": evidence.get("contract_version"),
                "bundle": evidence.get("bundle"),
                "statements": evidence.get("statements") or [],
                "evidence_items": evidence.get("evidence_items") or [],
                "external_sources": evidence.get("external_sources") or [],
                "care_notes": evidence.get("care_notes") or [],
                "missing_data_caveats": evidence.get("missing_data_caveats") or [],
                "budget": evidence.get("budget") or {},
            }
        if command == "opposing":
            return {
                "command": "opposing",
                "opposing_evidence": evidence.get("opposing_evidence") or [],
                "contradictions": evidence.get("contradictions") or [],
            }
        if command == "changes":
            current, _reasoning = companion_evidence.build_context(
                evidence.get("question_intent") or "whole person health context"
            )
            return {
                "command": "changes",
                **companion_evidence.compare_contexts(evidence, current),
            }
        return {"error": "Unknown evidence command.", "_status": 400}

    if action == "send":  # non-streaming fallback (frontend uses /api/companion/stream)
        text = (body.get("message") or "").strip()
        if not text:
            return {"error": "Message is empty.", "_status": 400}
        if body.get("thread_id"):
            if not _thread_for(body["thread_id"], actor):
                return {"error": "Thread not found.", "_status": 404}
            tid = body["thread_id"]
        else:
            tid = _new_thread(text, actor)["id"]
        _entity("ChatMessage").create(
            {
                "role": "user",
                "content": text,
                "thread_id": tid,
                "actor": actor,
                "created_date": _now(),
                "owner_email": OWNER_EMAIL,
            }
        )
        if actor == "owner" and companion_safety.crisis_signal(text):
            log.info("companion crisis support reply sent")
            message = _save_assistant_message(tid, actor, companion_safety.CRISIS_REPLY)
            return {
                "reply": companion_safety.CRISIS_REPLY,
                "evidence": None,
                "message_id": message["id"],
                "remembered": [],
                "thread_id": tid,
            }
        history = _thread_history(tid)
        memories = _memories() if actor == "owner" else []
        prompt_memories = memories[:PROMPT_MEMORY_LIMIT]
        notes = _care_notes()
        try:
            public_evidence, reasoning, metrics = _grounding(_retrieval_question(text, history))
            raw_reply = await _reply(
                text,
                reasoning,
                prompt_memories,
                history[:-1],
                metrics=metrics,
                audience=_audience_note(actor),
                care_notes=notes,
            )
            reply, evidence = await _safe_grounded_reply(
                raw_reply,
                public_evidence,
                prompt_memories,
                [],
                tier=body.get("tier", "default"),
                care_notes=notes,
                user_msg=text,
            )
        except Exception as err:
            log.exception("companion reply failed")
            return {"error": f"Companion is unavailable: {err}", "_status": 502}
        reply = (reply or "").strip()
        with unit_of_work() as work:
            message = work.repositories.entity("ChatMessage").create(
                {
                    "role": "assistant",
                    "content": reply,
                    "evidence": evidence,
                    "thread_id": tid,
                    "actor": actor,
                    "created_date": _now(),
                    "owner_email": OWNER_EMAIL,
                }
            )
            work.repositories.entity("CompanionThread").update(
                tid, {"updated_date": _now()}
            )
            work.commit()
        remembered = await _store_new_memories(text, memories) if actor == "owner" else []
        return {
            "reply": reply,
            "evidence": evidence,
            "message_id": message["id"],
            "remembered": remembered,
            "thread_id": tid,
        }

    return {"error": "Unknown action", "_status": 400}


MEMORY_CATEGORIES = {"symptom", "life_context", "treatment", "goal", "observation", "preference"}
MAX_MEMORIES_PER_MESSAGE = 3
MIN_MEMORY_SOURCE_WORDS = 4  # "Please" or "thanks" carries no fact to remember


def _only_questions(text: str) -> bool:
    """Questions alone ("How is my thyroid? Is my dose right?") report nothing to
    remember; facts arrive as statements, often before the question."""
    sentences = [part for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part]
    return bool(sentences) and all(sentence.rstrip().endswith("?") for sentence in sentences)


async def _store_new_memories(text: str, memories: list) -> list[str]:
    remembered: list[str] = []
    if (
        len(text.split()) < MIN_MEMORY_SOURCE_WORDS
        or _only_questions(text)
        or companion_safety.crisis_signal(text)
    ):
        return remembered
    known = [e.get("content", "") for e in memories]
    try:
        for m in await _extract_memories(text, memories):
            if len(remembered) >= MAX_MEMORIES_PER_MESSAGE:
                break
            c = (m.get("content") or "").strip()
            if not c:
                continue
            if not companion_safety.grounded_memory(c, text):
                log.info("companion memory rejected: not grounded in the message")
                continue
            if companion_safety.near_duplicate(c, known + remembered):
                continue
            category = str(m.get("category") or "").strip().lower()
            _entity("HealthMemory").create(
                {
                    "content": c,
                    "category": category if category in MEMORY_CATEGORIES else "observation",
                    "source": "companion",
                    "created_date": _now(),
                    "owner_email": OWNER_EMAIL,
                }
            )
            remembered.append(c)
    except Exception:
        log.warning("companion memory extraction failed", exc_info=True)
    return remembered


async def stream_send(text: str, tier: str = "default", thread_id: str | None = None, actor: str = "owner"):
    """Stream a reply as newline-delimited JSON. Emits {"thread": {...}} first if a
    new thread was created, {"delta": "..."} per chunk, then a final
    {"done": true, "remembered": [...], "thread_id": ...}. Persists the exchange
    to its thread and extracts memories once the reply completes. tier="quality"
    uses the bigger, slower local model. Provider actors chat in their own
    threads, without Emily's memories in the prompt and without writing any."""
    text = (text or "").strip()
    if not text:
        yield json.dumps({"error": "Message is empty."}) + "\n"
        return
    tier = "quality" if tier == "quality" else "default"
    if thread_id:
        if not _thread_for(thread_id, actor):
            yield json.dumps({"error": "Thread not found."}) + "\n"
            return
    else:
        thread = _new_thread(text, actor)
        thread_id = thread["id"]
        yield json.dumps({"thread": thread}) + "\n"
    _entity("ChatMessage").create(
        {
            "role": "user",
            "content": text,
            "thread_id": thread_id,
            "actor": actor,
            "created_date": _now(),
            "owner_email": OWNER_EMAIL,
        }
    )
    if actor == "owner" and companion_safety.crisis_signal(text):
        # Never routed to the model, never mined for memories.
        log.info("companion crisis support reply sent")
        _save_assistant_message(thread_id, actor, companion_safety.CRISIS_REPLY)
        yield json.dumps({"delta": companion_safety.CRISIS_REPLY}) + "\n"
        yield json.dumps({"done": True, "remembered": [], "thread_id": thread_id}) + "\n"
        return
    history = _thread_history(thread_id)
    memories = _memories() if actor == "owner" else []
    prompt_memories = memories[:PROMPT_MEMORY_LIMIT]

    # Optional grounding: search trusted medical sources and let the model cite
    # them, instead of recalling facts from memory (which it hallucinates).
    sources: list[dict[str, Any]] = []
    if _grounding_enabled():
        yield json.dumps({"searching": True}) + "\n"
        query = await _distill_query(text)
        if query:
            try:
                sources = await research.gather(query)
            except Exception:
                log.warning("companion grounding failed", exc_info=True)
        yield json.dumps({"sources": sources}) + "\n"

    yield json.dumps({"grounding": True}) + "\n"
    notes = _care_notes()
    try:
        public_evidence, reasoning, metrics = _grounding(_retrieval_question(text, history))
        prompt = await _fitted_reply_prompt(
            text,
            reasoning,
            prompt_memories,
            history[:-1],
            sources,
            metrics,
            tier=tier,
            audience=_audience_note(actor),
            care_notes=notes,
        )
    except Exception as err:
        log.exception("companion evidence grounding failed")
        yield json.dumps({"error": f"Companion evidence is unavailable: {err}"}) + "\n"
        return
    parts: list[str] = []
    stream_meta: dict[str, Any] = {}
    try:
        async for chunk in invoke_llm_stream(
            prompt,
            max_tokens=REPLY_MAX_TOKENS,
            tier=tier,
            stop=SIGNOFF_STOP,
            site="companion_reply",
            meta=stream_meta,
        ):
            if not chunk:
                continue
            parts.append(chunk)
    except Exception as err:
        log.exception("companion stream failed")
        yield json.dumps({"error": f"Companion is unavailable: {err}"}) + "\n"
        return

    reply = _strip_signoff("".join(parts).strip())
    if stream_meta.get("finish_reason") == "length":
        log.info("companion reply hit the token cap; trimmed to the last full sentence")
        reply = companion_safety.trim_cut_off(reply)
    if not reply:
        yield json.dumps({"error": "Companion returned an empty response."}) + "\n"
        return
    reply, evidence = await _safe_grounded_reply(
        reply,
        public_evidence,
        prompt_memories,
        sources,
        tier=tier,
        care_notes=notes,
        user_msg=text,
    )
    if not reply:
        yield json.dumps({"error": "Companion returned no supported response."}) + "\n"
        return
    with unit_of_work() as work:
        message = work.repositories.entity("ChatMessage").create(
            {
                "role": "assistant",
                "content": reply,
                "thread_id": thread_id,
                "actor": actor,
                "sources": sources or None,
                "evidence": evidence,
                "created_date": _now(),
                "owner_email": OWNER_EMAIL,
            }
        )
        work.repositories.entity("CompanionThread").update(
            thread_id, {"updated_date": _now()}
        )
        work.commit()
    for offset in range(0, len(reply), 240):
        yield json.dumps({"delta": reply[offset:offset + 240]}) + "\n"
    yield json.dumps({
        "evidence": evidence,
        "message_id": message["id"],
    }) + "\n"
    remembered = await _store_new_memories(text, memories) if actor == "owner" else []
    yield json.dumps({"done": True, "remembered": remembered, "thread_id": thread_id}) + "\n"


@router.post("/api/companion/stream")
async def companion_stream(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    b = body if isinstance(body, dict) else {}
    return StreamingResponse(
        stream_send(
            b.get("message", ""),
            b.get("tier", "default"),
            b.get("thread_id"),
            actor=session_actor(request),
        ),
        media_type="application/x-ndjson",
    )
