"""Care-team notes — the one place providers write directly into the app.

Provider logins are read-only over Emily's health data, but her care team still
needs somewhere to leave routines, protocols, and prescription instructions
that she (and every other provider) can see in context. These notes are that
channel: any logged-in session may author one, each note is stamped with the
actor who wrote it, and only that author can edit it afterwards. The owner can
delete anything (it is her record); providers can delete only their own.

Notes also ride into the Visit Report so standing instructions appear alongside
the data they are about.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import db
from .auth import require_login, session_actor, session_role
from .config import OWNER_EMAIL

log = logging.getLogger("glucopilot.care_notes")

router = APIRouter(dependencies=[Depends(require_login)])

KINDS = ("routine", "protocol", "prescription", "instruction", "note")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _display(actor: str) -> str:
    return actor.split(":", 1)[1] if actor.startswith("provider:") else "Owner"


def _clean_kind(value: Any) -> str:
    kind = str(value or "note").strip().lower()
    return kind if kind in KINDS else "note"


def _list() -> list[dict[str, Any]]:
    rows = db.query_entities("CareTeamNote", {"owner_email": OWNER_EMAIL}, "-updated_date", 500)
    # pinned first, most recently updated within each group (stable sort)
    rows.sort(key=lambda r: not r.get("pinned"))
    return rows


def report_block() -> list[dict[str, Any]] | None:
    """Standing care-team instructions for the Visit Report, pinned first."""
    notes = [
        {
            "kind": note.get("kind"),
            "title": note.get("title"),
            "body": note.get("body"),
            "author": note.get("author_name"),
            "updated": str(note.get("updated_date") or "")[:10],
            "pinned": bool(note.get("pinned")),
        }
        for note in _list()
    ]
    return notes or None


class NoteBody(BaseModel):
    kind: str = "note"
    title: str = ""
    body: str = ""
    pinned: bool = False


def _get_note(note_id: str) -> dict[str, Any]:
    note = db.query_entities("CareTeamNote", {"owner_email": OWNER_EMAIL, "id": note_id}, None, 1)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return note[0]


@router.get("/api/care-notes")
def list_notes(request: Request):
    return {
        "notes": _list(),
        "me": session_actor(request),
        "kinds": list(KINDS),
    }


@router.post("/api/care-notes")
def create_note(request: Request, body: NoteBody):
    title = body.title.strip()[:120]
    text = body.body.strip()[:8000]
    if not title and not text:
        raise HTTPException(status_code=400, detail="Note is empty")
    actor = session_actor(request)
    note = db.create_entity(
        "CareTeamNote",
        {
            "kind": _clean_kind(body.kind),
            "title": title,
            "body": text,
            "pinned": bool(body.pinned),
            "author": actor,
            "author_name": _display(actor),
            "owner_email": OWNER_EMAIL,
        },
    )
    return {"ok": True, "note": note}


@router.put("/api/care-notes/{note_id}")
def update_note(note_id: str, request: Request, body: NoteBody):
    note = _get_note(note_id)
    # Only the author edits their note: a protocol keeps saying what the
    # clinician who wrote it said, and the owner's notes stay hers.
    if note.get("author") != session_actor(request):
        raise HTTPException(status_code=403, detail="Only the author can edit this note.")
    db.update_entity(
        "CareTeamNote",
        note_id,
        {
            "kind": _clean_kind(body.kind),
            "title": body.title.strip()[:120],
            "body": body.body.strip()[:8000],
            "pinned": bool(body.pinned),
        },
    )
    return {"ok": True, "notes": _list()}


@router.delete("/api/care-notes/{note_id}")
def delete_note(note_id: str, request: Request):
    note = _get_note(note_id)
    # Authors remove their own; the owner can remove anything — it's her record.
    if note.get("author") != session_actor(request) and session_role(request) != "admin":
        raise HTTPException(status_code=403, detail="Only the author or the owner can delete this note.")
    db.delete_entity("CareTeamNote", note_id)
    return {"ok": True, "notes": _list()}
