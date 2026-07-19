"""Medications / supplements and allergies — high-value clinical context.

Owner-scoped lists that feed the shared health context (Companion, Overview)
and print on the Visit Report, so analysis and answers account for what Emily
takes and what she reacts to. Grouped here since they share the same shape.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db
from .auth import require_admin, require_login
from .config import OWNER_EMAIL

log = logging.getLogger("glucopilot.meds")

router = APIRouter()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---- Medications & supplements -------------------------------------------------

def _meds() -> list[dict[str, Any]]:
    rows = db.query_entities("Medication", {"owner_email": OWNER_EMAIL}, "-created_date", 400)
    rows.sort(key=lambda m: (m.get("status") != "active", m.get("kind") != "medication", str(m.get("name") or "").lower()))
    return rows


def get_medications() -> list[dict[str, Any]]:
    return [
        {"name": m.get("name"), "kind": m.get("kind") or "medication", "dose": m.get("dose") or "",
         "frequency": m.get("frequency") or "", "status": m.get("status") or "active", "notes": m.get("notes") or ""}
        for m in _meds()
    ]


class MedBody(BaseModel):
    name: str
    kind: str | None = None       # medication | supplement
    dose: str | None = None
    frequency: str | None = None
    status: str | None = None     # active | stopped
    notes: str | None = None


@router.get("/api/medications", dependencies=[Depends(require_login)])
def list_meds():
    return {"medications": _meds()}


@router.post("/api/medications", dependencies=[Depends(require_admin)])
def add_med(body: MedBody):
    if not (body.name or "").strip():
        raise HTTPException(status_code=400, detail="A name is required.")
    kind = (body.kind or "medication").strip().lower()
    status = (body.status or "active").strip().lower()
    db.create_entity("Medication", {
        "name": body.name.strip(),
        "kind": kind if kind in ("medication", "supplement") else "medication",
        "dose": (body.dose or "").strip(),
        "frequency": (body.frequency or "").strip(),
        "status": status if status in ("active", "stopped") else "active",
        "notes": (body.notes or "").strip(),
        "created_date": _now(),
        "owner_email": OWNER_EMAIL,
    })
    return {"medications": _meds()}


@router.delete("/api/medications/{mid}", dependencies=[Depends(require_admin)])
def delete_med(mid: str):
    if db.query_entities("Medication", {"id": mid, "owner_email": OWNER_EMAIL}, limit=1):
        db.delete_entity("Medication", mid)
    return {"ok": True, "medications": _meds()}


# ---- Allergies -----------------------------------------------------------------

def _allergies() -> list[dict[str, Any]]:
    return db.query_entities("Allergy", {"owner_email": OWNER_EMAIL}, "-created_date", 200)


def get_allergies() -> list[dict[str, Any]]:
    return [
        {"allergen": a.get("allergen"), "reaction": a.get("reaction") or "",
         "severity": a.get("severity") or "", "notes": a.get("notes") or ""}
        for a in _allergies()
    ]


class AllergyBody(BaseModel):
    allergen: str
    reaction: str | None = None
    severity: str | None = None   # mild | moderate | severe
    notes: str | None = None


@router.get("/api/allergies", dependencies=[Depends(require_login)])
def list_allergies():
    return {"allergies": _allergies()}


@router.post("/api/allergies", dependencies=[Depends(require_admin)])
def add_allergy(body: AllergyBody):
    if not (body.allergen or "").strip():
        raise HTTPException(status_code=400, detail="An allergen is required.")
    sev = (body.severity or "").strip().lower()
    db.create_entity("Allergy", {
        "allergen": body.allergen.strip(),
        "reaction": (body.reaction or "").strip(),
        "severity": sev if sev in ("mild", "moderate", "severe") else "",
        "notes": (body.notes or "").strip(),
        "created_date": _now(),
        "owner_email": OWNER_EMAIL,
    })
    return {"allergies": _allergies()}


@router.delete("/api/allergies/{aid}", dependencies=[Depends(require_admin)])
def delete_allergy(aid: str):
    if db.query_entities("Allergy", {"id": aid, "owner_email": OWNER_EMAIL}, limit=1):
        db.delete_entity("Allergy", aid)
    return {"ok": True, "allergies": _allergies()}
