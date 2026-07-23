"""Diagnosed conditions (medical history) — e.g. Type 1 Diabetes, Hashimoto's.

A simple owner-scoped list that becomes part of the health context every AI
feature sees (Companion, Overview) and prints on the Visit Report, so analysis
is anchored to what Emily has actually been diagnosed with.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db
from .auth import require_admin, require_login
from .config import OWNER_EMAIL

log = logging.getLogger("glucopilot.conditions")

router = APIRouter()

STATUSES = ("active", "resolved")


def _list() -> list[dict[str, Any]]:
    rows = db.query_entities("Diagnosis", {"owner_email": OWNER_EMAIL}, "-created_date", 300)
    # active first, then by diagnosed_date desc
    rows.sort(key=lambda d: (d.get("status") != "active", str(d.get("diagnosed_date") or "")), reverse=False)
    return rows


def get_conditions() -> list[dict[str, Any]]:
    """Confirmed diagnoses only; tentative entries live in the hypothesis ledger."""
    return [
        {"name": d.get("name"), "status": d.get("status") or "active",
         "diagnosed": d.get("diagnosed_date") or "", "notes": d.get("notes") or ""}
        for d in _list()
        if d.get("status") != "suspected"
    ]


def report_block() -> list[dict[str, Any]]:
    return get_conditions()


@router.get("/api/conditions", dependencies=[Depends(require_login)])
def list_conditions():
    return {"conditions": _list()}


class DiagnosisBody(BaseModel):
    name: str
    diagnosed_date: str | None = None
    status: str | None = None
    notes: str | None = None


@router.post("/api/conditions", dependencies=[Depends(require_admin)])
def add_condition(body: DiagnosisBody):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="A condition name is required.")
    status = (body.status or "active").strip().lower()
    if status == "suspected":
        raise HTTPException(
            status_code=400,
            detail="Tentative conditions must be recorded in the hypothesis ledger.",
        )
    db.create_entity("Diagnosis", {
        "name": name,
        "diagnosed_date": (body.diagnosed_date or "").strip(),
        "status": status if status in STATUSES else "active",
        "notes": (body.notes or "").strip(),
        "created_date": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "owner_email": OWNER_EMAIL,
    })
    return {"conditions": _list()}


@router.delete("/api/conditions/{cid}", dependencies=[Depends(require_admin)])
def delete_condition(cid: str):
    rows = db.query_entities("Diagnosis", {"id": cid, "owner_email": OWNER_EMAIL}, limit=1)
    if rows:
        db.delete_entity("Diagnosis", cid)
    return {"ok": True, "conditions": _list()}
