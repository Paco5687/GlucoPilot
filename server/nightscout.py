"""Nightscout sync — port of base44/functions/nightscout/entry.ts."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from . import db
from .config import OWNER_EMAIL

VALID_TRENDS = {"DoubleUp", "SingleUp", "FortyFiveUp", "Flat", "FortyFiveDown", "SingleDown", "DoubleDown"}


def _normalize_url(url: str) -> str:
    url = (url or "").rstrip("/")
    if url and not url.startswith("http"):
        url = "https://" + url
    return url


def _map_trend(direction: Any) -> str:
    return direction if direction in VALID_TRENDS else "Unknown"


def _headers(api_secret: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_secret:
        headers["api-secret"] = hashlib.sha1(api_secret.encode("utf-8")).hexdigest()
    return headers


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_ts(value: Any) -> datetime | None:
    """Parse Nightscout timestamps: epoch ms or ISO strings."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def map_treatment(t: dict[str, Any]) -> dict[str, Any]:
    et = (t.get("eventType") or "").strip()
    et_low = et.lower()
    insulin = _num(t.get("insulin"))
    carbs = _num(t.get("carbs"))
    glucose = _num(t.get("glucose"))

    ttype = "other"
    insulin_type = None

    if insulin > 0 or "bolus" in et_low or "correction" in et_low or et_low == "insulin":
        ttype = "insulin"
        if "combo" in et_low or "multiwave" in et_low or "extended" in et_low:
            insulin_type = "mixed"
        else:
            insulin_type = "rapid"
    elif carbs > 0 or "carb" in et_low or "meal" in et_low or "snack" in et_low:
        ttype = "carb"
        if insulin > 0:
            ttype = "insulin"
    elif "temp basal" in et_low or "temporary basal" in et_low or et_low == "tempbasal":
        ttype = "tempbasal"
    elif "suspen" in et_low or "pump suspended" in et_low or "pump resumed" in et_low or "resume" in et_low:
        ttype = "suspension"
    elif glucose > 0 or "bg check" in et_low or "blood glucose" in et_low or "finger" in et_low:
        ttype = "bg"
    elif "note" in et_low or "announcement" in et_low or et_low == "":
        ttype = "note"

    if ttype == "other" and insulin > 0:
        ttype, insulin_type = "insulin", "rapid"
    if ttype == "other" and carbs > 0:
        ttype = "carb"

    ts = _parse_ts(t.get("created_at") or t.get("timestamp"))
    mapped: dict[str, Any] = {
        "event_type": et or None,
        "type": ttype,
        "timestamp": _iso(ts) if ts else None,
        "source": "nightscout",
        "ns_id": t.get("_id"),
    }
    if insulin > 0:
        mapped["amount"] = insulin
    elif carbs > 0 and ttype == "carb":
        mapped["amount"] = carbs
    if insulin_type:
        mapped["insulin_type"] = insulin_type
    if _num(t.get("duration")) > 0:
        mapped["duration"] = _num(t.get("duration"))
    if t.get("percent") is not None:
        mapped["percent"] = t.get("percent")
    if t.get("absolute") is not None:
        mapped["absolute"] = t.get("absolute")
    if glucose > 0:
        mapped["glucose"] = glucose
    if t.get("glucoseType"):
        mapped["glucose_type"] = t.get("glucoseType")
    if t.get("preBolus"):
        mapped["preBolus"] = t.get("preBolus")
    if t.get("notes"):
        mapped["notes"] = t.get("notes")
    if ttype == "insulin" and carbs > 0:
        prefix = mapped.get("notes")
        mapped["notes"] = (f"{prefix} | " if prefix else "") + f"Carbs: {carbs:g}g"
    return {k: v for k, v in mapped.items() if v is not None}


def _get_config() -> dict[str, str] | None:
    settings = db.query_entities("UserSettings", {"owner_email": OWNER_EMAIL}, "-created_date", 1)
    if not settings or not settings[0].get("nightscout_url"):
        return None
    return {
        "url": _normalize_url(settings[0]["nightscout_url"]),
        "api_secret": settings[0].get("nightscout_api_secret") or "",
        "settings_id": settings[0]["id"],
    }


async def _sync_profile(client: httpx.AsyncClient, ns_url: str, headers: dict) -> None:
    res = await client.get(f"{ns_url}/api/v1/profile.json", headers=headers)
    if res.status_code >= 400:
        return
    profiles = res.json()
    if not isinstance(profiles, list):
        profiles = [profiles]
    for p in profiles:
        profile_name = p.get("defaultProfile") or "Default"
        store = (p.get("store") or {}).get(profile_name) or {}
        data = {
            "profile_name": profile_name,
            "dia": store.get("dia"),
            "carb_ratio": json.dumps(store.get("carbratio") or []),
            "isf": json.dumps(store.get("sens") or []),
            "basal": json.dumps(store.get("basal") or []),
            "target_low": json.dumps(store.get("target_low") or []),
            "target_high": json.dumps(store.get("target_high") or []),
            "carbs_hr": store.get("carbs_hr"),
            "timezone": store.get("timezone"),
            "units": store.get("units") or p.get("units"),
            "synced_at": _iso(datetime.now(timezone.utc)),
            "owner_email": OWNER_EMAIL,
        }
        existing = db.query_entities("NightscoutProfile", {"profile_name": profile_name, "owner_email": OWNER_EMAIL})
        if existing:
            db.update_entity("NightscoutProfile", existing[0]["id"], data)
        else:
            db.create_entity("NightscoutProfile", data)


async def handle(body: dict[str, Any]) -> dict[str, Any]:
    action = body.get("action")
    config = _get_config()
    if not config:
        return {"error": "Nightscout not configured. Add your Nightscout URL in Connections.", "_status": 400}
    ns_url, api_secret = config["url"], config["api_secret"]
    host = ns_url.split("//", 1)[-1].split("/", 1)[0]
    if "." not in host:
        return {
            "error": f'"{host}" is not a valid Nightscout address. Enter the full site URL, e.g. https://yoursite.up.railway.app',
            "_status": 400,
        }
    headers = _headers(api_secret)

    async with httpx.AsyncClient(timeout=60) as client:
        if action == "test":
            no_auth = await client.get(f"{ns_url}/api/v1/entries.json?count=1", headers={"Accept": "application/json"})
            authed = await client.get(f"{ns_url}/api/v1/entries.json?count=1", headers=headers)
            working = authed if authed.status_code < 400 else no_auth if no_auth.status_code < 400 else None
            if working is None:
                return {"error": f"Nightscout unreachable. Status: {authed.status_code}", "_status": 502}
            entries = working.json()
            latest = entries[0] if entries else None
            return {
                "ok": True,
                "version": "connected",
                "name": "Nightscout",
                "latest_reading": {"value": latest.get("sgv"), "time": latest.get("dateString")} if latest else None,
            }

        if action == "sync":
            days = int(body.get("days") or 1)
            existing = db.query_entities(
                "GlucoseReading", {"owner_email": OWNER_EMAIL, "source": "nightscout"}, "-timestamp", 1
            )
            if existing:
                from_time = _parse_ts(existing[0]["timestamp"])
            else:
                from_time = datetime.now(timezone.utc) - timedelta(days=days)
            from_iso = _iso(from_time)

            count = days * 288
            entries_res = await client.get(f"{ns_url}/api/v1/entries.json?count={count}&type=sgv", headers=headers)
            if entries_res.status_code >= 400:
                return {"error": f"Entries fetch failed: {entries_res.status_code}", "_status": 502}
            entries = entries_res.json()

            mbg_res = await client.get(f"{ns_url}/api/v1/entries.json?find[type]=mbg&count=100", headers=headers)
            mbg_entries = mbg_res.json() if mbg_res.status_code < 400 else []

            treatments_res = await client.get(
                f"{ns_url}/api/v1/treatments.json?find[created_at][$gte]={from_iso}&count=1000", headers=headers
            )
            treatments = treatments_res.json() if treatments_res.status_code < 400 else []

            readings = []
            for e in entries:
                ts = _parse_ts(e.get("date") or e.get("dateString"))
                if e.get("sgv") and ts and ts > from_time:
                    readings.append(
                        {
                            "value": round(e["sgv"]),
                            "timestamp": _iso(ts),
                            "trend": _map_trend(e.get("direction")),
                            "source": "nightscout",
                            "owner_email": OWNER_EMAIL,
                        }
                    )

            mbg_treatments = []
            for e in mbg_entries:
                ts = _parse_ts(e.get("dateString") or e.get("date"))
                if e.get("mbg") and ts and ts > from_time:
                    mbg_treatments.append(
                        {
                            "type": "bg",
                            "event_type": "BG Check",
                            "timestamp": _iso(ts),
                            "glucose": e["mbg"],
                            "source": "nightscout",
                            "owner_email": OWNER_EMAIL,
                        }
                    )

            mapped_treatments = [
                {**map_treatment(t), "owner_email": OWNER_EMAIL}
                for t in treatments
                if _parse_ts(t.get("created_at")) and _parse_ts(t.get("created_at")) > from_time
            ] + mbg_treatments

            existing_treatments = db.query_entities(
                "Treatment", {"source": "nightscout", "owner_email": OWNER_EMAIL}, "-timestamp", 500
            )
            existing_ns_ids = {t.get("ns_id") for t in existing_treatments if t.get("ns_id")}
            new_treatments = [t for t in mapped_treatments if not t.get("ns_id") or t["ns_id"] not in existing_ns_ids]

            readings_created = 0
            if readings:
                from .readings import persist_readings_deduped
                readings_created, _ = persist_readings_deduped(readings)
            if new_treatments:
                db.bulk_create_entities("Treatment", new_treatments)

            if body.get("profile", True):
                await _sync_profile(client, ns_url, headers)
            _touch_last_sync(config["settings_id"])
            return {"ok": True, "readings_synced": readings_created, "treatments_synced": len(new_treatments)}

        if action == "backfill":
            days = int(body.get("days") or 14)
            count = min(days * 288, 5760)
            from_dt = datetime.now(timezone.utc) - timedelta(days=days)
            from_iso = _iso(from_dt)

            entries_res = await client.get(f"{ns_url}/api/v1/entries.json?count={count}&type=sgv", headers=headers)
            if entries_res.status_code >= 400:
                return {"error": f"Entries fetch failed: {entries_res.status_code}", "_status": 502}
            entries = entries_res.json()

            mbg_res = await client.get(f"{ns_url}/api/v1/entries.json?find[type]=mbg&count=1000", headers=headers)
            mbg_entries = mbg_res.json() if mbg_res.status_code < 400 else []

            treatments_res = await client.get(
                f"{ns_url}/api/v1/treatments.json?find[created_at][$gte]={from_iso}&count=2000", headers=headers
            )
            treatments = treatments_res.json() if treatments_res.status_code < 400 else []

            readings = []
            for e in entries:
                ts = _parse_ts(e.get("date") or e.get("dateString"))
                if e.get("sgv") and ts:
                    readings.append(
                        {
                            "value": round(e["sgv"]),
                            "timestamp": _iso(ts),
                            "trend": _map_trend(e.get("direction")),
                            "source": "nightscout",
                            "owner_email": OWNER_EMAIL,
                        }
                    )

            mbg_treatments = [
                {
                    "type": "bg",
                    "event_type": "BG Check",
                    "timestamp": _iso(_parse_ts(e.get("dateString") or e.get("date"))),
                    "glucose": e["mbg"],
                    "source": "nightscout",
                    "owner_email": OWNER_EMAIL,
                }
                for e in mbg_entries
                if e.get("mbg") and _parse_ts(e.get("dateString") or e.get("date"))
            ]

            mapped_treatments = [
                {**map_treatment(t), "owner_email": OWNER_EMAIL} for t in treatments if t.get("created_at")
            ] + mbg_treatments

            db.delete_entities_where("GlucoseReading", {"source": "nightscout", "owner_email": OWNER_EMAIL})
            db.delete_entities_where("Treatment", {"source": "nightscout", "owner_email": OWNER_EMAIL})

            readings_created = 0
            if readings:
                from .readings import persist_readings_deduped
                readings_created, _ = persist_readings_deduped(readings)
            if mapped_treatments:
                db.bulk_create_entities("Treatment", mapped_treatments)

            await _sync_profile(client, ns_url, headers)
            _touch_last_sync(config["settings_id"])
            return {"ok": True, "readings_synced": readings_created, "treatments_synced": len(mapped_treatments)}

    return {"error": "Unknown action", "_status": 400}


def _touch_last_sync(settings_id: str) -> None:
    db.update_entity("UserSettings", settings_id, {"last_nightscout_sync": _iso(datetime.now(timezone.utc))})
