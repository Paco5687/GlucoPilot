"""Glooko sync — pump/treatment data via Glooko's private web API.

Why: Glooko is cloud-connected to both Tandem (t:slim X2 / Mobi, via a linked
Tandem Source account) and Omnipod 5 (cloud-to-cloud, ~1 h delay). That makes
it a failsafe treatment source if the Tandem path breaks, the escape hatch if
Emily returns to Omnipod, and a backfill source for treatment gaps.

Endpoints and login flow are modeled on nightscout-connect's glooko driver
(the community bridge). This is an unofficial API: field names vary by pump,
so mappers are defensive and unknown records are skipped, not guessed at.
Accounts with 2FA enabled cannot be scraped — disable 2FA on the Glooko
account used here.

NOTE: v2 `scheduled_basals` carries programmed/temp basal segments. Automated
micro-basal series (Control-IQ / Omnipod 5) live in the v3 graph API and can
be added once real payloads are available to inspect.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from . import db
from .config import OWNER_EMAIL
from .db import config_value, set_config_value

log = logging.getLogger("glucopilot.glooko")

REGION_HOSTS = {
    "us": "api.glooko.com",
    "eu": "eu.api.glooko.com",
    "ca": "ca.api.glooko.com",
}
WEB_ORIGINS = {
    "us": "https://my.glooko.com",
    "eu": "https://eu.my.glooko.com",
    "ca": "https://ca.my.glooko.com",
}

DEVICE_INFO = {
    "applicationType": "logbook",
    "os": "ios",
    "osVersion": "17.0",
    "device": "iPhone",
    "deviceManufacturer": "Apple",
    "deviceModel": "iPhone",
    "serialNumber": "",
    "deviceId": "glucopilot",
    "applicationVersion": "6.1.0",
    "buildNumber": "0",
    "gitHash": "0",
}

TREATMENT_TOLERANCE = 90  # seconds; cross-source duplicate window
READING_TOLERANCE = 240

_sync_lock = asyncio.Lock()


def _region() -> str:
    return (config_value("glooko_region", "us") or "us").strip().lower()


def _base_url() -> str:
    return f"https://{REGION_HOSTS.get(_region(), REGION_HOSTS['us'])}"


def _headers() -> dict[str, str]:
    origin = WEB_ORIGINS.get(_region(), WEB_ORIGINS["us"])
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Origin": origin,
        "Referer": origin + "/",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _configured() -> bool:
    return bool(config_value("glooko_email") and config_value("glooko_password"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_ts(value: Any) -> datetime | None:
    # Glooko v2 timestamps are UTC; naive values are treated as UTC. Verify
    # against real payloads on first live sync.
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _first(record: dict, *keys) -> Any:
    for key in keys:
        if record.get(key) is not None:
            return record[key]
    return None


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _login(client: httpx.AsyncClient) -> dict:
    response = await client.post(
        f"{_base_url()}/api/v2/users/sign_in",
        json={
            "userLogin": {"email": config_value("glooko_email"), "password": config_value("glooko_password")},
            "deviceInformation": DEVICE_INFO,
        },
        headers=_headers(),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Glooko login failed ({response.status_code}): {response.text[:200]}")
    data = response.json() if response.text else {}
    if isinstance(data, dict) and data.get("twoFaRequired"):
        raise RuntimeError("This Glooko account requires 2FA, which the sync cannot handle. Disable 2FA for it.")
    return data if isinstance(data, dict) else {}


async def _fetch_list(client: httpx.AsyncClient, path: str, key: str, since: datetime, limit: int = 2000) -> list[dict]:
    """Paginated fetch: Glooko cursors by (lastUpdatedAt, lastGuid) and flags
    the final page with lastPage=true. lastUpdatedAt filters on sync time,
    not pump time."""
    items: list[dict] = []
    cursor_updated = _iso(since)
    cursor_guid = "1e0c094e-1e54-4a4f-8e6a-f94484b53789"
    for _ in range(50):  # page cap
        response = await client.get(
            f"{_base_url()}{path}",
            params={"lastGuid": cursor_guid, "lastUpdatedAt": cursor_updated, "limit": limit},
            headers=_headers(),
        )
        if response.status_code >= 400:
            log.warning("glooko %s failed: %s %s", path, response.status_code, response.text[:200])
            break
        data = response.json()
        if not isinstance(data, dict):
            items.extend(data if isinstance(data, list) else [])
            break
        page = data.get(key)
        items.extend(page if isinstance(page, list) else [])
        if data.get("lastPage", True):
            break
        cursor_updated = data.get("lastUpdatedAt") or cursor_updated
        cursor_guid = data.get("lastGuid") or cursor_guid
    return items


# ── mapping ──────────────────────────────────────────────────────────────


def _map_bolus(r: dict) -> list[dict]:
    ts = _parse_ts(_first(r, "pumpTimestamp", "timestamp", "deviceTimestamp", "displayTime"))
    amount = _num(_first(r, "insulinDelivered", "totalInsulinDelivered", "units", "amount", "value"))
    if ts is None or not amount:
        return []
    mapped = {
        "type": "insulin",
        "event_type": _first(r, "bolusType", "type") or "Bolus",
        "timestamp": _iso(ts),
        "amount": amount,
        "insulin_type": "rapid",
        "source": "glooko",
        "owner_email": OWNER_EMAIL,
    }
    iob = _num(r.get("insulinOnBoard"))
    notes = []
    carbs = _num(_first(r, "carbsInput", "carbInput", "carbs"))
    if carbs:
        notes.append(f"Carbs: {carbs:g}g")
    if iob is not None:
        notes.append(f"IOB: {iob:g}U")
    if r.get("isManual"):
        notes.append("manual")
    if notes:
        mapped["notes"] = " | ".join(notes)
    guid = _first(r, "guid", "id")
    if guid:
        mapped["ns_id"] = f"glooko-{guid}"
    results = [mapped]
    if carbs:
        # Emit the carb entry separately so post-meal pattern analysis sees it
        # (matches how the pump-CSV import modeled carbs).
        carb_entry = {
            "type": "carb",
            "event_type": "Carbs",
            "timestamp": _iso(ts),
            "amount": carbs,
            "source": "glooko",
            "owner_email": OWNER_EMAIL,
        }
        if guid:
            carb_entry["ns_id"] = f"glooko-{guid}-carbs"
        results.append(carb_entry)
    return results


def _map_food(r: dict) -> dict | None:
    ts = _parse_ts(_first(r, "pumpTimestamp", "timestamp", "deviceTimestamp", "displayTime"))
    carbs = _num(_first(r, "carbs", "carbsCount", "value"))
    if ts is None or not carbs:
        return None
    mapped = {
        "type": "carb",
        "event_type": "Carbs",
        "timestamp": _iso(ts),
        "amount": carbs,
        "source": "glooko",
        "owner_email": OWNER_EMAIL,
    }
    guid = _first(r, "guid", "id")
    if guid:
        mapped["ns_id"] = f"glooko-{guid}"
    return mapped


def _map_insulin(r: dict) -> dict | None:
    ts = _parse_ts(_first(r, "pumpTimestamp", "timestamp", "deviceTimestamp", "displayTime"))
    amount = _num(_first(r, "units", "value", "amount"))
    if ts is None or not amount:
        return None
    kind = str(_first(r, "insulinType", "type") or "").lower()
    mapped = {
        "type": "insulin",
        "event_type": "Insulin (logged)",
        "timestamp": _iso(ts),
        "amount": amount,
        "insulin_type": "long" if "long" in kind or "basal" in kind else "rapid",
        "source": "glooko",
        "owner_email": OWNER_EMAIL,
    }
    guid = _first(r, "guid", "id")
    if guid:
        mapped["ns_id"] = f"glooko-{guid}"
    return mapped


def _map_basal(r: dict) -> dict | None:
    ts = _parse_ts(_first(r, "pumpTimestamp", "timestamp", "deviceTimestamp", "displayTime"))
    rate = _num(_first(r, "rate", "value", "units"))
    if ts is None or rate is None:
        return None
    if r.get("durationMinutes") is not None:
        duration = _num(r.get("durationMinutes"))
    else:
        # v2 scheduledBasals durations are seconds (verified against live data)
        duration = _num(r.get("duration"))
        if duration:
            duration = duration / 60
    mapped = {
        "type": "tempbasal",
        "event_type": "Temp Basal",
        "timestamp": _iso(ts),
        "absolute": rate,
        "source": "glooko",
        "owner_email": OWNER_EMAIL,
    }
    if duration:
        mapped["duration"] = duration
    guid = _first(r, "guid", "id")
    if guid:
        mapped["ns_id"] = f"glooko-{guid}"
    return mapped


def _map_reading(r: dict) -> dict | None:
    ts = _parse_ts(_first(r, "pumpTimestamp", "timestamp", "deviceTimestamp", "displayTime", "updatedAt"))
    value = _num(_first(r, "value", "glucose", "sgv"))
    if ts is None or not value:
        return None
    if value > 1000:  # Glooko scales some values by 100
        value = value / 100
    if value < 30:  # mmol/L → mg/dL
        value = value * 18.0143
    return {
        "value": round(value),
        "timestamp": _iso(ts),
        "trend": "Unknown",
        "source": "glooko",
        "owner_email": OWNER_EMAIL,
    }


# ── persistence with cross-source dedup ─────────────────────────────────


def _persist_treatments(mapped: list[dict]) -> tuple[int, int]:
    existing = db.query_entities("Treatment", {"owner_email": OWNER_EMAIL}, "-timestamp", 1000000)
    existing_ns_ids = {t.get("ns_id") for t in existing if t.get("ns_id")}
    by_type: dict[str, list[float]] = {}
    for t in existing:
        ts = _parse_ts(t.get("timestamp"))
        if ts:
            by_type.setdefault(t.get("type") or "other", []).append(ts.timestamp())
    for lst in by_type.values():
        lst.sort()

    import bisect

    def near(ttype: str, epoch: float) -> bool:
        lst = by_type.get(ttype, [])
        i = bisect.bisect_left(lst, epoch)
        return any(
            0 <= j < len(lst) and abs(lst[j] - epoch) <= TREATMENT_TOLERANCE for j in (i - 1, i)
        )

    created = skipped = 0
    for m in sorted(mapped, key=lambda x: x["timestamp"]):
        epoch = _parse_ts(m["timestamp"]).timestamp()
        if (m.get("ns_id") and m["ns_id"] in existing_ns_ids) or near(m["type"], epoch):
            skipped += 1
            continue
        db.create_entity("Treatment", m)
        if m.get("ns_id"):
            existing_ns_ids.add(m["ns_id"])
        bisect.insort(by_type.setdefault(m["type"], []), epoch)
        created += 1
    return created, skipped


def _persist_readings(mapped: list[dict]) -> tuple[int, int]:
    from .readings import persist_readings_deduped

    return persist_readings_deduped(mapped, READING_TOLERANCE)


# ── actions ──────────────────────────────────────────────────────────────


async def _sync(days: int, include_cgm: bool) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
        await _login(client)

        boluses = await _fetch_list(client, "/api/v2/pumps/normal_boluses", "normalBoluses", since)
        basals = await _fetch_list(client, "/api/v2/pumps/scheduled_basals", "scheduledBasals", since)
        foods = await _fetch_list(client, "/api/v2/foods", "foods", since)
        insulins = await _fetch_list(client, "/api/v2/insulins", "insulins", since)
        readings = (
            await _fetch_list(client, "/api/v2/cgm/readings", "readings", since) if include_cgm else []
        )

    treatments = [m for r in boluses for m in _map_bolus(r)] + [
        m
        for m in (
            [_map_basal(r) for r in basals]
            + [_map_food(r) for r in foods]
            + [_map_insulin(r) for r in insulins]
        )
        if m
    ]
    t_created, t_skipped = _persist_treatments(treatments)

    r_created = r_skipped = 0
    if readings:
        mapped_readings = [m for m in (_map_reading(r) for r in readings) if m]
        r_created, r_skipped = _persist_readings(mapped_readings)

    return {
        "ok": True,
        "treatments_synced": t_created,
        "treatments_skipped": t_skipped,
        "readings_synced": r_created,
        "readings_skipped": r_skipped,
        "fetched": {
            "boluses": len(boluses),
            "basals": len(basals),
            "foods": len(foods),
            "insulins": len(insulins),
            "cgm": len(readings),
        },
    }


async def handle(body: dict[str, Any]) -> dict[str, Any]:
    action = body.get("action")

    if action == "status":
        return {
            "configured": _configured(),
            "connected": config_value("glooko_verified") == "true",
            "region": _region(),
            "last_sync": config_value("glooko_last_sync") or None,
        }

    if action == "configure":
        if body.get("email"):
            set_config_value("glooko_email", str(body["email"]).strip())
        if body.get("password"):
            set_config_value("glooko_password", str(body["password"]))
        if body.get("region"):
            set_config_value("glooko_region", str(body["region"]).strip().lower())
        set_config_value("glooko_verified", "")
        return {"ok": True}

    if action == "disconnect":
        for key in ("glooko_email", "glooko_password", "glooko_verified"):
            set_config_value(key, "")
        return {"success": True}

    if not _configured():
        return {"error": "Glooko is not configured. Enter your Glooko account email and password.", "_status": 400}

    if action == "test":
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                login_data = await _login(client)
                profile = await client.get(f"{_base_url()}/api/v3/session/users", headers=_headers())
                profile_data = profile.json() if profile.status_code < 400 else {}
        except Exception as err:
            set_config_value("glooko_verified", "")
            return {"error": str(err), "_status": 502}
        set_config_value("glooko_verified", "true")
        return {"ok": True, "profile": bool(profile_data), "login": bool(login_data)}

    if action in ("sync", "backfill"):
        days = min(int(body.get("days") or (30 if action == "backfill" else 2)), 90)
        include_cgm = bool(body.get("include_cgm"))
        async with _sync_lock:
            try:
                result = await _sync(days, include_cgm)
            except Exception as err:
                log.exception("glooko sync failed")
                return {"error": f"Glooko sync failed: {err}", "_status": 502}
        set_config_value("glooko_verified", "true")
        set_config_value("glooko_last_sync", _iso(datetime.now(timezone.utc)))
        return result

    return {"error": "Unknown action", "_status": 400}
