"""Health Companion — a chat grounded in Emily's full health data, with a
persistent memory of her lived experience.

Each turn assembles a compact health dossier (glucose, labs w/ dates, cycle,
wearables, insulin, imaging, profile) + everything the companion remembers about
her + the recent conversation, and answers with her real data as evidence. After
each exchange it extracts durable new facts she shared (symptoms, life events,
how she's feeling, goals) and saves them as HealthMemory — so it keeps learning
what she's going through and can compare it against her records over time.

Not a doctor: it offers observations and questions for her care team, never
diagnoses or dosing.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from . import db, health_summary, insulin
from .auth import require_admin
from .config import OWNER_EMAIL
from .llm import invoke_llm, invoke_llm_stream

log = logging.getLogger("glucopilot.companion")

router = APIRouter(dependencies=[Depends(require_admin)])

MAX_MEMORIES = 150
HISTORY_TURNS = 8  # exchanges of prior context sent each turn

SYSTEM = (
    "You are Emily's personal health companion — warm, grounded, honest, and genuinely curious about her WHOLE "
    "health, not just one part of it. Emily lives with Type 1 diabetes, but that is only one thread of her story: "
    "her thyroid, hormones and menstrual cycle, inflammation and immune markers, gut health, sleep, energy, mood "
    "and stress, medications and supplements, and everything she tells you about her life all matter just as much. "
    "Do NOT funnel every conversation back to glucose or diabetes — follow the evidence and her actual question "
    "wherever they lead, and focus on the parts of her data that are relevant to what she asked.\n"
    "Ground factual claims in her real data: cite specific numbers, dates, and trends rather than generalities, and "
    "say plainly when the data is old or missing. When she shares how she's feeling or what's happening in her life, "
    "take it seriously and connect it to what you see. Be concise and human — a few focused paragraphs, not an "
    "exhaustive report, and never repeat yourself. You are NOT a physician: never diagnose or give dosing/medication "
    "instructions — instead surface observations, patterns, and specific things worth raising with her care team. "
    "If you don't have the data to answer, say so plainly."
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


def _memories() -> list[dict[str, Any]]:
    return db.query_entities("HealthMemory", {"owner_email": OWNER_EMAIL}, "-created_date", MAX_MEMORIES)


def _dossier() -> dict[str, Any]:
    """Compact, current health picture for grounding (kept small for the local
    model's context window)."""
    ctx = health_summary._build_context()
    ins = insulin.estimate()
    absn = insulin.absorption()
    recent_docs = [
        {"title": r.get("title"), "date": r.get("record_date")}
        for r in db.query_entities("MedicalRecord", {"owner_email": OWNER_EMAIL}, "-created_date", 500)
        if r.get("status") == "processed"
    ]
    recent_docs.sort(key=lambda d: str(d.get("date") or ""), reverse=True)
    # Ordered whole-person first (conditions, meds, labs, cycle, wearables),
    # with the diabetes-specific data last so the model doesn't tunnel on it.
    return {
        "diagnosed_conditions": ctx.get("conditions"),
        "medications_and_supplements": ctx.get("medications"),
        "allergies": ctx.get("allergies"),
        "profile": ctx.get("profile"),
        "symptom_journal": ctx.get("symptom_journal"),
        "labs_out_of_range": (ctx.get("labs_out_of_range") or [])[:20],
        "lab_trends": (ctx.get("lab_trends") or [])[:12],
        "menstrual_cycle": ctx.get("cycle"),
        "wearables_recent_vs_prior": ctx.get("wearables"),
        "imaging": ctx.get("imaging"),
        "recent_documents": recent_docs[:12],
        "glucose_90d": ctx.get("glucose"),
        "insulin": {
            "resistance": ins.get("category"), "tdd_per_kg": ins.get("tdd_per_kg"),
            "data_through": ins.get("data_through"), "response_consistency": absn.get("consistency"),
            "response_variability_cv_pct": absn.get("cv_pct"),
        } if ins.get("available") else None,
    }


def _reply_prompt(user_msg: str, dossier: dict, memories: list, history: list) -> str:
    mem_txt = "\n".join(f"- [{m.get('category', 'note')}] {m.get('content')}" for m in memories) or "(nothing remembered yet)"
    hist_txt = "\n".join(f"{'Emily' if m['role'] == 'user' else 'Companion'}: {m['content']}" for m in history) or "(start of conversation)"
    return (
        f"{SYSTEM}\n\n"
        f"=== EMILY'S HEALTH DATA (evidence to ground your answers) ===\n{json.dumps(dossier, indent=1, default=str)}\n\n"
        f"=== WHAT YOU REMEMBER ABOUT HER LIVED EXPERIENCE ===\n{mem_txt}\n\n"
        f"=== RECENT CONVERSATION ===\n{hist_txt}\n\n"
        f"Emily: {user_msg}\nCompanion:"
    )


async def _reply(user_msg: str, dossier: dict, memories: list, history: list) -> str:
    return await invoke_llm(_reply_prompt(user_msg, dossier, memories, history), max_tokens=700)


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
    action = body.get("action", "history")

    if action == "history":
        msgs = db.query_entities("ChatMessage", {"owner_email": OWNER_EMAIL}, "created_date", 400)
        return {"messages": msgs}

    if action == "memories":
        return {"memories": _memories()}

    if action == "add_memory":
        content = (body.get("content") or "").strip()
        if content:
            db.create_entity("HealthMemory", {"content": content, "category": (body.get("category") or "note"),
                                              "source": "manual", "created_date": _now(), "owner_email": OWNER_EMAIL})
        return {"ok": True, "memories": _memories()}

    if action == "delete_memory":
        if body.get("id"):
            db.delete_entity("HealthMemory", body["id"])
        return {"ok": True}

    if action == "clear":
        for m in db.query_entities("ChatMessage", {"owner_email": OWNER_EMAIL}, "-created_date", 10000):
            db.delete_entity("ChatMessage", m["id"])
        return {"ok": True}

    if action == "send":
        text = (body.get("message") or "").strip()
        if not text:
            return {"error": "Message is empty.", "_status": 400}
        db.create_entity("ChatMessage", {"role": "user", "content": text, "created_date": _now(), "owner_email": OWNER_EMAIL})
        history = list(reversed(db.query_entities("ChatMessage", {"owner_email": OWNER_EMAIL}, "-created_date", HISTORY_TURNS * 2)))
        memories = _memories()
        try:
            reply = await _reply(text, _dossier(), memories, history[:-1])
        except Exception as err:
            log.exception("companion reply failed")
            return {"error": f"Companion is unavailable: {err}", "_status": 502}
        reply = (reply or "").strip()
        db.create_entity("ChatMessage", {"role": "assistant", "content": reply, "created_date": _now(), "owner_email": OWNER_EMAIL})
        remembered = await _store_new_memories(text, reply, memories)
        return {"reply": reply, "remembered": remembered}

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
            db.create_entity("HealthMemory", {"content": c, "category": (m.get("category") or "observation"),
                                              "source": "companion", "created_date": _now(), "owner_email": OWNER_EMAIL})
            remembered.append(c)
    except Exception:
        log.warning("companion memory extraction failed", exc_info=True)
    return remembered


async def stream_send(text: str, tier: str = "default"):
    """Stream a reply as newline-delimited JSON: {"delta": "..."} per chunk, then
    a final {"done": true, "remembered": [...]}. Persists the exchange and extracts
    memories once the reply completes. tier="quality" uses the bigger, slower
    local model so its answers can be compared against the fast default."""
    text = (text or "").strip()
    if not text:
        yield json.dumps({"error": "Message is empty."}) + "\n"
        return
    tier = "quality" if tier == "quality" else "default"
    db.create_entity("ChatMessage", {"role": "user", "content": text, "created_date": _now(), "owner_email": OWNER_EMAIL})
    history = list(reversed(db.query_entities("ChatMessage", {"owner_email": OWNER_EMAIL}, "-created_date", HISTORY_TURNS * 2)))
    memories = _memories()
    prompt = _reply_prompt(text, _dossier(), memories, history[:-1])

    parts: list[str] = []
    try:
        async for chunk in invoke_llm_stream(prompt, max_tokens=700, tier=tier):
            if not chunk:
                continue
            parts.append(chunk)
            yield json.dumps({"delta": chunk}) + "\n"
    except Exception as err:
        log.exception("companion stream failed")
        yield json.dumps({"error": f"Companion is unavailable: {err}"}) + "\n"
        return

    reply = "".join(parts).strip()
    if not reply:
        yield json.dumps({"error": "Companion returned an empty response."}) + "\n"
        return
    db.create_entity("ChatMessage", {"role": "assistant", "content": reply, "created_date": _now(), "owner_email": OWNER_EMAIL})
    remembered = await _store_new_memories(text, reply, memories)
    yield json.dumps({"done": True, "remembered": remembered}) + "\n"


@router.post("/api/companion/stream")
async def companion_stream(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    message = (body or {}).get("message", "") if isinstance(body, dict) else ""
    tier = (body or {}).get("tier", "default") if isinstance(body, dict) else "default"
    return StreamingResponse(stream_send(message, tier), media_type="application/x-ndjson")
