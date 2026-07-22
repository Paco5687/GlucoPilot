"""Dexcom Share — real-time glucose via the follower feed (pydexcom).

Why this exists alongside the official Dexcom v3 API: the official API serves
data with a fixed ~1 hour delay (US) — fine for history, useless for live
monitoring. Dexcom Share is the feed the Follow app uses: near-real-time,
unofficial but stable for a decade, and the same feed the Railway Nightscout
was relaying. Credentials are the Dexcom *account* username/password (the
sharer's, with the Share feature enabled), not developer API keys — no
production OAuth slot is involved.

Readings are stored as GlucoseReading with source="dexcom_share". Inserts use
a global ±240 s tolerance against readings from ALL sources, so Share (first
to see a reading), the official API (an hour later), and Nightscout (while it
still runs) never double-store the same physiological reading.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from . import db
from .config import OWNER_EMAIL
from .connector_provenance import can_advance_freshness, capture_records, latest_observed
from .db import config_value, set_config_value
from .readings import persist_readings_deduped

log = logging.getLogger("glucopilot.dexcom_share")

VALID_TRENDS = {"DoubleUp", "SingleUp", "FortyFiveUp", "Flat", "FortyFiveDown", "SingleDown", "DoubleDown"}

_sync_lock = asyncio.Lock()
_cached_client = None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _configured() -> bool:
    return bool(config_value("dexcom_share_username") and config_value("dexcom_share_password"))


def _client():
    from pydexcom import Dexcom, Region

    region = {"us": Region.US, "ous": Region.OUS, "jp": Region.JP}.get(
        (config_value("dexcom_share_region", "us") or "us").lower(), Region.US
    )
    return Dexcom(
        username=config_value("dexcom_share_username"),
        password=config_value("dexcom_share_password"),
        region=region,
    )


def _fetch_readings(minutes: int, max_count: int) -> list[dict[str, Any]]:
    """Blocking: pull recent readings from Share and map to our schema.
    Reuses one authenticated session; re-logs-in once on failure."""
    global _cached_client
    if _cached_client is None:
        _cached_client = _client()
    try:
        readings = _cached_client.get_glucose_readings(minutes=minutes, max_count=max_count) or []
    except Exception:
        _cached_client = _client()
        readings = _cached_client.get_glucose_readings(minutes=minutes, max_count=max_count) or []
    mapped = []
    raw = []
    for r in readings:
        dt = r.datetime
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        trend = r.trend_direction if r.trend_direction in VALID_TRENDS else "Unknown"
        raw.append(
            {
                "mg_dl": r.mg_dl,
                "datetime": _iso(dt),
                "trend_direction": r.trend_direction,
            }
        )
        mapped.append(
            {
                "value": round(r.mg_dl),
                "timestamp": _iso(dt),
                "trend": trend,
                "source": "dexcom_share",
                "owner_email": OWNER_EMAIL,
            }
        )
    capture_records(
        raw,
        external_id="share-glucose-readings",
        observed_at=latest_observed(raw, "datetime"),
        metadata={"feed": "dexcom_share"},
    )
    return mapped


async def handle(body: dict[str, Any]) -> dict[str, Any]:
    action = body.get("action")

    if action == "status":
        latest = db.query_entities(
            "GlucoseReading", {"source": "dexcom_share", "owner_email": OWNER_EMAIL}, "-timestamp", 1
        )
        return {
            "configured": _configured(),
            "connected": config_value("dexcom_share_verified") == "true",
            "region": config_value("dexcom_share_region", "us"),
            "latest_reading": latest[0]["timestamp"] if latest else None,
            "last_sync": config_value("dexcom_share_last_sync") or None,
        }

    if action == "configure":
        if body.get("username"):
            set_config_value("dexcom_share_username", str(body["username"]).strip())
        if body.get("password"):
            set_config_value("dexcom_share_password", str(body["password"]))
        if body.get("region"):
            set_config_value("dexcom_share_region", str(body["region"]).strip().lower())
        set_config_value("dexcom_share_verified", "")
        return {"ok": True}

    if action == "disconnect":
        for key in ("dexcom_share_username", "dexcom_share_password", "dexcom_share_verified"):
            set_config_value(key, "")
        return {"success": True}

    if not _configured():
        return {
            "error": "Dexcom Share is not configured. Enter the Dexcom account username and password.",
            "_status": 400,
        }

    if action == "test":
        try:
            reading = await asyncio.to_thread(lambda: _client().get_latest_glucose_reading())
        except Exception as err:
            set_config_value("dexcom_share_verified", "")
            return {"error": f"Dexcom Share login failed: {err}", "_status": 502}
        set_config_value("dexcom_share_verified", "true")
        return {
            "ok": True,
            "latest": {"value": reading.mg_dl, "trend": reading.trend_description, "time": _iso(reading.datetime)}
            if reading
            else None,
        }

    if action == "sync":
        # Share serves at most 24h / 288 readings. Frequent scheduler polls
        # pass a small window; manual syncs default to the full 24h.
        minutes = min(int(body.get("minutes") or 1440), 1440)
        max_count = min(int(body.get("max_count") or 288), 288)
        async with _sync_lock:
            try:
                mapped = await asyncio.to_thread(_fetch_readings, minutes, max_count)
            except Exception as err:
                log.exception("dexcom share sync failed")
                return {"error": f"Dexcom Share sync failed: {err}", "_status": 502}
            created, skipped = persist_readings_deduped(mapped)
        if can_advance_freshness():
            set_config_value("dexcom_share_verified", "true")
            set_config_value("dexcom_share_last_sync", _iso(datetime.now(timezone.utc)))
        return {"ok": True, "readings_synced": created, "readings_skipped": skipped, "fetched": len(mapped)}

    return {"error": "Unknown action", "_status": 400}
