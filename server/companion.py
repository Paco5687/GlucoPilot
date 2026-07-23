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

from . import companion_evidence, insulin, research
from .auth import require_admin
from .config import APP_TIMEZONE, OWNER_EMAIL
from .db import config_value
from .data_quality import assess_cgm, cgm_points
from .llm import invoke_llm, invoke_llm_stream
from .repositories import EntityRepository, get_repositories
from .unit_of_work import unit_of_work

log = logging.getLogger("glucopilot.companion")

router = APIRouter(dependencies=[Depends(require_admin)])

MAX_MEMORIES = 150
PROMPT_MEMORY_LIMIT = 40
HISTORY_TURNS = 8  # exchanges of prior context sent each turn
REPLY_MAX_TOKENS = 1200  # enough for a substantive answer without truncating mid-thought
MAX_REPLY_PROMPT_CHARS = 96_000

# The small model likes to sign replies like a letter ("— Emily's Health Companion").
# Stop generation before a dash-led sign-off line (em/en dash only — hyphens are
# bullet lists), and strip any that slips through as a backstop.
SIGNOFF_STOP = ["\n\n—", "\n\n–"]
_SIGNOFF_RE = re.compile(r"\n+\s*[—–-][^\n]*\bCompanion\b\s*$", re.IGNORECASE)


def _strip_signoff(text: str) -> str:
    return _SIGNOFF_RE.sub("", text).rstrip()

SYSTEM = (
    "You are Emily's personal health companion — warm, grounded, honest, and genuinely curious about her WHOLE "
    "health, not just one part of it. Emily lives with Type 1 diabetes, but that is only one thread of her story: "
    "her thyroid, hormones and menstrual cycle, inflammation and immune markers, gut health, sleep, energy, mood "
    "and stress, medications and supplements, and everything she tells you about her life all matter just as much. "
    "Do NOT funnel every conversation back to glucose or diabetes — follow the evidence and her actual question "
    "wherever they lead, and focus on the parts of her data that are relevant to what she asked.\n"
    "Ground factual claims in her real data: cite specific numbers, dates, and trends rather than generalities, and "
    "say plainly when the data is old or missing. Every sentence that makes a personal-data observation, calculation, "
    "correlation, or hypothesis MUST end with one or more exact [E#] aliases copied from the supplied evidence items. "
    "Never invent or alter an evidence alias. Cite lived-experience memory only with its exact [M#] alias, and keep "
    "general medical information separate from personal inference, using [G#] only for supplied trusted references. "
    "When she shares how she's feeling or what's happening in her life, take it seriously and connect it to what you see.\n"
    "When the evidence context contains an unresolved contradiction, show both sides and call it unresolved. Never choose a "
    "side silently, and never use a blocking contradiction as the basis for a definitive claim.\n"
    "The clinical_reviews block is authoritative about review semantics: only entries under clinician_confirmed_facts may "
    "be described as clinician-confirmed. Provider annotations are attributed notes, not confirmed facts. Items under "
    "owner_disputed_reviews remain disputed even though their immutable clinician history is retained.\n"
    "SHARE YOUR ACTUAL ANALYSIS. Connect the dots, name the patterns you see, and give your real interpretation of "
    "what they could mean — including which conditions, mechanisms, or explanations the evidence is consistent with, and "
    "how her medications or cycle might be driving what she's feeling. Offer these as hypotheses to explore, not "
    "verdicts. Do NOT hide behind vague hedging, boilerplate disclaimers, or refuse to weigh in — Emily wants your "
    "honest read, and withholding a useful insight helps no one.\n"
    "Two real limits, stated plainly and not belabored: you are not her doctor, so what you offer is insight to "
    "confirm with her care team rather than a formal diagnosis; and you don't tell her to start, stop, or change the "
    "dose of a medication (you can absolutely discuss how her meds may be affecting her). Be concise and human — a "
    "few focused paragraphs, not an exhaustive report, and never repeat yourself. If you truly lack the data to "
    "answer, say so plainly. Write as a natural chat message: do NOT sign off, add a signature, or close with a line "
    "like '— Emily's Health Companion'. Machine-extracted lab evidence with clinically_verified=false must always be "
    "called unverified; parser confidence is never clinical verification."
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
        "cv": round(100 * statistics.pstdev(vals) / mean) if mean and n > 1 else None,
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


def _threads() -> list[dict[str, Any]]:
    _ensure_thread_migration()
    return _entity("CompanionThread").query(
        {"owner_email": OWNER_EMAIL}, "-updated_date", 100
    )


def _thread_history(thread_id: str, limit: int = HISTORY_TURNS * 2) -> list[dict[str, Any]]:
    return _entity("ChatMessage").query(
        {"owner_email": OWNER_EMAIL, "thread_id": thread_id},
        "created_date",
        limit,
    )


def _new_thread(first_msg: str) -> dict[str, Any]:
    title = (first_msg or "").strip().replace("\n", " ")[:60] or "New chat"
    return _entity("CompanionThread").create(
        {
            "title": title,
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


def _reply_prompt(
    user_msg: str,
    evidence_context: dict,
    memories: list,
    history: list,
    sources: list | None = None,
    metrics: dict | None = None,
) -> str:
    memory_items = companion_evidence.memory_aliases(memories[:PROMPT_MEMORY_LIMIT])
    mem_txt = "\n".join(
        f"[{item['alias']}] [{item['category']}] {item['content'][:300]}"
        for item in memory_items
    ) or "(nothing remembered yet)"
    hist_txt = "\n".join(
        f"{'Emily' if message['role'] == 'user' else 'Companion'}: "
        f"{str(message.get('content') or '')[:800]}"
        for message in history[-HISTORY_TURNS * 2:]
    ) or "(start of conversation)"
    src_txt = ""
    if sources:
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
    prompt = (
        f"{SYSTEM}\n\n"
        "=== BOUNDED PERSONAL EVIDENCE ===\n"
        f"{companion_evidence.prompt_context(evidence_context)}\n\n"
        "=== DETERMINISTIC METRICS (cite the Evidence Bundle sources that support any personal statement) ===\n"
        f"{json.dumps(metrics or {}, indent=1, default=str)}\n\n"
        f"=== WHAT YOU REMEMBER ABOUT HER LIVED EXPERIENCE ===\n{mem_txt}\n\n"
        f"=== RECENT CONVERSATION ===\n{hist_txt}"
        f"{src_txt}\n\n"
        f"Emily: {user_msg}\nCompanion:"
    )
    if len(prompt) > MAX_REPLY_PROMPT_CHARS:
        raise companion_evidence.CompanionEvidenceError(
            "bounded Companion prompt exceeds the local-model context limit"
        )
    return prompt


async def _reply(
    user_msg: str,
    evidence_context: dict,
    memories: list,
    history: list,
    *,
    metrics: dict | None = None,
    sources: list | None = None,
    tier: str = "default",
) -> str:
    return _strip_signoff(await invoke_llm(
        _reply_prompt(
            user_msg,
            evidence_context,
            memories,
            history,
            sources,
            metrics,
        ),
        max_tokens=REPLY_MAX_TOKENS,
        tier=tier,
    ))


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
        res = await invoke_llm(prompt, max_tokens=24)
    except Exception:
        return None
    q = (res or "").strip().strip('"').splitlines()[0].strip().rstrip(".")
    if not q or "NONE" in q.upper() or len(q) > 100:
        return None
    return q


async def _extract_memories(user_msg: str, reply: str, existing: list) -> list[dict]:
    known = "\n".join(f"- {m.get('content', '')}" for m in existing) or "(none)"
    prompt = (
        "You maintain a long-term memory about Emily. From the exchange below, extract any NEW, durable facts about "
        "her LIVED EXPERIENCE worth remembering — symptoms, how she's feeling, life events/stressors, treatment or "
        "routine changes, goals, preferences. Only concrete lasting facts, in the third person.\n"
        "IMPORTANT: Emily often shares how she's feeling WHILE asking a question (e.g. 'I've been run down and my "
        "joints ache — does my data explain it?'). Extract the symptom/feeling/event she mentions even when it is "
        "wrapped in a question; you are recording the fact she reported, not the question itself.\n"
        "Do NOT restate her numeric lab/glucose data (that's already tracked), skip pure small talk, and skip "
        "anything already known.\n\n"
        "EXAMPLE\n"
        "Emily said: Work has been brutal this month and I'm barely sleeping. Also my hands feel puffy in the "
        "mornings — is that related to anything?\n"
        'Output: {"memories": [{"content": "Reports high work stress and poor sleep this month.", "category": "life_context"}, '
        '{"content": "Reports morning hand puffiness/swelling.", "category": "symptom"}]}\n\n'
        f"ALREADY KNOWN:\n{known}\n\n"
        f"Emily said: {user_msg}\nCompanion replied: {reply}\n\nReturn the new memories (empty list if none)."
    )
    res = await invoke_llm(prompt, response_json_schema=MEMORY_SCHEMA, max_tokens=500)
    mems = (res or {}).get("memories", []) if isinstance(res, dict) else []
    if not mems:  # small local model is noisy on this task — one retry before giving up
        res = await invoke_llm(prompt, response_json_schema=MEMORY_SCHEMA, max_tokens=500)
        mems = (res or {}).get("memories", []) if isinstance(res, dict) else []
    return mems


async def handle(body: dict[str, Any]) -> dict[str, Any]:
    action = body.get("action", "threads")

    if action == "threads":
        return {"threads": _threads()}

    if action == "history":
        tid = body.get("thread_id")
        if not tid:
            return {"messages": []}
        return {
            "messages": _entity("ChatMessage").query(
                {"owner_email": OWNER_EMAIL, "thread_id": tid},
                "created_date",
                1000,
            )
        }

    if action == "rename_thread":
        tid, title = body.get("thread_id"), (body.get("title") or "").strip()
        if tid and title:
            _entity("CompanionThread").update(tid, {"title": title[:60]})
        return {"ok": True, "threads": _threads()}

    if action in ("delete_thread", "clear"):  # "clear" kept for shim compatibility
        tid = body.get("thread_id")
        if tid:
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
        return {"ok": True, "threads": _threads()}

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
        if not message:
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
        tid = body.get("thread_id") or _new_thread(text)["id"]
        _entity("ChatMessage").create(
            {
                "role": "user",
                "content": text,
                "thread_id": tid,
                "created_date": _now(),
                "owner_email": OWNER_EMAIL,
            }
        )
        history = _thread_history(tid)
        memories = _memories()
        prompt_memories = memories[:PROMPT_MEMORY_LIMIT]
        try:
            public_evidence, reasoning, metrics = _grounding(text)
            raw_reply = await _reply(
                text,
                reasoning,
                prompt_memories,
                history[:-1],
                metrics=metrics,
            )
            reply, evidence = companion_evidence.finalize_reply(
                raw_reply,
                public_evidence,
                prompt_memories,
                [],
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
                    "created_date": _now(),
                    "owner_email": OWNER_EMAIL,
                }
            )
            work.repositories.entity("CompanionThread").update(
                tid, {"updated_date": _now()}
            )
            work.commit()
        remembered = await _store_new_memories(text, reply, memories)
        return {
            "reply": reply,
            "evidence": evidence,
            "message_id": message["id"],
            "remembered": remembered,
            "thread_id": tid,
        }

    return {"error": "Unknown action", "_status": 400}


async def _store_new_memories(text: str, reply: str, memories: list) -> list[str]:
    remembered: list[str] = []
    try:
        for m in await _extract_memories(text, reply, memories):
            c = (m.get("content") or "").strip()
            if not c:
                continue
            if any(c.lower() in e.get("content", "").lower() or e.get("content", "").lower() in c.lower() for e in memories):
                continue  # dedupe against what we already know
            _entity("HealthMemory").create(
                {
                    "content": c,
                    "category": m.get("category") or "observation",
                    "source": "companion",
                    "created_date": _now(),
                    "owner_email": OWNER_EMAIL,
                }
            )
            remembered.append(c)
    except Exception:
        log.warning("companion memory extraction failed", exc_info=True)
    return remembered


async def stream_send(text: str, tier: str = "default", thread_id: str | None = None):
    """Stream a reply as newline-delimited JSON. Emits {"thread": {...}} first if a
    new thread was created, {"delta": "..."} per chunk, then a final
    {"done": true, "remembered": [...], "thread_id": ...}. Persists the exchange
    to its thread and extracts memories once the reply completes. tier="quality"
    uses the bigger, slower local model."""
    text = (text or "").strip()
    if not text:
        yield json.dumps({"error": "Message is empty."}) + "\n"
        return
    tier = "quality" if tier == "quality" else "default"
    if not thread_id:
        thread = _new_thread(text)
        thread_id = thread["id"]
        yield json.dumps({"thread": thread}) + "\n"
    _entity("ChatMessage").create(
        {
            "role": "user",
            "content": text,
            "thread_id": thread_id,
            "created_date": _now(),
            "owner_email": OWNER_EMAIL,
        }
    )
    history = _thread_history(thread_id)
    memories = _memories()
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
    try:
        public_evidence, reasoning, metrics = _grounding(text)
        prompt = _reply_prompt(
            text,
            reasoning,
            prompt_memories,
            history[:-1],
            sources,
            metrics,
        )
    except Exception as err:
        log.exception("companion evidence grounding failed")
        yield json.dumps({"error": f"Companion evidence is unavailable: {err}"}) + "\n"
        return
    parts: list[str] = []
    try:
        async for chunk in invoke_llm_stream(prompt, max_tokens=REPLY_MAX_TOKENS, tier=tier, stop=SIGNOFF_STOP):
            if not chunk:
                continue
            parts.append(chunk)
    except Exception as err:
        log.exception("companion stream failed")
        yield json.dumps({"error": f"Companion is unavailable: {err}"}) + "\n"
        return

    reply = _strip_signoff("".join(parts).strip())
    if not reply:
        yield json.dumps({"error": "Companion returned an empty response."}) + "\n"
        return
    reply, evidence = companion_evidence.finalize_reply(
        reply,
        public_evidence,
        prompt_memories,
        sources,
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
    remembered = await _store_new_memories(text, reply, memories)
    yield json.dumps({"done": True, "remembered": remembered, "thread_id": thread_id}) + "\n"


@router.post("/api/companion/stream")
async def companion_stream(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    b = body if isinstance(body, dict) else {}
    return StreamingResponse(
        stream_send(b.get("message", ""), b.get("tier", "default"), b.get("thread_id")),
        media_type="application/x-ndjson",
    )
