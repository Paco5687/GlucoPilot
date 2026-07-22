"""Oura Ring OAuth + sync — port of base44/functions/ouraAuth & ouraSync.

Flow: frontend opens Oura authorize with redirect_uri={APP_PUBLIC_URL}/oura-callback,
the SPA callback page posts the code back here (action=exchange_code). This is
the redirect URI to register in the Oura developer console.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from . import db
from .config import OWNER_EMAIL, env
from .connector_provenance import capture_records, latest_observed, source_failure
from .db import config_value

OURA_TOKEN_URL = "https://api.ouraring.com/oauth/token"
OURA_API = "https://api.ouraring.com/v2/usercollection"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _get_connection() -> dict[str, Any] | None:
    rows = db.query_entities("OuraConnection", {"owner_email": OWNER_EMAIL}, "-created_date", 1)
    return rows[0] if rows else None


def _save_tokens(tokens: dict[str, Any], existing: dict[str, Any] | None) -> dict[str, Any]:
    data = {
        "owner_email": OWNER_EMAIL,
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token") or (existing or {}).get("refresh_token", ""),
        "expires_at": _iso(datetime.now(timezone.utc) + timedelta(seconds=int(tokens.get("expires_in", 3600)))),
        "connected": True,
    }
    if existing:
        return db.update_entity("OuraConnection", existing["id"], data)
    return db.create_entity("OuraConnection", data)


async def handle_auth(body: dict[str, Any]) -> dict[str, Any]:
    action = body.get("action")

    if action == "get_client_id":
        return {"client_id": config_value("oura_client_id")}

    if action == "status":
        conn = _get_connection()
        return {"connected": bool(conn and conn.get("connected"))}

    if action == "disconnect":
        conn = _get_connection()
        if conn:
            db.update_entity("OuraConnection", conn["id"], {"connected": False, "access_token": "", "refresh_token": ""})
        return {"success": True}

    if action == "exchange_code":
        code = body.get("code")
        redirect_uri = body.get("redirect_uri") or f"{env('APP_PUBLIC_URL').rstrip('/')}/oura-callback"
        if not code:
            return {"error": "Missing authorization code.", "_status": 400}
        client_id, client_secret = config_value("oura_client_id"), config_value("oura_client_secret")
        if not client_id or not client_secret:
            return {"error": "Oura client credentials are not configured.", "_status": 500}

        async with httpx.AsyncClient(timeout=30) as client:
            token_res = await client.post(
                OURA_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
        if token_res.status_code >= 400:
            return {"error": f"Token exchange failed ({token_res.status_code}): {token_res.text[:300]}", "_status": 502}
        _save_tokens(token_res.json(), _get_connection())
        return {"success": True}

    return {"error": "Unknown action", "_status": 400}


async def _refresh_token(client: httpx.AsyncClient, conn: dict[str, Any]) -> str | None:
    res = await client.post(
        OURA_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": conn.get("refresh_token", ""),
            "client_id": config_value("oura_client_id"),
            "client_secret": config_value("oura_client_secret"),
        },
    )
    if res.status_code >= 400:
        return None
    tokens = res.json()
    _save_tokens(tokens, conn)
    return tokens["access_token"]


async def _fetch(client: httpx.AsyncClient, endpoint: str, token: str, start: str, end: str) -> list[dict]:
    """Fetch one endpoint over a date range, following next_token pagination."""
    base_params = (
        {"start_date": start, "end_date": end}
        if endpoint != "heartrate"
        else {"start_datetime": f"{start}T00:00:00+00:00", "end_datetime": f"{end}T23:59:59+00:00"}
    )
    items: list[dict] = []
    next_token = None
    for _ in range(100):  # page cap
        params = dict(base_params)
        if next_token:
            params["next_token"] = next_token
        res = await client.get(
            f"{OURA_API}/{endpoint}", params=params, headers={"Authorization": f"Bearer {token}"}
        )
        if res.status_code >= 400:
            source_failure(f"Oura {endpoint} failed with status {res.status_code}")
            break
        payload = res.json()
        page = payload.get("data") or []
        capture_records(
            page,
            external_id=endpoint,
            observed_at=latest_observed(page, "timestamp", "day"),
            metadata={"endpoint": endpoint},
        )
        items.extend(page)
        next_token = payload.get("next_token")
        if not next_token:
            break
    return items


def _process_daily(sleep, readiness, activity, hr, spo2) -> dict[str, dict]:
    by_day: dict[str, dict] = {}

    def day(d: str) -> dict:
        return by_day.setdefault(d, {})

    for s in sleep:
        d = day(s["day"])
        contributors = s.get("contributors") or {}
        d.update(
            sleep_score=s.get("score"),
            sleep_total_seconds=contributors.get("total_sleep"),
            sleep_efficiency=contributors.get("efficiency"),
            sleep_rem_seconds=contributors.get("rem_sleep"),
            sleep_deep_seconds=contributors.get("deep_sleep"),
            sleep_latency_seconds=contributors.get("latency"),
        )
    for r in readiness:
        d = day(r["day"])
        d.update(
            readiness_score=r.get("score"),
            readiness_temperature_deviation=r.get("temperature_deviation"),
            readiness_hrv_balance=(r.get("contributors") or {}).get("hrv_balance"),
        )
    for a in activity:
        d = day(a["day"])
        d.update(
            activity_score=a.get("score"),
            activity_steps=a.get("steps"),
            activity_calories=a.get("total_calories"),
            activity_active_calories=a.get("active_calories"),
        )
    hr_by_day: dict[str, list] = {}
    for sample in hr:
        ts = sample.get("timestamp") or ""
        if "T" in ts and sample.get("bpm"):
            hr_by_day.setdefault(ts.split("T")[0], []).append(sample["bpm"])
    for d_key, bpms in hr_by_day.items():
        d = day(d_key)
        d["average_heart_rate"] = round(sum(bpms) / len(bpms))
        d["lowest_heart_rate"] = min(bpms)
    for s in spo2:
        d = day(s["day"])
        d["spo2_average"] = (s.get("spo2_percentage") or {}).get("average")
    return by_day


def _parse_ts(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def _sync_intraday_hr(hr_data: list[dict]) -> tuple[int, int]:
    """Insert HR samples not already stored. Dedup is by exact timestamp (not
    a high-water mark) so historical backfills work; Oura samples can be
    seconds apart, so no tolerance window."""
    if not hr_data:
        return 0, 0
    existing = db.query_entities("OuraHeartRate", {"owner_email": OWNER_EMAIL}, "timestamp", 1000000)
    seen = {r.get("timestamp") for r in existing}

    new_samples = []
    for sample in hr_data:
        ts = _parse_ts(sample.get("timestamp") or "")
        if not ts or not sample.get("bpm"):
            continue
        iso = _iso(ts)
        if iso in seen:
            continue
        seen.add(iso)
        new_samples.append({"timestamp": iso, "bpm": sample["bpm"], "source": "oura", "owner_email": OWNER_EMAIL})
    if new_samples:
        db.bulk_create_entities("OuraHeartRate", new_samples)
    return len(new_samples), len(hr_data) - len(new_samples)


async def handle_sync(body: dict[str, Any]) -> dict[str, Any]:
    conn = _get_connection()
    if not conn or not conn.get("connected"):
        return {"error": "Oura not connected", "_status": 400}

    days = int(body.get("days") or 30)
    # Intraday HR is ~288 samples/day; cap its window so a multi-year daily
    # backfill doesn't drag in a million HR rows.
    hr_days = min(days, int(body.get("hr_days") or 365))
    async with httpx.AsyncClient(timeout=120) as client:
        token = conn.get("access_token")
        expires_at = _parse_ts(conn.get("expires_at") or "")
        if expires_at and expires_at < datetime.now(timezone.utc):
            token = await _refresh_token(client, conn)
            if not token:
                return {"error": "Token refresh failed", "_status": 502}

        now = datetime.now(timezone.utc)
        sleep, readiness, activity, spo2, hr = [], [], [], [], []
        # 90-day chunks, oldest first; pagination handles density within chunks
        cursor = now - timedelta(days=days)
        while cursor < now:
            chunk_end = min(cursor + timedelta(days=90), now)
            start_s, end_s = cursor.date().isoformat(), chunk_end.date().isoformat()
            sleep += await _fetch(client, "daily_sleep", token, start_s, end_s)
            readiness += await _fetch(client, "daily_readiness", token, start_s, end_s)
            activity += await _fetch(client, "daily_activity", token, start_s, end_s)
            spo2 += await _fetch(client, "daily_spo2", token, start_s, end_s)
            cursor = chunk_end
        cursor = now - timedelta(days=hr_days)
        while cursor < now:
            chunk_end = min(cursor + timedelta(days=30), now)
            hr += await _fetch(client, "heartrate", token, cursor.date().isoformat(), chunk_end.date().isoformat())
            cursor = chunk_end

    by_day = _process_daily(sleep, readiness, activity, hr, spo2)

    existing = db.query_entities("OuraDaily", {"owner_email": OWNER_EMAIL})
    existing_by_date = {e.get("date"): e for e in existing}

    created = updated = 0
    for day_key, data in by_day.items():
        record = {**data, "date": day_key, "owner_email": OWNER_EMAIL}
        if day_key in existing_by_date:
            db.update_entity("OuraDaily", existing_by_date[day_key]["id"], record)
            updated += 1
        else:
            db.create_entity("OuraDaily", record)
            created += 1

    hr_created, hr_skipped = _sync_intraday_hr(hr)
    return {
        "success": True,
        "created": created,
        "updated": updated,
        "days_synced": len(by_day),
        "hr_samples": hr_created,
        "hr_samples_skipped": hr_skipped,
    }
