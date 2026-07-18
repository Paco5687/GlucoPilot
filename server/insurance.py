"""Health insurance details — a single stored record surfaced on the Visit
Report so it's always at hand at appointments.

Kept as one InsuranceInfo entity (owner-scoped). Fields can be typed on the
Settings page or auto-filled from a photo of the card via the same on-server
vision model used for lab extraction (PHI stays local with the local model).
"""

import base64
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel

from . import db
from .auth import require_admin, require_login
from .config import OWNER_EMAIL
from .llm import invoke_llm

log = logging.getLogger("glucopilot.insurance")

router = APIRouter()

FIELDS = (
    "carrier", "plan_name", "plan_type", "member_name", "member_id",
    "group_number", "rx_bin", "rx_pcn", "rx_group", "customer_service_phone",
    "effective_date", "notes",
)

ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
MAX_FILE_MB = 15

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "carrier": {"type": "string", "description": "Insurance company / carrier name"},
        "plan_name": {"type": "string"},
        "plan_type": {"type": "string", "description": "e.g. PPO, HMO, EPO"},
        "member_name": {"type": "string", "description": "Primary member / subscriber name"},
        "member_id": {"type": "string", "description": "Member / subscriber ID"},
        "group_number": {"type": "string"},
        "rx_bin": {"type": "string", "description": "Pharmacy RxBIN"},
        "rx_pcn": {"type": "string", "description": "Pharmacy RxPCN"},
        "rx_group": {"type": "string", "description": "Pharmacy Rx Group"},
        "customer_service_phone": {"type": "string", "description": "Member/customer service phone"},
        "effective_date": {"type": "string", "description": "Effective date if shown, YYYY-MM-DD or as printed"},
    },
}

EXTRACTION_PROMPT = """You are reading a health insurance card (front and/or back) for the cardholder's own records.
Extract the printed fields exactly as shown: carrier, plan name/type, member (subscriber) name, member/subscriber ID,
group number, the pharmacy RxBIN / RxPCN / RxGroup, the member services phone number, and effective date if present.
Only return values that are actually printed on the card — leave a field out if it is not visible. Do not guess."""


def _get() -> dict[str, Any] | None:
    rows = db.query_entities("InsuranceInfo", {"owner_email": OWNER_EMAIL}, "-created_date", 1)
    return rows[0] if rows else None


def report_block() -> dict[str, Any]:
    """Insurance section for the Visit Report payload."""
    row = _get()
    if not row or not any(str(row.get(f) or "").strip() for f in FIELDS):
        return {"available": False}
    return {"available": True, **{f: row.get(f) or "" for f in FIELDS}}


@router.get("/api/insurance", dependencies=[Depends(require_login)])
def get_insurance():
    row = _get() or {}
    return {f: row.get(f) or "" for f in FIELDS}


class InsuranceBody(BaseModel):
    carrier: str | None = None
    plan_name: str | None = None
    plan_type: str | None = None
    member_name: str | None = None
    member_id: str | None = None
    group_number: str | None = None
    rx_bin: str | None = None
    rx_pcn: str | None = None
    rx_group: str | None = None
    customer_service_phone: str | None = None
    effective_date: str | None = None
    notes: str | None = None


@router.put("/api/insurance", dependencies=[Depends(require_admin)])
def save_insurance(body: InsuranceBody):
    data = {f: (getattr(body, f) or "").strip() for f in FIELDS}
    existing = _get()
    if existing:
        db.update_entity("InsuranceInfo", existing["id"], data)
    else:
        db.create_entity("InsuranceInfo", {**data, "owner_email": OWNER_EMAIL})
    return get_insurance()


@router.post("/api/insurance/extract", dependencies=[Depends(require_admin)])
async def extract_insurance(file: UploadFile, back: UploadFile | None = None):
    """Auto-fill from a photo of the card (front, optionally back). Returns the
    parsed fields for review — does not save."""
    images: list[str] = []
    for up in (file, back):
        if up is None:
            continue
        suffix = Path(up.filename or "card").suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(status_code=400, detail=f"Unsupported image type {suffix}. Use PNG or JPG.")
        content = await up.read()
        if len(content) > MAX_FILE_MB * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"Image too large (max {MAX_FILE_MB} MB).")
        media = "image/png" if suffix == ".png" else "image/webp" if suffix == ".webp" else "image/jpeg"
        images.append(f"{media}|{base64.b64encode(content).decode()}")
    if not images:
        raise HTTPException(status_code=400, detail="No image provided.")
    try:
        extracted = await invoke_llm(EXTRACTION_PROMPT, response_json_schema=EXTRACTION_SCHEMA, max_tokens=1500, images=images)
    except Exception as err:
        log.exception("insurance extraction failed")
        raise HTTPException(status_code=502, detail=f"Extraction failed: {err}")
    return {f: str((extracted or {}).get(f) or "").strip() for f in FIELDS if f != "notes"}
