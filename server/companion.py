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
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from . import db, health_summary, insulin
from .auth import require_admin
from .config import APP_TIMEZONE, OWNER_EMAIL
from .db import config_value
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
    rows = db.query_entities(
        "GlucoseReading", {"owner_email": OWNER_EMAIL, "timestamp": {"$gte": since90}}, "-timestamp", 40000
    )
    pts = []
    for r in rows:
        try:
            t = datetime.fromisoformat(str(r.get("timestamp")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        v = r.get("value")
        if v is not None:
            pts.append((t, float(v)))
    if not pts:
        return None

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
    }


def _threads() -> list[dict[str, Any]]:
    _ensure_thread_migration()
    return db.query_entities("CompanionThread", {"owner_email": OWNER_EMAIL}, "-updated_date", 100)


def _thread_history(thread_id: str, limit: int = HISTORY_TURNS * 2) -> list[dict[str, Any]]:
    return db.query_entities(
        "ChatMessage", {"owner_email": OWNER_EMAIL, "thread_id": thread_id}, "created_date", limit
    )


def _new_thread(first_msg: str) -> dict[str, Any]:
    title = (first_msg or "").strip().replace("\n", " ")[:60] or "New chat"
    return db.create_entity("CompanionThread", {
        "title": title, "created_date": _now(), "updated_date": _now(), "owner_email": OWNER_EMAIL,
    })


def _ensure_thread_migration() -> None:
    """One-time: fold any pre-threads ChatMessages into a single legacy thread."""
    orphans = [
        m for m in db.query_entities("ChatMessage", {"owner_email": OWNER_EMAIL}, "created_date", 10000)
        if not m.get("thread_id")
    ]
    if not orphans:
        return
    t = db.create_entity("CompanionThread", {
        "title": "Earlier conversation",
        "created_date": orphans[0].get("created_date") or _now(),
        "updated_date": orphans[-1].get("created_date") or _now(),
        "owner_email": OWNER_EMAIL,
    })
    for m in orphans:
        db.update_entity("ChatMessage", m["id"], {"thread_id": t["id"]})


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
        "glucose": _glucose_detail() or ctx.get("glucose"),
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
    action = body.get("action", "threads")

    if action == "threads":
        return {"threads": _threads()}

    if action == "history":
        tid = body.get("thread_id")
        if not tid:
            return {"messages": []}
        return {"messages": db.query_entities("ChatMessage", {"owner_email": OWNER_EMAIL, "thread_id": tid}, "created_date", 1000)}

    if action == "rename_thread":
        tid, title = body.get("thread_id"), (body.get("title") or "").strip()
        if tid and title:
            db.update_entity("CompanionThread", tid, {"title": title[:60]})
        return {"ok": True, "threads": _threads()}

    if action in ("delete_thread", "clear"):  # "clear" kept for shim compatibility
        tid = body.get("thread_id")
        if tid:
            for m in db.query_entities("ChatMessage", {"owner_email": OWNER_EMAIL, "thread_id": tid}, "-created_date", 10000):
                db.delete_entity("ChatMessage", m["id"])
            db.delete_entity("CompanionThread", tid)
        return {"ok": True, "threads": _threads()}

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

    if action == "send":  # non-streaming fallback (frontend uses /api/companion/stream)
        text = (body.get("message") or "").strip()
        if not text:
            return {"error": "Message is empty.", "_status": 400}
        tid = body.get("thread_id") or _new_thread(text)["id"]
        db.create_entity("ChatMessage", {"role": "user", "content": text, "thread_id": tid, "created_date": _now(), "owner_email": OWNER_EMAIL})
        history = _thread_history(tid)
        memories = _memories()
        try:
            reply = await _reply(text, _dossier(), memories, history[:-1])
        except Exception as err:
            log.exception("companion reply failed")
            return {"error": f"Companion is unavailable: {err}", "_status": 502}
        reply = (reply or "").strip()
        db.create_entity("ChatMessage", {"role": "assistant", "content": reply, "thread_id": tid, "created_date": _now(), "owner_email": OWNER_EMAIL})
        db.update_entity("CompanionThread", tid, {"updated_date": _now()})
        remembered = await _store_new_memories(text, reply, memories)
        return {"reply": reply, "remembered": remembered, "thread_id": tid}

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
    db.create_entity("ChatMessage", {"role": "user", "content": text, "thread_id": thread_id, "created_date": _now(), "owner_email": OWNER_EMAIL})
    history = _thread_history(thread_id)
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
    db.create_entity("ChatMessage", {"role": "assistant", "content": reply, "thread_id": thread_id, "created_date": _now(), "owner_email": OWNER_EMAIL})
    db.update_entity("CompanionThread", thread_id, {"updated_date": _now()})
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
