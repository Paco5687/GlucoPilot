"""Daily symptom journal — Emily logs how she's feeling each night (title,
description, severity, duration, time of day). Each entry is individual and
rolls into her health history, so every AI feature (Companion, Overview,
insights) and the Visit Report can reason over what she has actually been
experiencing day to day, alongside her glucose, labs, cycle, and wearables.
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db
from .auth import require_admin, require_login
from .config import APP_TIMEZONE, OWNER_EMAIL
from .db import config_value

log = logging.getLogger("glucopilot.symptoms")

router = APIRouter()

SEVERITY_LABELS = {1: "very mild", 2: "mild", 3: "moderate", 4: "severe", 5: "very severe"}
TIMES = ("morning", "afternoon", "evening", "night", "all day")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _today() -> str:
    return datetime.now(ZoneInfo(config_value("app_timezone", APP_TIMEZONE))).date().isoformat()


def _clean_severity(v: Any) -> int:
    try:
        return min(5, max(1, int(v)))
    except (TypeError, ValueError):
        return 3


def _list(limit: int = 2000) -> list[dict[str, Any]]:
    rows = db.query_entities("SymptomLog", {"owner_email": OWNER_EMAIL}, "-created_date", limit)
    # newest day first, then newest-created within the same day
    rows.sort(key=lambda r: (str(r.get("entry_date") or ""), str(r.get("created_date") or "")), reverse=True)
    return rows


def get_recent(days: int = 90) -> list[dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    return [r for r in _list() if str(r.get("entry_date") or "") >= cutoff]


def context_block(days: int = 90) -> dict[str, Any] | None:
    """Compact symptom picture for LLM context and the Visit Report: the recent
    entries plus a recurring-symptom rollup (how often + how severe)."""
    rows = get_recent(days)
    if not rows:
        return None
    recent = [
        {
            "date": r.get("entry_date"),
            "symptom": r.get("title"),
            "severity": r.get("severity"),
            "severity_label": SEVERITY_LABELS.get(r.get("severity"), ""),
            "duration": r.get("duration") or "",
            "time_of_day": r.get("time_of_day") or "",
            "notes": r.get("description") or "",
        }
        for r in rows[:60]
    ]
    agg: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        title = (r.get("title") or "").strip().lower()
        if title:
            agg[title].append(r.get("severity") or 3)
    recurring = sorted(
        (
            {"symptom": t, "days_logged": len(sev), "avg_severity": round(sum(sev) / len(sev), 1)}
            for t, sev in agg.items()
        ),
        key=lambda x: (x["days_logged"], x["avg_severity"]),
        reverse=True,
    )[:12]
    return {"window_days": days, "entries_logged": len(rows), "recent": recent, "recurring": recurring}


def report_block(days: int = 90) -> dict[str, Any] | None:
    return context_block(days)


@router.get("/api/symptoms", dependencies=[Depends(require_login)])
def list_symptoms(days: int = 120):
    rows = get_recent(days) if days > 0 else _list()
    return {"symptoms": rows, "today": _today(), "severity_labels": SEVERITY_LABELS}


class SymptomBody(BaseModel):
    title: str
    description: str | None = None
    severity: int | None = None
    duration: str | None = None
    time_of_day: str | None = None
    entry_date: str | None = None


@router.post("/api/symptoms", dependencies=[Depends(require_admin)])
def add_symptom(body: SymptomBody):
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="A symptom title is required.")
    entry_date = (body.entry_date or "").strip() or _today()
    db.create_entity("SymptomLog", {
        "title": title,
        "description": (body.description or "").strip(),
        "severity": _clean_severity(body.severity),
        "duration": (body.duration or "").strip(),
        "time_of_day": (body.time_of_day or "").strip(),
        "entry_date": entry_date,
        "created_date": _now(),
        "owner_email": OWNER_EMAIL,
    })
    return {"symptoms": get_recent(120), "today": _today(), "severity_labels": SEVERITY_LABELS}


@router.delete("/api/symptoms/{sid}", dependencies=[Depends(require_admin)])
def delete_symptom(sid: str):
    rows = db.query_entities("SymptomLog", {"id": sid, "owner_email": OWNER_EMAIL}, limit=1)
    if rows:
        db.delete_entity("SymptomLog", sid)
    return {"ok": True, "symptoms": get_recent(120), "today": _today(), "severity_labels": SEVERITY_LABELS}
