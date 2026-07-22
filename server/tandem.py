"""Tandem Source (t:connect) pump sync — treatments direct from Tandem's cloud.

Uses the tconnectsync package (jwoglom) as a library: its TandemSourceApi
client logs into Tandem Source with the user's account credentials, fetches the
raw pump event stream (t:slim X2 / Mobi), and its processors decode boluses,
basal segments, suspensions, and pump events. Instead of tconnectsync's real
Nightscout uploader, we hand its processors an EntityStoreNightscout shim that
writes straight into our Treatment store (source="tandem") — no Nightscout
instance required.

Credentials are Tandem Source account email/password, stored via the config
store (tandem_email / tandem_password). This rides an undocumented Tandem API;
if Tandem changes it, bump the tconnectsync version.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from . import db
from .config import APP_TIMEZONE, OWNER_EMAIL
from .connector_provenance import can_advance_freshness, capture_payload
from .db import config_value, set_config_value
from .nightscout import map_treatment

log = logging.getLogger("glucopilot.tandem")

# tconnectsync names every standard bolus "Combo Bolus"; store it as plain
# "Bolus" (insulin_type stays rapid) but translate back when its processors
# ask for the last-uploaded entry of that eventType.
EVENTTYPE_ALIASES = {"Combo Bolus": "Bolus"}

FEATURES = ["BASAL", "BOLUS", "PUMP_EVENTS"]  # no PROFILES/CGM/DEVICE_STATUS

_sync_lock = asyncio.Lock()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_ts(value: Any) -> datetime | None:
    """Parse tconnectsync/arrow/ISO timestamps; naive values are pump-local."""
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(config_value("app_timezone", APP_TIMEZONE)))
    return dt.astimezone(timezone.utc)


class EntityStoreNightscout:
    """Duck-typed stand-in for tconnectsync's NightscoutApi, backed by the
    entity store. Only the surface its tandemsource processors use."""

    def __init__(self) -> None:
        self.created = 0
        self.skipped = 0

    # -- write path -------------------------------------------------------

    def upload_entry(self, ns_format: dict, entity: str = "treatments") -> None:
        if entity != "treatments":
            return  # CGM entries feature is disabled; Dexcom owns glucose
        entry = dict(ns_format)
        capture_payload(
            {"entity": entity, "entry": json.loads(json.dumps(entry, default=str))},
            external_id=str(entry.get("pump_event_id") or "") or None,
            observed_at=_iso(ts) if (ts := _parse_ts(entry.get("created_at"))) else None,
            fetched_count=1,
        )
        et = entry.get("eventType") or ""
        entry["eventType"] = EVENTTYPE_ALIASES.get(et, et)

        ts = _parse_ts(entry.get("created_at"))
        if ts is None:
            return
        entry["created_at"] = _iso(ts)

        pump_event_id = entry.pop("pump_event_id", "") or ""
        ns_id = f"tandem-{pump_event_id}" if pump_event_id else None
        if ns_id and db.query_entities("Treatment", {"ns_id": ns_id, "owner_email": OWNER_EMAIL}, limit=1):
            self.skipped += 1
            return

        mapped = map_treatment(entry)
        mapped["source"] = "tandem"
        mapped["owner_email"] = OWNER_EMAIL
        if ns_id:
            mapped["ns_id"] = ns_id
        if entry.get("reason"):
            mapped["reason"] = entry["reason"]
        if mapped.get("type") == "other":
            mapped["type"] = "note"
        db.create_entity("Treatment", mapped)
        self.created += 1

    def put_entry(self, ns_format: dict, entity: str) -> None:
        rid = ns_format.get("_id")
        if not rid:
            return
        capture_payload(
            {"entity": entity, "entry": json.loads(json.dumps(ns_format, default=str))},
            external_id=str(rid),
            observed_at=_iso(ts) if (ts := _parse_ts(ns_format.get("created_at"))) else None,
            fetched_count=1,
        )
        patch = {k: v for k, v in ns_format.items() if k != "_id"}
        if "created_at" in patch:
            ts = _parse_ts(patch.pop("created_at"))
            if ts:
                patch["timestamp"] = _iso(ts)
        db.update_entity("Treatment", rid, patch)

    def delete_entry(self, entity: str) -> None:
        rid = entity.rsplit("/", 1)[-1]
        db.delete_entity("Treatment", rid)

    # -- read path (dedup anchors for the processors) ----------------------

    def last_uploaded_entry(self, eventType: str, time_start=None, time_end=None):
        stored_type = EVENTTYPE_ALIASES.get(eventType, eventType)
        rows = db.query_entities(
            "Treatment",
            {"source": "tandem", "event_type": stored_type, "owner_email": OWNER_EMAIL},
            "-timestamp",
            100,
        )
        lo, hi = _parse_ts(time_start), _parse_ts(time_end)
        for r in rows:
            ts = _parse_ts(r.get("timestamp"))
            if ts is None:
                continue
            if lo and ts < lo:
                continue
            if hi and ts > hi:
                continue
            return {"created_at": r["timestamp"], "_id": r["id"], "reason": r.get("reason", "")}
        return None

    def last_uploaded_bg_entry(self, time_start=None, time_end=None):
        return None

    def last_uploaded_activity(self, activityType, time_start=None, time_end=None):
        return None

    def last_uploaded_devicestatus(self, time_start=None, time_end=None):
        return None

    def current_profile(self, time_start=None, time_end=None):
        return None

    def api_status(self):
        return {"status": "ok"}


def _configured() -> bool:
    return bool(config_value("tandem_email") and config_value("tandem_password"))


def _make_secret() -> SimpleNamespace:
    tz = config_value("app_timezone", APP_TIMEZONE)
    # helpers inside tconnectsync import its secret module directly, so patch
    # the module too, not just the namespace we pass around.
    import tconnectsync.secret as tc_secret

    tc_secret.TIMEZONE_NAME = tz
    tc_secret.FETCH_ALL_EVENT_TYPES = False
    tc_secret.PUMP_SERIAL_NUMBER = config_value("tandem_pump_serial") or None
    return SimpleNamespace(
        TIMEZONE_NAME=tz,
        FETCH_ALL_EVENT_TYPES=False,
        PUMP_SERIAL_NUMBER=config_value("tandem_pump_serial") or None,
    )


def _login_and_choose_device():
    """Blocking: authenticate against Tandem Source and pick the active pump."""
    from tconnectsync.api.tandemsource import TandemSourceApi
    from tconnectsync.sync.tandemsource.choose_device import ChooseDevice

    secret = _make_secret()
    api = TandemSourceApi(config_value("tandem_email"), config_value("tandem_password"))
    tconnect = SimpleNamespace(tandemsource=api)
    device = ChooseDevice(secret, tconnect).choose()
    return secret, tconnect, device


def _run_sync(time_start: datetime, time_end: datetime) -> dict[str, Any]:
    """Blocking: fetch + decode pump events and write treatments."""
    import arrow
    from tconnectsync.sync.tandemsource.process import ProcessTimeRange

    secret, tconnect, device = _login_and_choose_device()
    shim = EntityStoreNightscout()
    ProcessTimeRange(tconnect, shim, device, pretend=False, secret=secret, features=FEATURES).process(
        arrow.get(time_start), arrow.get(time_end)
    )
    return {
        "ok": True,
        "treatments_synced": shim.created,
        "duplicates_skipped": shim.skipped,
        "pump_serial": device.get("serialNumber"),
    }


async def handle(body: dict[str, Any]) -> dict[str, Any]:
    action = body.get("action")

    if action == "status":
        return {
            "configured": _configured(),
            "connected": config_value("tandem_verified") == "true",
            "pump_serial": config_value("tandem_pump_serial_seen") or None,
            "last_sync": config_value("tandem_last_sync") or None,
        }

    if action == "configure":
        email = (body.get("email") or "").strip()
        password = body.get("password") or ""
        if email:
            set_config_value("tandem_email", email)
        if password:
            set_config_value("tandem_password", password)
        set_config_value("tandem_verified", "")
        return {"ok": True}

    if action == "disconnect":
        for key in ("tandem_email", "tandem_password", "tandem_verified", "tandem_pump_serial_seen"):
            set_config_value(key, "")
        return {"success": True}

    if not _configured():
        return {"error": "Tandem Source is not configured. Enter your t:connect account email and password.", "_status": 400}

    if action == "test":
        try:
            _, _, device = await asyncio.to_thread(_login_and_choose_device)
        except Exception as err:
            set_config_value("tandem_verified", "")
            return {"error": f"Tandem Source login failed: {err}", "_status": 502}
        set_config_value("tandem_verified", "true")
        set_config_value("tandem_pump_serial_seen", str(device.get("serialNumber") or ""))
        return {
            "ok": True,
            "pump_serial": device.get("serialNumber"),
            "last_seen": device.get("maxDateOfEvents"),
        }

    if action in ("sync", "backfill"):
        now = datetime.now(timezone.utc)
        if action == "backfill":
            start = now - timedelta(days=min(int(body.get("days") or 30), 90))
        else:
            latest = db.query_entities(
                "Treatment", {"source": "tandem", "owner_email": OWNER_EMAIL}, "-timestamp", 1
            )
            latest_ts = _parse_ts(latest[0]["timestamp"]) if latest else None
            # 2h overlap: the processors' last-upload anchors + ns_id dedup
            # make re-reads idempotent.
            start = (latest_ts - timedelta(hours=2)) if latest_ts else now - timedelta(days=1)

        async with _sync_lock:
            try:
                result = await asyncio.to_thread(_run_sync, start, now)
            except Exception as err:
                log.exception("tandem sync failed")
                return {"error": f"Tandem sync failed: {err}", "_status": 502}
        if can_advance_freshness():
            set_config_value("tandem_verified", "true")
            set_config_value("tandem_last_sync", _iso(now))
        if result.get("pump_serial"):
            set_config_value("tandem_pump_serial_seen", str(result["pump_serial"]))
        return result

    return {"error": "Unknown action", "_status": 400}
