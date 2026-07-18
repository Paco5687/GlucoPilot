"""Dexcom API v3 integration.

PRODUCTION ONLY: this app targets the real Dexcom account. There is no sandbox
mode — connecting a sandbox user would consume the app's only production
user-license slot for nothing. The OAuth flow is strictly user-initiated
(clicking Connect in the UI); nothing here starts it automatically.

Flow: GET /dexcom/login redirects to Dexcom's consent page; Dexcom redirects
back to GET /dexcom/callback (must exactly match DEXCOM_REDIRECT_URI in the
Dexcom developer portal); tokens are stored server-side in the entity store.
EGVs are persisted as GlucoseReading rows (source="dexcom"); Dexcom app events
(carbs/insulin logged in the Dexcom app) map to Treatment rows.
"""

import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import db
from .auth import require_admin
from .config import OWNER_EMAIL, dexcom_base_url, env
from .db import config_value
from .readings import persist_readings_deduped

router = APIRouter()

TREND_MAP = {
    "doubleUp": "DoubleUp",
    "singleUp": "SingleUp",
    "fortyFiveUp": "FortyFiveUp",
    "flat": "Flat",
    "fortyFiveDown": "FortyFiveDown",
    "singleDown": "SingleDown",
    "doubleDown": "DoubleDown",
}

MAX_QUERY_DAYS = 30  # Dexcom v3 limits each records query window


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _dexcom_ts(dt: datetime) -> str:
    # Dexcom v3 expects ISO-8601 without timezone suffix, in UTC.
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _parse_ts(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _get_connection() -> dict[str, Any] | None:
    rows = db.query_entities("DexcomConnection", {"owner_email": OWNER_EMAIL}, "-created_date", 1)
    return rows[0] if rows else None


def _save_tokens(payload: dict[str, Any]) -> None:
    existing = _get_connection()
    data = {
        "owner_email": OWNER_EMAIL,
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token") or (existing or {}).get("refresh_token", ""),
        "token_type": payload.get("token_type", "Bearer"),
        "expires_at": int(time.time()) + int(payload.get("expires_in", 7200)) - 60,
        "connected": True,
    }
    if existing:
        db.update_entity("DexcomConnection", existing["id"], data)
    else:
        db.create_entity("DexcomConnection", data)


@router.get("/dexcom/login")
def dexcom_login(request: Request, _: None = Depends(require_admin)):
    if not config_value("dexcom_client_id") or not config_value("dexcom_client_secret"):
        raise HTTPException(status_code=500, detail="Dexcom client credentials are not configured.")
    state = secrets.token_urlsafe(24)
    request.session["dexcom_state"] = state
    params = {
        "client_id": config_value("dexcom_client_id"),
        "redirect_uri": env("DEXCOM_REDIRECT_URI"),
        "response_type": "code",
        "scope": "offline_access",
        "state": state,
    }
    return RedirectResponse(dexcom_base_url() + "/v3/oauth2/login?" + urlencode(params))


@router.get("/dexcom/callback")
async def dexcom_callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None
):
    if error:
        raise HTTPException(status_code=400, detail=f"Dexcom authorization failed: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Missing Dexcom authorization code.")
    if state != request.session.get("dexcom_state"):
        raise HTTPException(status_code=400, detail="OAuth state mismatch.")
    payload = {
        "client_id": config_value("dexcom_client_id"),
        "client_secret": config_value("dexcom_client_secret"),
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": env("DEXCOM_REDIRECT_URI"),
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(dexcom_base_url() + "/v3/oauth2/token", data=payload)
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    _save_tokens(response.json())
    return RedirectResponse("/connections?dexcom=connected", status_code=303)


async def _access_token(client: httpx.AsyncClient) -> str:
    conn = _get_connection()
    if not conn or not conn.get("connected"):
        raise HTTPException(status_code=401, detail="Dexcom is not connected.")
    if int(conn.get("expires_at") or 0) > int(time.time()):
        return conn["access_token"]
    response = await client.post(
        dexcom_base_url() + "/v3/oauth2/token",
        data={
            "client_id": config_value("dexcom_client_id"),
            "client_secret": config_value("dexcom_client_secret"),
            "refresh_token": conn.get("refresh_token", ""),
            "grant_type": "refresh_token",
        },
    )
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=f"Dexcom token refresh failed: {response.text}")
    tokens = response.json()
    _save_tokens(tokens)
    return tokens["access_token"]


def _map_egv(record: dict[str, Any]) -> dict[str, Any] | None:
    value = record.get("value")
    ts = _parse_ts(record.get("systemTime") or record.get("displayTime"))
    if value is None or ts is None:
        return None
    return {
        "value": round(value),
        "timestamp": _iso(ts),
        "trend": TREND_MAP.get(record.get("trend"), "Unknown"),
        "source": "dexcom",
        "owner_email": OWNER_EMAIL,
    }


def _map_event(record: dict[str, Any]) -> dict[str, Any] | None:
    ts = _parse_ts(record.get("systemTime") or record.get("displayTime"))
    if ts is None or record.get("eventStatus") == "deleted":
        return None
    etype = (record.get("eventType") or "").lower()
    base = {
        "timestamp": _iso(ts),
        "source": "dexcom",
        "ns_id": record.get("recordId"),  # reuse the dedup-id field for Dexcom record ids
        "owner_email": OWNER_EMAIL,
    }
    value = record.get("value")
    if etype == "carbs" and value:
        return {**base, "type": "carb", "event_type": "Carbs", "amount": float(value)}
    if etype == "insulin" and value:
        sub = (record.get("eventSubType") or "").lower()
        return {
            **base,
            "type": "insulin",
            "event_type": "Insulin",
            "amount": float(value),
            "insulin_type": "long" if sub == "longacting" else "rapid",
        }
    if etype == "bloodglucose" and value:
        return {**base, "type": "bg", "event_type": "BG Check", "glucose": float(value)}
    if etype in ("exercise", "health", "notes", "note"):
        return {**base, "type": "note", "event_type": record.get("eventType"), "notes": record.get("eventSubType")}
    return None


async def _fetch_range(client: httpx.AsyncClient, token: str, path: str, start: datetime, end: datetime) -> list[dict]:
    records: list[dict] = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=MAX_QUERY_DAYS), end)
        response = await client.get(
            dexcom_base_url() + path,
            params={"startDate": _dexcom_ts(cursor), "endDate": _dexcom_ts(chunk_end)},
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=f"Dexcom fetch failed: {response.text[:300]}")
        records.extend(response.json().get("records") or [])
        cursor = chunk_end
    return records


def _persist(readings: list[dict], events: list[dict]) -> dict[str, int]:
    # Global cross-source dedup: Share/Nightscout usually stored these same
    # readings an hour before the official API delivers them.
    created = 0
    if readings:
        created, _skipped = persist_readings_deduped(readings)

    new_events = []
    if events:
        existing_events = db.query_entities("Treatment", {"source": "dexcom", "owner_email": OWNER_EMAIL}, "-timestamp", 5000)
        existing_ids = {t.get("ns_id") for t in existing_events if t.get("ns_id")}
        for e in events:
            if e.get("ns_id") and e["ns_id"] in existing_ids:
                continue
            new_events.append(e)

    if new_events:
        db.bulk_create_entities("Treatment", new_events)
    return {"readings_synced": created, "events_synced": len(new_events)}


async def _sync(days: int | None = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    if days:
        start = now - timedelta(days=days)
    else:
        # Cursor from BOTH official and Share rows: when Share is live it owns
        # the recent slots, so the official sync trails as a ~2h gap-filler
        # rather than stalling on its own last stored row.
        latest_ts = None
        for source in ("dexcom", "dexcom_share"):
            rows = db.query_entities(
                "GlucoseReading", {"source": source, "owner_email": OWNER_EMAIL}, "-timestamp", 1
            )
            ts = _parse_ts(rows[0]["timestamp"]) if rows else None
            if ts and (latest_ts is None or ts > latest_ts):
                latest_ts = ts
        start = (latest_ts - timedelta(hours=2)) if latest_ts else now - timedelta(days=1)
        start = max(start, now - timedelta(days=30))
        if start >= now:
            return {"ok": True, "readings_synced": 0, "events_synced": 0}

    async with httpx.AsyncClient(timeout=60) as client:
        token = await _access_token(client)
        egvs = await _fetch_range(client, token, "/v3/users/self/egvs", start, now)
        try:
            raw_events = await _fetch_range(client, token, "/v3/users/self/events", start, now)
        except HTTPException:
            raw_events = []  # events scope/endpoint is best-effort

    readings = [m for m in (_map_egv(r) for r in egvs) if m]
    events = [m for m in (_map_event(r) for r in raw_events) if m]
    result = _persist(readings, events)
    conn = _get_connection()
    if conn:
        db.update_entity("DexcomConnection", conn["id"], {"last_sync": _iso(now)})
    return {"ok": True, **result}


async def handle(body: dict[str, Any]) -> dict[str, Any]:
    action = body.get("action")

    if action == "status":
        conn = _get_connection()
        return {
            "configured": bool(config_value("dexcom_client_id") and config_value("dexcom_client_secret")),
            "connected": bool(conn and conn.get("connected")),
            "env": env("DEXCOM_ENV", "production_us"),
            "redirect_uri": env("DEXCOM_REDIRECT_URI"),
            "last_sync": (conn or {}).get("last_sync"),
        }

    if action == "disconnect":
        conn = _get_connection()
        if conn:
            db.update_entity(
                "DexcomConnection", conn["id"], {"connected": False, "access_token": "", "refresh_token": ""}
            )
        return {"success": True}

    if action == "sync":
        return await _sync()

    if action == "backfill":
        days = min(int(body.get("days") or 30), 90)
        return await _sync(days=days)

    return {"error": "Unknown action", "_status": 400}
