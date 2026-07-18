"""Fitbit Web API sync — daily wearable metrics.

Emily wears the Fitbit ~24/7 (Dexcom readout on wrist), so it supplies the
daytime record alongside Oura: steps, calories, active minutes, resting HR,
sleep, nightly SpO2, breathing rate, and skin-temperature deviation, one row
per day in FitbitDaily.

OAuth2 authorization-code flow (register a "Personal" app at dev.fitbit.com,
redirect {APP_PUBLIC_URL}/fitbit-callback). IMPORTANT: Fitbit refresh tokens
are single-use and rotate on every refresh — both tokens are re-saved each
time. Rate limit is 150 requests/user/hour; a 365-day backfill costs ~30.
"""

import asyncio
import base64
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from . import db
from .config import OWNER_EMAIL, env
from .db import config_value, set_config_value

log = logging.getLogger("glucopilot.fitbit")

TOKEN_URL = "https://api.fitbit.com/oauth2/token"
API = "https://api.fitbit.com"
SCOPES = "activity heartrate oxygen_saturation respiratory_rate sleep temperature"

_sync_lock = asyncio.Lock()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _get_connection() -> dict[str, Any] | None:
    rows = db.query_entities("FitbitConnection", {"owner_email": OWNER_EMAIL}, "-created_date", 1)
    return rows[0] if rows else None


def _basic_auth() -> str:
    raw = f"{config_value('fitbit_client_id')}:{config_value('fitbit_client_secret')}"
    return "Basic " + base64.b64encode(raw.encode()).decode()


def _save_tokens(tokens: dict[str, Any], existing: dict[str, Any] | None) -> dict[str, Any]:
    data = {
        "owner_email": OWNER_EMAIL,
        "access_token": tokens["access_token"],
        # Rotating refresh tokens: always persist the newest one.
        "refresh_token": tokens.get("refresh_token") or (existing or {}).get("refresh_token", ""),
        "expires_at": (
            datetime.now(timezone.utc) + timedelta(seconds=int(tokens.get("expires_in", 28800)))
        ).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "connected": True,
    }
    if existing:
        return db.update_entity("FitbitConnection", existing["id"], data)
    return db.create_entity("FitbitConnection", data)


async def _access_token(client: httpx.AsyncClient) -> str:
    conn = _get_connection()
    if not conn or not conn.get("connected"):
        raise RuntimeError("Fitbit is not connected.")
    try:
        expires = datetime.fromisoformat(conn.get("expires_at", "").replace("Z", "+00:00"))
    except ValueError:
        expires = datetime.now(timezone.utc)
    if expires > datetime.now(timezone.utc) + timedelta(minutes=5):
        return conn["access_token"]
    res = await client.post(
        TOKEN_URL,
        headers={"Authorization": _basic_auth(), "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": conn.get("refresh_token", "")},
    )
    if res.status_code >= 400:
        raise RuntimeError(f"Fitbit token refresh failed ({res.status_code}): {res.text[:200]}")
    tokens = res.json()
    _save_tokens(tokens, conn)
    return tokens["access_token"]


async def _get(client: httpx.AsyncClient, token: str, path: str) -> Any:
    res = await client.get(f"{API}{path}", headers={"Authorization": f"Bearer {token}"})
    if res.status_code >= 400:
        log.warning("fitbit %s failed: %s %s", path, res.status_code, res.text[:200])
        return None
    return res.json()


def _chunks(start: date, end: date, max_days: int):
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=max_days - 1), end)
        yield cursor.isoformat(), chunk_end.isoformat()
        cursor = chunk_end + timedelta(days=1)


async def _fetch_window(client: httpx.AsyncClient, token: str, start: date, end: date) -> dict[str, dict]:
    by_day: dict[str, dict] = {}

    def day(d: str) -> dict:
        return by_day.setdefault(d, {})

    # Long-range series endpoints (single request each)
    for resource, field in (("steps", "steps"), ("calories", "calories_out"), ("minutesVeryActive", "active_minutes")):
        payload = await _get(client, token, f"/1/user/-/activities/{resource}/date/{start}/{end}.json")
        for item in (payload or {}).get(f"activities-{resource}", []):
            try:
                day(item["dateTime"])[field] = round(float(item["value"]))
            except (KeyError, ValueError):
                continue

    payload = await _get(client, token, f"/1/user/-/activities/heart/date/{start}/{end}.json")
    for item in (payload or {}).get("activities-heart", []):
        rhr = (item.get("value") or {}).get("restingHeartRate")
        if rhr is not None:
            day(item["dateTime"])["resting_heart_rate"] = rhr

    # 100-day chunks
    for s, e in _chunks(start, end, 100):
        payload = await _get(client, token, f"/1.2/user/-/sleep/date/{s}/{e}.json")
        for item in (payload or {}).get("sleep", []):
            if not item.get("isMainSleep", True):
                continue
            d = day(item.get("dateOfSleep", ""))
            d["sleep_minutes"] = item.get("minutesAsleep")
            d["sleep_efficiency"] = item.get("efficiency")

    # 30-day chunks
    for s, e in _chunks(start, end, 30):
        payload = await _get(client, token, f"/1/user/-/spo2/date/{s}/{e}.json")
        for item in payload or []:
            value = item.get("value") or {}
            if value.get("avg") is not None:
                d = day(item.get("dateTime", ""))
                d["spo2_avg"] = value.get("avg")
                d["spo2_min"] = value.get("min")

        payload = await _get(client, token, f"/1/user/-/br/date/{s}/{e}.json")
        for item in (payload or {}).get("br", []):
            rate = (item.get("value") or {}).get("breathingRate")
            if rate is not None:
                day(item.get("dateTime", ""))["breathing_rate"] = rate

        payload = await _get(client, token, f"/1/user/-/temp/skin/date/{s}/{e}.json")
        for item in (payload or {}).get("tempSkin", []):
            dev = (item.get("value") or {}).get("nightlyRelative")
            if dev is not None:
                day(item.get("dateTime", ""))["skin_temp_deviation"] = dev

    by_day.pop("", None)
    return by_day


async def handle(body: dict[str, Any]) -> dict[str, Any]:
    action = body.get("action")

    if action == "get_client_id":
        return {"client_id": config_value("fitbit_client_id")}

    if action == "status":
        conn = _get_connection()
        latest = db.query_entities("FitbitDaily", {"owner_email": OWNER_EMAIL}, "-date", 1)
        return {
            "configured": bool(config_value("fitbit_client_id") and config_value("fitbit_client_secret")),
            "connected": bool(conn and conn.get("connected")),
            "latest_day": latest[0].get("date") if latest else None,
            "last_sync": config_value("fitbit_last_sync") or None,
        }

    if action == "disconnect":
        conn = _get_connection()
        if conn:
            db.update_entity("FitbitConnection", conn["id"], {"connected": False, "access_token": "", "refresh_token": ""})
        return {"success": True}

    if action == "exchange_code":
        code = body.get("code")
        redirect_uri = body.get("redirect_uri") or f"{env('APP_PUBLIC_URL').rstrip('/')}/fitbit-callback"
        if not code:
            return {"error": "Missing authorization code.", "_status": 400}
        if not config_value("fitbit_client_id") or not config_value("fitbit_client_secret"):
            return {"error": "Fitbit client credentials are not configured (Settings page).", "_status": 500}
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                TOKEN_URL,
                headers={"Authorization": _basic_auth(), "Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": config_value("fitbit_client_id"),
                },
            )
        if res.status_code >= 400:
            return {"error": f"Token exchange failed ({res.status_code}): {res.text[:300]}", "_status": 502}
        _save_tokens(res.json(), _get_connection())
        return {"success": True}

    if action == "sync":
        days = min(int(body.get("days") or 7), 365)
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=days)
        async with _sync_lock:
            try:
                async with httpx.AsyncClient(timeout=90) as client:
                    token = await _access_token(client)
                    by_day = await _fetch_window(client, token, start, end)
            except Exception as err:
                log.exception("fitbit sync failed")
                return {"error": f"Fitbit sync failed: {err}", "_status": 502}

            existing = db.query_entities("FitbitDaily", {"owner_email": OWNER_EMAIL})
            existing_by_date = {e.get("date"): e for e in existing}
            created = updated = 0
            for day_key, data in by_day.items():
                record = {**data, "date": day_key, "owner_email": OWNER_EMAIL}
                if day_key in existing_by_date:
                    db.update_entity("FitbitDaily", existing_by_date[day_key]["id"], record)
                    updated += 1
                else:
                    db.create_entity("FitbitDaily", record)
                    created += 1
        set_config_value("fitbit_last_sync", _iso_now())
        return {"success": True, "created": created, "updated": updated, "days_synced": len(by_day)}

    return {"error": "Unknown action", "_status": 400}
