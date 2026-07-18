"""Background sync loop replacing Base44's scheduledSync cron function.

Every 5 minutes:  Nightscout sync (if configured + connected) — near-realtime
                  CGM feed until Dexcom/Tandem take over. Profile sync rides
                  along hourly only.
Every 5 minutes:  Dexcom EGV sync (if connected).
Every 10 minutes: Tandem Source pump sync (if verified) — Control-IQ adjusts
                  basal continuously; the pump logs a basal event every 5 min
                  and its phone app uploads opportunistically, so short polls
                  keep the basal trace near-live.
Every hour:       Oura sync, last 2 days (if connected).

All syncs are incremental and idempotent; failures are logged and retried on
the next tick. Disable with SYNC_ENABLED=false.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from . import cycle_inference, db, dexcom, dexcom_share, fitbit, glooko, google_health, health_summary, nightscout, oura, tandem
from .config import OWNER_EMAIL, env_bool

log = logging.getLogger("glucopilot.scheduler")

TICK_SECONDS = 60
DEXCOM_SHARE_INTERVAL = 60  # real-time feed; session is cached so this is one light GET
DEXCOM_INTERVAL = 5 * 60
NIGHTSCOUT_INTERVAL = 5 * 60
NIGHTSCOUT_PROFILE_INTERVAL = 60 * 60
OURA_INTERVAL = 3600  # hourly: ring uploads to Oura cloud sporadically; poll often so HR lands ASAP
FITBIT_INTERVAL = 6 * 3600
GOOGLE_HEALTH_INTERVAL = 6 * 3600
GOOGLE_HEALTH_HR_INTERVAL = 2 * 60  # intraday HR — poll often; Fitbit cloud lag (~5-15 min) is the real floor
CYCLE_INFERENCE_INTERVAL = 24 * 3600
TANDEM_INTERVAL = 10 * 60
GLOOKO_INTERVAL = 30 * 60  # Glooko's cloud feed lags ~1h; 30 min polls suffice

_last_run = {
    "dexcom_share": 0.0,
    "dexcom": 0.0,
    "nightscout": 0.0,
    "nightscout_profile": 0.0,
    "oura": 0.0,
    "tandem": 0.0,
    "glooko": 0.0,
    "fitbit": 0.0,
    "google_health": 0.0,
    "google_health_hr": 0.0,
    "cycle_inference": 0.0,
    "health_summary": 0.0,
}

HEALTH_SUMMARY_INTERVAL = 7 * 24 * 3600  # weekly, tracked by wall-clock (survives restarts)
HEALTH_SUMMARY_RETRY = 3600  # in-process throttle so a failing run doesn't hammer the 27B model


def _health_summary_due() -> bool:
    last = db.config_value("health_summary_last_run")
    if not last:
        return True  # bootstrap the first summary
    try:
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - last_dt).total_seconds() >= HEALTH_SUMMARY_INTERVAL


def _has_summary_data() -> bool:
    return bool(
        db.query_entities("LabResult", {"owner_email": OWNER_EMAIL}, "collected_date", 1)
        or _dexcom_connected()
        or _nightscout_connected()
    )


def _google_health_connected() -> bool:
    rows = db.query_entities("GoogleHealthConnection", {"owner_email": OWNER_EMAIL, "connected": True}, "-created_date", 1)
    return bool(rows)


def _dexcom_connected() -> bool:
    rows = db.query_entities("DexcomConnection", {"owner_email": OWNER_EMAIL, "connected": True}, "-created_date", 1)
    return bool(rows)


def _nightscout_connected() -> bool:
    rows = db.query_entities("UserSettings", {"owner_email": OWNER_EMAIL}, "-created_date", 1)
    return bool(rows and rows[0].get("nightscout_url") and rows[0].get("nightscout_connected"))


def _oura_connected() -> bool:
    rows = db.query_entities("OuraConnection", {"owner_email": OWNER_EMAIL, "connected": True}, "-created_date", 1)
    return bool(rows)


async def _tick() -> None:
    now = time.monotonic()

    if now - _last_run["dexcom_share"] >= DEXCOM_SHARE_INTERVAL and db.config_value("dexcom_share_verified") == "true":
        _last_run["dexcom_share"] = now
        try:
            result = await dexcom_share.handle({"action": "sync", "minutes": 60, "max_count": 12})
            if result.get("readings_synced"):
                log.info("dexcom share sync: %s", result)
        except Exception as err:
            log.warning("dexcom share sync failed: %s", err)

    if now - _last_run["dexcom"] >= DEXCOM_INTERVAL and _dexcom_connected():
        _last_run["dexcom"] = now
        try:
            result = await dexcom._sync()
            log.info("dexcom sync: %s", result)
        except Exception as err:
            log.warning("dexcom sync failed: %s", err)

    if now - _last_run["nightscout"] >= NIGHTSCOUT_INTERVAL and _nightscout_connected():
        _last_run["nightscout"] = now
        with_profile = now - _last_run["nightscout_profile"] >= NIGHTSCOUT_PROFILE_INTERVAL
        if with_profile:
            _last_run["nightscout_profile"] = now
        try:
            result = await nightscout.handle({"action": "sync", "days": 1, "profile": with_profile})
            log.info("nightscout sync: %s", result)
        except Exception as err:
            log.warning("nightscout sync failed: %s", err)

    if now - _last_run["tandem"] >= TANDEM_INTERVAL and db.config_value("tandem_verified") == "true":
        _last_run["tandem"] = now
        try:
            result = await tandem.handle({"action": "sync"})
            log.info("tandem sync: %s", result)
        except Exception as err:
            log.warning("tandem sync failed: %s", err)

    if now - _last_run["glooko"] >= GLOOKO_INTERVAL and db.config_value("glooko_verified") == "true":
        _last_run["glooko"] = now
        try:
            result = await glooko.handle({"action": "sync"})
            log.info("glooko sync: %s", result)
        except Exception as err:
            log.warning("glooko sync failed: %s", err)

    if now - _last_run["fitbit"] >= FITBIT_INTERVAL:
        rows = db.query_entities("FitbitConnection", {"owner_email": OWNER_EMAIL, "connected": True}, "-created_date", 1)
        if rows:
            _last_run["fitbit"] = now
            try:
                result = await fitbit.handle({"action": "sync", "days": 3})
                log.info("fitbit sync: %s", result)
            except Exception as err:
                log.warning("fitbit sync failed: %s", err)

    if now - _last_run["google_health_hr"] >= GOOGLE_HEALTH_HR_INTERVAL and _google_health_connected():
        _last_run["google_health_hr"] = now
        try:
            result = await google_health.handle({"action": "sync_hr", "minutes": 30})
            if result.get("created"):
                log.info("google health HR sync: %s", result)
        except Exception as err:
            log.warning("google health HR sync failed: %s", err)

    if now - _last_run["google_health"] >= GOOGLE_HEALTH_INTERVAL and _google_health_connected():
        _last_run["google_health"] = now
        try:
            result = await google_health.handle({"action": "sync", "days": 3})
            log.info("google health sync: %s", result)
        except Exception as err:
            log.warning("google health sync failed: %s", err)

    if now - _last_run["oura"] >= OURA_INTERVAL and _oura_connected():
        _last_run["oura"] = now
        try:
            result = await oura.handle_sync({"days": 2})
            log.info("oura sync: %s", result)
        except Exception as err:
            log.warning("oura sync failed: %s", err)

    if now - _last_run["cycle_inference"] >= CYCLE_INFERENCE_INTERVAL and _oura_connected():
        _last_run["cycle_inference"] = now
        try:
            await cycle_inference.infer()
        except Exception as err:
            log.warning("cycle inference failed: %s", err)

    if now - _last_run["health_summary"] >= HEALTH_SUMMARY_RETRY and _health_summary_due() and _has_summary_data():
        _last_run["health_summary"] = now
        try:
            await health_summary.generate()
            log.info("health summary regenerated")
        except Exception as err:
            log.warning("health summary failed: %s", err)


def _sync_enabled() -> bool:
    # In-app setting wins over env; re-checked every tick so the Settings
    # page toggle takes effect without a restart.
    stored = db.config_value("sync_enabled")
    if stored:
        return stored.strip().lower() in ("1", "true", "yes", "on")
    return env_bool("SYNC_ENABLED", True)


async def run() -> None:
    # Let the app settle before the first sync pass.
    await asyncio.sleep(10)
    while True:
        try:
            if _sync_enabled():
                await _tick()
        except Exception as err:
            log.error("scheduler tick failed: %s", err)
        await asyncio.sleep(TICK_SECONDS)
