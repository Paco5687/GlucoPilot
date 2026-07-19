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

from . import db, health_summary, insulin
from .config import OWNER_EMAIL
from .llm import invoke_llm

log = logging.getLogger("glucopilot.companion")

MAX_MEMORIES = 150
HISTORY_TURNS = 8  # exchanges of prior context sent each turn

SYSTEM = (
    "You are Emily's personal health companion inside GlucoPilot — warm, grounded, and honest. "
    "You have her diagnosed conditions, her actual health data (glucose, labs, menstrual cycle, wearables, insulin, imaging) and a "
    "memory of what she's told you about her lived experience. Ground every factual claim in her real data: "
    "cite specific numbers, dates, and trends rather than generalities, and say when the data is old or missing. "
    "When she shares how she's feeling or what's happening in her life, take it seriously and connect it to what "
    "you see in her data. You are NOT a physician: never diagnose or give dosing/medication instructions — instead "
    "surface observations, patterns, and specific things worth raising with her care team. If you don't have the "
    "data to answer, say so plainly."
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
    return {
        "diagnosed_conditions": ctx.get("conditions"),
        "profile": ctx.get("profile"),
        "glucose_90d": ctx.get("glucose"),
        "cycle": ctx.get("cycle"),
        "wearables_recent_vs_prior": ctx.get("wearables"),
        "labs_out_of_range": (ctx.get("labs_out_of_range") or [])[:20],
        "lab_trends": (ctx.get("lab_trends") or [])[:12],
        "insulin": {
            "resistance": ins.get("category"), "tdd_per_kg": ins.get("tdd_per_kg"),
            "data_through": ins.get("data_through"), "response_consistency": absn.get("consistency"),
            "response_variability_cv_pct": absn.get("cv_pct"),
        } if ins.get("available") else None,
        "imaging": ctx.get("imaging"),
        "recent_documents": recent_docs[:12],
    }


async def _reply(user_msg: str, dossier: dict, memories: list, history: list) -> str:
    mem_txt = "\n".join(f"- [{m.get('category', 'note')}] {m.get('content')}" for m in memories) or "(nothing remembered yet)"
    hist_txt = "\n".join(f"{'Emily' if m['role'] == 'user' else 'Companion'}: {m['content']}" for m in history) or "(start of conversation)"
    prompt = (
        f"{SYSTEM}\n\n"
        f"=== EMILY'S HEALTH DATA (evidence to ground your answers) ===\n{json.dumps(dossier, indent=1, default=str)}\n\n"
        f"=== WHAT YOU REMEMBER ABOUT HER LIVED EXPERIENCE ===\n{mem_txt}\n\n"
        f"=== RECENT CONVERSATION ===\n{hist_txt}\n\n"
        f"Emily: {user_msg}\nCompanion:"
    )
    return await invoke_llm(prompt, max_tokens=900)


async def _extract_memories(user_msg: str, reply: str, existing: list) -> list[dict]:
    known = "\n".join(f"- {m.get('content', '')}" for m in existing) or "(none)"
    prompt = (
        "You maintain a long-term memory about Emily. From the exchange below, extract any NEW, durable facts about "
        "her LIVED EXPERIENCE worth remembering — symptoms, how she's feeling, life events/stressors, treatment or "
        "routine changes, goals, preferences. Only concrete lasting facts, in the third person. Do NOT restate her "
        "numeric lab/glucose data (that's already tracked), do NOT include questions or one-off small talk, and skip "
        "anything already known.\n\n"
        f"ALREADY KNOWN:\n{known}\n\n"
        f"Emily said: {user_msg}\nCompanion replied: {reply}\n\nReturn the new memories (empty list if none)."
    )
    res = await invoke_llm(prompt, response_json_schema=MEMORY_SCHEMA, max_tokens=500)
    return (res or {}).get("memories", []) if isinstance(res, dict) else []


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

        remembered = []
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

        return {"reply": reply, "remembered": remembered}

    return {"error": "Unknown action", "_status": 400}
