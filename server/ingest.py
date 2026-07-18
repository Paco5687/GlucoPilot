"""Token-authenticated ingest for phone-side automations.

POST /api/ingest/cycle with `Authorization: Bearer <ingest token>` accepts:
  - Lively CSV export (text/csv body or multipart "file") — same mapping as
    the in-app importer, parsed server-side.
  - Health Auto Export JSON (Apple Health → scheduled REST push): defensively
    maps menstrual-flow metrics/symptoms to PeriodLog rows. Unrecognized
    payload shapes are logged (truncated) so the mapper can be extended.

The token lives in the config store (ingest_token, generated on demand,
shown on the Settings page). It is NOT a session: it can only write cycle
data through this endpoint.

Dedup: one PeriodLog per date; existing dates are updated only if the
incoming row carries more information (existing manual logs are respected).
"""

import csv
import io
import json
import logging
import re
import secrets as pysecrets
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from . import db
from .config import OWNER_EMAIL
from .db import config_value, set_config_value

log = logging.getLogger("glucopilot.ingest")

router = APIRouter()

FLOW_MAP = {
    "none": "none", "no flow": "none",
    "spotting": "spotting", "spot": "spotting", "light spotting": "spotting",
    "light": "light",
    "medium": "medium", "moderate": "medium", "normal": "medium",
    "heavy": "heavy",
    # Apple Health / Health Auto Export numeric-ish levels
    "1": "light", "2": "medium", "3": "heavy",
    "unspecified": "medium",
}
PHASE_MAP = {
    "menstrual": "menstrual", "period": "menstrual", "bleeding": "menstrual",
    "follicular": "follicular",
    "ovulation": "ovulation", "ovulatory": "ovulation", "fertile": "ovulation",
    "luteal": "luteal",
}


def ingest_token() -> str:
    token = config_value("ingest_token")
    if not token:
        token = pysecrets.token_urlsafe(24)
        set_config_value("ingest_token", token)
    return token


def _require_token(authorization: str | None) -> None:
    expected = ingest_token()
    provided = (authorization or "").removeprefix("Bearer ").strip()
    if not provided or not pysecrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid ingest token")


def _norm_date(raw: str) -> str | None:
    raw = (raw or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})", raw)
    if m:
        mm, dd, yy = m.groups()
        yy = ("20" + yy) if len(yy) == 2 else yy
        return f"{yy}-{mm.zfill(2)}-{dd.zfill(2)}"
    return None


def _rows_from_lively_csv(text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for r in reader:
        lower = {str(k).strip().lower(): (v or "").strip() for k, v in r.items() if k}
        date = _norm_date(lower.get("date") or lower.get("day") or "")
        if not date:
            continue
        phase = PHASE_MAP.get((lower.get("phase") or lower.get("cycle phase") or "").lower())
        flow = FLOW_MAP.get((lower.get("flow") or lower.get("period flow") or lower.get("flow intensity") or "").lower())
        row: dict[str, Any] = {"date": date, "source": "lively_import"}
        if phase:
            row["phase"] = phase
        if flow:
            row["flow"] = flow
        if lower.get("symptoms"):
            row["symptoms"] = lower["symptoms"]
        if lower.get("notes"):
            row["notes"] = lower["notes"]
        rows.append(row)
    return rows


def _rows_from_health_export(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Health Auto Export: {"data": {"metrics": [{name, data: [{date, qty|value}]}], "symptoms": [...]}}"""
    data = payload.get("data") or payload
    by_date: dict[str, dict[str, Any]] = {}

    for metric in data.get("metrics") or []:
        name = str(metric.get("name") or "").lower()
        if "menstrual" not in name and "cycle" not in name:
            continue
        for point in metric.get("data") or []:
            date = _norm_date(str(point.get("date") or ""))
            if not date:
                continue
            row = by_date.setdefault(date, {"date": date, "source": "lively_import"})
            raw_flow = str(point.get("value") or point.get("qty") or "").strip().lower()
            flow = FLOW_MAP.get(raw_flow)
            if flow:
                row["flow"] = flow
            row.setdefault("phase", "menstrual")

    for symptom in data.get("symptoms") or []:
        name = str(symptom.get("name") or "").strip()
        if not name:
            continue
        for point in symptom.get("data") or []:
            date = _norm_date(str(point.get("date") or ""))
            if not date:
                continue
            row = by_date.setdefault(date, {"date": date, "source": "lively_import"})
            existing = row.get("symptoms", "")
            if name.lower() not in existing.lower():
                row["symptoms"] = f"{existing}, {name}".strip(", ")

    return list(by_date.values())


def _upsert(rows: list[dict[str, Any]]) -> dict[str, int]:
    existing = db.query_entities("PeriodLog", {"owner_email": OWNER_EMAIL}, "-date", 5000)
    by_date = {e.get("date"): e for e in existing}
    created = updated = skipped = 0
    for row in rows:
        row["owner_email"] = OWNER_EMAIL
        current = by_date.get(row["date"])
        if current is None:
            db.create_entity("PeriodLog", row)
            by_date[row["date"]] = row
            created += 1
            continue
        if current.get("source") == "manual":
            skipped += 1  # never clobber manual logs
            continue
        patch = {k: v for k, v in row.items() if v and not current.get(k)}
        if patch:
            db.update_entity("PeriodLog", current["id"], patch)
            updated += 1
        else:
            skipped += 1
    return {"created": created, "updated": updated, "skipped": skipped}


@router.post("/api/ingest/cycle")
async def ingest_cycle(request: Request, authorization: str | None = Header(default=None)):
    _require_token(authorization)

    content_type = (request.headers.get("content-type") or "").lower()
    rows: list[dict[str, Any]] = []

    if "multipart/form-data" in content_type:
        form = await request.form()
        upload = form.get("file")
        if upload is None:
            raise HTTPException(status_code=400, detail="multipart body needs a 'file' field")
        rows = _rows_from_lively_csv((await upload.read()).decode("utf-8", errors="replace"))
    else:
        body = await request.body()
        text = body.decode("utf-8", errors="replace")
        if "json" in content_type or text.lstrip().startswith("{"):
            try:
                payload = json.loads(text)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid JSON body")
            rows = _rows_from_health_export(payload)
            if not rows:
                log.info("ingest/cycle: no cycle rows recognized in JSON payload; keys=%s sample=%s",
                         list(payload.keys())[:8], text[:400])
        else:
            rows = _rows_from_lively_csv(text)

    if not rows:
        return {"ok": True, "created": 0, "updated": 0, "skipped": 0, "note": "no cycle rows recognized"}
    result = _upsert(rows)
    return {"ok": True, **result}
