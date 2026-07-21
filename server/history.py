"""Health history — a standing narrative plus a dated timeline of medical events
(diagnoses, injuries, exposures, prescriptions, hospital visits, doctor advice).

Feeds every AI feature (Companion, Overview) and the Visit Report so the model
reasons over the actual story and timeline — diagnosis dates, a mold exposure, an
ER visit — not just the numbers.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db
from .auth import require_admin, require_login
from .config import OWNER_EMAIL
from .db import config_value, set_config_value

log = logging.getLogger("glucopilot.history")

router = APIRouter()

NARRATIVE_KEY = "health_narrative"
KINDS = ("diagnosis", "injury", "exposure", "prescription", "hospital", "appointment", "advice", "note")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _entries(limit: int = 1000) -> list[dict[str, Any]]:
    rows = db.query_entities("HistoryEntry", {"owner_email": OWNER_EMAIL}, "-created_date", limit)
    # newest event first; undated entries sink to the bottom
    rows.sort(key=lambda r: (str(r.get("entry_date") or ""), str(r.get("created_date") or "")), reverse=True)
    return rows


def get_narrative() -> str:
    return config_value(NARRATIVE_KEY, "") or ""


def _events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"date": r.get("entry_date") or "", "type": r.get("kind"), "title": r.get("title"),
         "details": r.get("details") or ""}
        for r in rows
    ]


def context_block(max_narrative: int = 2200, max_events: int = 40) -> dict[str, Any] | None:
    """Compact history for LLM context (narrative trimmed to protect the window)."""
    narrative = get_narrative().strip()
    rows = _entries()
    if not narrative and not rows:
        return None
    trimmed = narrative[:max_narrative] + ("…" if len(narrative) > max_narrative else "")
    return {"narrative": trimmed or None, "events": _events(rows[:max_events])}


def report_block() -> dict[str, Any] | None:
    """Full history for the Visit Report."""
    narrative = get_narrative().strip()
    rows = _entries()
    if not narrative and not rows:
        return None
    return {"narrative": narrative or None, "events": _events(rows)}


@router.get("/api/history", dependencies=[Depends(require_login)])
def get_history():
    return {"narrative": get_narrative(), "entries": _entries(), "kinds": list(KINDS)}


class NarrativeBody(BaseModel):
    narrative: str = ""


@router.put("/api/history/narrative", dependencies=[Depends(require_admin)])
def save_narrative(body: NarrativeBody):
    set_config_value(NARRATIVE_KEY, (body.narrative or "").strip())
    return {"ok": True, "narrative": get_narrative()}


class EntryBody(BaseModel):
    title: str
    kind: str | None = None
    entry_date: str | None = None
    details: str | None = None


@router.post("/api/history", dependencies=[Depends(require_admin)])
def add_entry(body: EntryBody):
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="A title is required.")
    kind = (body.kind or "note").strip().lower()
    db.create_entity("HistoryEntry", {
        "title": title,
        "kind": kind if kind in KINDS else "note",
        "entry_date": (body.entry_date or "").strip(),
        "details": (body.details or "").strip(),
        "created_date": _now(),
        "owner_email": OWNER_EMAIL,
    })
    return {"entries": _entries()}


@router.delete("/api/history/{eid}", dependencies=[Depends(require_admin)])
def delete_entry(eid: str):
    rows = db.query_entities("HistoryEntry", {"id": eid, "owner_email": OWNER_EMAIL}, limit=1)
    if rows:
        db.delete_entity("HistoryEntry", eid)
    return {"ok": True, "entries": _entries()}
