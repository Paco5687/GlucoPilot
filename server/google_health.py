"""Google Health API sync — the successor to the Fitbit Web API.

Google is turning down the legacy Fitbit Web API on 2026-09-30 and replacing it
with the Google Health API (register in Google Cloud Console, Google OAuth 2.0,
one `/v4/users/me/dataTypes/{type}/dataPoints` read schema). This module is a
drop-in second source that writes the same daily wearable metrics as
``server/fitbit.py`` into the shared **FitbitDaily** entity, so everything
downstream (Dashboard, Insights, Report) is untouched — only the source flips.

It runs *alongside* Fitbit during Google's May–Sept 2026 side-by-side window: we
verify parity here, then disconnect Fitbit. Rows written here carry
``source="google_health"``.

OAuth notes that differ from Fitbit:
  - Auth server is Google (accounts.google.com / oauth2.googleapis.com).
  - The authorize URL must use ``access_type=offline`` + ``prompt=consent`` to
    get a refresh token (the frontend builds it).
  - Google refresh tokens do NOT rotate on refresh — we keep the stored one.
  - Apps left in OAuth "Testing" status issue refresh tokens that expire after
    ~7 days; publish the Cloud app to Production for an always-on sync.

Data-type coverage: steps, active minutes, daily resting HR, sleep, and daily
SpO2 are mapped from documented value schemas. Calories, respiratory rate, and
skin-temperature dataType identifiers aren't fully documented — use the ``probe``
action against the live account to capture their exact names/shapes, then add
them to ``_FETCHERS``.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx

from . import db
from .config import OWNER_EMAIL, env
from .db import config_value, set_config_value

log = logging.getLogger("glucopilot.google_health")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://health.googleapis.com/v4"

# Read-only scopes covering our metrics. (Emily granted more on the consent
# screen — ECG/IRN/write — but the token only needs these three to read.)
SCOPES = (
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly "
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly "
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly"
)

_sync_lock = asyncio.Lock()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(config_value("app_timezone", "America/New_York"))
    except Exception:
        return ZoneInfo("America/New_York")


def _local_date(iso_ts: str, tz: ZoneInfo) -> str | None:
    """Local calendar date (YYYY-MM-DD) for an RFC-3339 timestamp."""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz).date().isoformat()
    except (ValueError, AttributeError):
        return None


def _date_obj(d: dict) -> str | None:
    """YYYY-MM-DD from a Google {year,month,day} Date object."""
    try:
        return f"{int(d['year']):04d}-{int(d['month']):02d}-{int(d['day']):02d}"
    except (KeyError, TypeError, ValueError):
        return None


def _get_connection() -> dict[str, Any] | None:
    rows = db.query_entities("GoogleHealthConnection", {"owner_email": OWNER_EMAIL}, "-created_date", 1)
    return rows[0] if rows else None


def _save_tokens(tokens: dict[str, Any], existing: dict[str, Any] | None) -> dict[str, Any]:
    data = {
        "owner_email": OWNER_EMAIL,
        "access_token": tokens["access_token"],
        # Google returns a refresh_token only on the first offline consent; on
        # refresh it's absent, so fall back to the stored one.
        "refresh_token": tokens.get("refresh_token") or (existing or {}).get("refresh_token", ""),
        "expires_at": (
            datetime.now(timezone.utc) + timedelta(seconds=int(tokens.get("expires_in", 3600)))
        ).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "connected": True,
    }
    if existing:
        return db.update_entity("GoogleHealthConnection", existing["id"], data)
    return db.create_entity("GoogleHealthConnection", data)


async def _access_token(client: httpx.AsyncClient) -> str:
    conn = _get_connection()
    if not conn or not conn.get("connected"):
        raise RuntimeError("Google Health is not connected.")
    try:
        expires = datetime.fromisoformat(conn.get("expires_at", "").replace("Z", "+00:00"))
    except ValueError:
        expires = datetime.now(timezone.utc)
    if expires > datetime.now(timezone.utc) + timedelta(minutes=5):
        return conn["access_token"]
    res = await client.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": conn.get("refresh_token", ""),
            "client_id": config_value("google_health_client_id"),
            "client_secret": config_value("google_health_client_secret"),
        },
    )
    if res.status_code >= 400:
        raise RuntimeError(f"Google Health token refresh failed ({res.status_code}): {res.text[:200]}")
    tokens = res.json()
    _save_tokens(tokens, conn)
    return tokens["access_token"]


async def _list_points(
    client: httpx.AsyncClient,
    token: str,
    data_type: str,
    start: str,
    end: str,
    time_field: str | None = None,
    max_pages: int = 25,
) -> list[dict]:
    """All dataPoints for a dataType. Tries a server-side time filter when
    ``time_field`` is given; on a 400 (unsupported filter) it retries unfiltered
    and lets the caller date-filter client-side. Non-fatal: logs and returns []
    on other errors so one bad type never kills the whole sync."""
    base = f"{API}/users/me/dataTypes/{data_type}/dataPoints"
    base_params: dict[str, Any] = {"pageSize": 1000}
    if time_field:
        base_params["filter"] = f'{time_field} >= "{start}T00:00:00Z" AND {time_field} < "{end}T00:00:00Z"'

    points: list[dict] = []
    page_token: str | None = None
    for _ in range(max_pages):
        params = dict(base_params)
        if page_token:
            params["pageToken"] = page_token
        res = await client.get(base, params=params, headers={"Authorization": f"Bearer {token}"})
        if res.status_code == 400 and "filter" in base_params:
            base_params.pop("filter", None)  # filter rejected — retry unfiltered
            page_token, points = None, []
            continue
        if res.status_code >= 400:
            log.warning("google health %s failed: %s %s", data_type, res.status_code, res.text[:200])
            return points
        data = res.json()
        points.extend(data.get("dataPoints", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return points


# --- per-metric extractors: each fills day-keyed dict fields for [start,end) ---

def _in_window(d: str | None, start: str, end: str) -> bool:
    return bool(d and start <= d < end)


async def _list_interval(
    client: httpx.AsyncClient, token: str, data_type: str, value_key: str, start: str, max_pages: int = 80
) -> list[tuple[str, dict]]:
    """Page a per-minute interval dataType NEWEST-FIRST (the default order),
    stopping once points fall before ``start``. Returns (local_date, value)
    pairs. Interval types (steps, active-minutes) have no reliable server-side
    time filter — every filter-field spelling 400s — so we page and early-break
    instead. Date comes from the point's own civilStartTime (no tz math)."""
    base = f"{API}/users/me/dataTypes/{data_type}/dataPoints"
    out: list[tuple[str, dict]] = []
    page_token: str | None = None
    tz = _tz()
    for _ in range(max_pages):
        params: dict[str, Any] = {"pageSize": 1000}
        if page_token:
            params["pageToken"] = page_token
        res = await client.get(base, params=params, headers={"Authorization": f"Bearer {token}"})
        if res.status_code >= 400:
            log.warning("google health %s failed: %s %s", data_type, res.status_code, res.text[:200])
            return out
        data = res.json()
        stop = False
        for p in data.get("dataPoints", []):
            v = p.get(value_key) or {}
            interval = v.get("interval") or {}
            d = _date_obj(((interval.get("civilStartTime") or {}).get("date")) or {}) or _local_date(
                interval.get("startTime") or "", tz
            )
            if d and d < start:
                stop = True  # newest-first, so everything after this is older too
                continue
            out.append((d, v))
        page_token = data.get("nextPageToken")
        if stop or not page_token:
            break
    return out


async def _fetch_steps(client, token, by_day, day, s, e, tz):
    for d, v in await _list_interval(client, token, "steps", "steps", s):
        if _in_window(d, s, e) and v.get("count") is not None:
            day(d)["steps"] = day(d).get("steps", 0) + int(v["count"])


async def _fetch_active_minutes(client, token, by_day, day, s, e, tz):
    # There's no total field — active minutes are split into activeMinutesByActivityLevel
    # ({activityLevel, activeMinutes-as-string}). Sum the non-light levels so this
    # matches Fitbit's "active minutes" (fairly+very active); LIGHT ~= all wear time.
    for d, v in await _list_interval(client, token, "active-minutes", "activeMinutes", s):
        if not _in_window(d, s, e):
            continue
        mins = 0
        for lvl in v.get("activeMinutesByActivityLevel") or []:
            if (lvl.get("activityLevel") or "").upper() in ("LIGHT", "SEDENTARY"):
                continue
            try:
                mins += int(lvl.get("activeMinutes") or 0)
            except (TypeError, ValueError):
                continue
        if mins:
            day(d)["active_minutes"] = day(d).get("active_minutes", 0) + mins


async def _fetch_resting_hr(client, token, by_day, day, s, e, tz):
    for p in await _list_points(client, token, "daily-resting-heart-rate", s, e):
        v = p.get("dailyRestingHeartRate") or {}
        d = _date_obj(v.get("date") or {})
        if _in_window(d, s, e) and v.get("beatsPerMinute") is not None:
            day(d)["resting_heart_rate"] = int(v["beatsPerMinute"])


async def _fetch_spo2(client, token, by_day, day, s, e, tz):
    for p in await _list_points(client, token, "daily-oxygen-saturation", s, e):
        v = p.get("dailyOxygenSaturation") or {}
        d = _date_obj(v.get("date") or {})
        if not _in_window(d, s, e):
            continue
        if v.get("averagePercentage") is not None:
            day(d)["spo2_avg"] = round(float(v["averagePercentage"]), 1)
        if v.get("lowerBoundPercentage") is not None:
            day(d)["spo2_min"] = round(float(v["lowerBoundPercentage"]), 1)


async def _fetch_sleep(client, token, by_day, day, s, e, tz):
    for p in await _list_points(client, token, "sleep", s, e, "sleep.interval.start_time"):
        v = p.get("sleep") or {}
        if (v.get("metadata") or {}).get("nap"):
            continue  # main sleep only, matching the Fitbit behaviour
        itv = v.get("interval") or {}
        # Fitbit keyed sleep by the wake date; use the interval end.
        d = _local_date(itv.get("endTime") or itv.get("startTime") or "", tz)
        summary = v.get("summary") or {}
        asleep = summary.get("minutesAsleep")
        in_bed = summary.get("minutesInSleepPeriod")
        if _in_window(d, s, e) and asleep is not None:
            day(d)["sleep_minutes"] = int(asleep)
            if in_bed:
                day(d)["sleep_efficiency"] = round(int(asleep) / int(in_bed) * 100)


async def _fetch_respiratory_rate(client, token, by_day, day, s, e, tz):
    for p in await _list_points(client, token, "daily-respiratory-rate", s, e):
        v = p.get("dailyRespiratoryRate") or {}
        d = _date_obj(v.get("date") or {})
        if _in_window(d, s, e) and v.get("breathsPerMinute") is not None:
            day(d)["breathing_rate"] = round(float(v["breathsPerMinute"]), 1)


async def _fetch_hrv(client, token, by_day, day, s, e, tz):
    # HRV is the strongest wearable signal for cross-domain analysis (autonomic
    # stress/recovery). Daily-keyed, so it backfills a full year cheaply.
    for p in await _list_points(client, token, "daily-heart-rate-variability", s, e):
        v = p.get("dailyHeartRateVariability") or {}
        d = _date_obj(v.get("date") or {})
        if not _in_window(d, s, e):
            continue
        if v.get("averageHeartRateVariabilityMilliseconds") is not None:
            day(d)["hrv"] = round(float(v["averageHeartRateVariabilityMilliseconds"]), 1)
        if v.get("nonRemHeartRateBeatsPerMinute") is not None:
            day(d)["nonrem_heart_rate"] = int(float(v["nonRemHeartRateBeatsPerMinute"]))


_FETCHERS: list[Callable] = [
    _fetch_steps,
    _fetch_active_minutes,
    _fetch_resting_hr,
    _fetch_spo2,
    _fetch_sleep,
    _fetch_respiratory_rate,
    _fetch_hrv,
    # Calories and skin-temperature: no valid Google Health dataType id found
    # via probe (tried total-/active-/energy-expended, skin-/body-/core-temperature
    # and daily- variants — all INVALID_ARGUMENT). Temperature is covered by Oura.
]


async def _fetch_window(client: httpx.AsyncClient, token: str, start: date, end: date) -> dict[str, dict]:
    tz = _tz()
    s, e = start.isoformat(), (end + timedelta(days=1)).isoformat()  # inclusive end day
    by_day: dict[str, dict] = {}

    def day(d: str) -> dict:
        return by_day.setdefault(d, {})

    for fetch in _FETCHERS:
        try:
            await fetch(client, token, by_day, day, s, e, tz)
        except Exception:
            log.exception("google health fetcher %s failed", getattr(fetch, "__name__", fetch))
    by_day.pop("", None)
    return by_day


async def _sync_heart_rate(minutes: int = 180) -> dict[str, Any]:
    """Pull intraday heart-rate samples for the last `minutes`, downsample to
    1-minute buckets, and store as FitbitHeartRate (mirrors OuraHeartRate:
    {timestamp, bpm, source}). Fitbit records HR every few seconds, so raw
    storage would be ~24k rows/day — bucketing keeps it to 1440/day. Near
    real-time: samples lag ~5-15 min behind live (Fitbit cloud sync)."""
    conn = _get_connection()
    if not conn or not conn.get("connected"):
        return {"error": "Google Health is not connected.", "_status": 400}
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=minutes)
    buckets: dict[str, int] = {}
    # heart-rate has no server-side time filter, so we page newest-first and
    # early-break at the cutoff. Small windows stop after ~1 page; a multi-day
    # backfill needs a high cap (raised proportionally to the window).
    max_pages = 30 if minutes <= 240 else 300
    async with httpx.AsyncClient(timeout=120) as client:
        token = await _access_token(client)
        base = f"{API}/users/me/dataTypes/heart-rate/dataPoints"
        page_token: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"pageSize": 1000}
            if page_token:
                params["pageToken"] = page_token
            res = await client.get(base, params=params, headers={"Authorization": f"Bearer {token}"})
            if res.status_code >= 400:
                log.warning("google health heart-rate failed: %s %s", res.status_code, res.text[:200])
                break
            data = res.json()
            stop = False
            for p in data.get("dataPoints", []):
                v = p.get("heartRate") or {}
                st = (v.get("sampleTime") or {}).get("physicalTime")
                bpm = v.get("beatsPerMinute")
                if not st or bpm is None:
                    continue
                try:
                    dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if dt < cutoff:
                    stop = True  # newest-first — the rest are older too
                    continue
                minute = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
                key = minute.isoformat(timespec="seconds").replace("+00:00", "Z")
                # newest-first: keep the newest sample seen for each minute
                buckets.setdefault(key, int(float(bpm)))
            page_token = data.get("nextPageToken")
            if stop or not page_token:
                break

    cutoff_iso = cutoff.isoformat(timespec="seconds").replace("+00:00", "Z")
    existing = {
        r.get("timestamp")
        for r in db.query_entities(
            "FitbitHeartRate", {"owner_email": OWNER_EMAIL, "timestamp": {"$gte": cutoff_iso}}, "-timestamp", 5000
        )
    }
    to_create = [
        {"timestamp": k, "bpm": bpm, "source": "google_health", "owner_email": OWNER_EMAIL}
        for k, bpm in buckets.items()
        if k not in existing
    ]
    if to_create:
        db.bulk_create_entities("FitbitHeartRate", to_create)
    set_config_value("google_health_hr_last_sync", _iso_now())
    return {"success": True, "created": len(to_create), "buckets": len(buckets)}


async def handle(body: dict[str, Any]) -> dict[str, Any]:
    action = body.get("action")

    if action == "sync_hr":
        # up to 14 days for an intraday HR backfill; routine polls pass ~30 min
        return await _sync_heart_rate(min(int(body.get("minutes") or 180), 14 * 1440))

    if action == "get_client_id":
        return {"client_id": config_value("google_health_client_id"), "scopes": SCOPES}

    if action == "status":
        conn = _get_connection()
        latest = db.query_entities(
            "FitbitDaily", {"owner_email": OWNER_EMAIL, "source": "google_health"}, "-date", 1
        )
        return {
            "configured": bool(
                config_value("google_health_client_id") and config_value("google_health_client_secret")
            ),
            "connected": bool(conn and conn.get("connected")),
            "latest_day": latest[0].get("date") if latest else None,
            "last_sync": config_value("google_health_last_sync") or None,
        }

    if action == "disconnect":
        conn = _get_connection()
        if conn:
            db.update_entity(
                "GoogleHealthConnection", conn["id"],
                {"connected": False, "access_token": "", "refresh_token": ""},
            )
        return {"success": True}

    if action == "exchange_code":
        code = body.get("code")
        redirect_uri = body.get("redirect_uri") or f"{env('APP_PUBLIC_URL').rstrip('/')}/google-health-callback"
        if not code:
            return {"error": "Missing authorization code.", "_status": 400}
        if not config_value("google_health_client_id") or not config_value("google_health_client_secret"):
            return {"error": "Google Health client credentials are not configured (Settings page).", "_status": 500}
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": config_value("google_health_client_id"),
                    "client_secret": config_value("google_health_client_secret"),
                },
            )
        if res.status_code >= 400:
            return {"error": f"Token exchange failed ({res.status_code}): {res.text[:300]}", "_status": 502}
        tokens = res.json()
        if not tokens.get("refresh_token") and not (_get_connection() or {}).get("refresh_token"):
            # No refresh token and none stored → the authorize URL lacked
            # access_type=offline / prompt=consent, or consent was reused.
            log.warning("google health: no refresh_token in exchange response")
        _save_tokens(tokens, _get_connection())
        return {"success": True}

    if action == "probe":
        # Diagnostic: dump raw dataPoints for a dataType so we can finalise the
        # calories / respiratory-rate / skin-temp mappings against live data.
        data_type = body.get("data_type")
        if not data_type:
            return {"error": "probe requires data_type", "_status": 400}
        days = min(int(body.get("days") or 3), 30)
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=days)
        async with httpx.AsyncClient(timeout=60) as client:
            token = await _access_token(client)
            pts = await _list_points(client, token, data_type, start.isoformat(), (end + timedelta(days=1)).isoformat(), max_pages=1)
        return {"data_type": data_type, "count": len(pts), "sample": pts[:5]}

    if action == "sync":
        days = min(int(body.get("days") or 7), 365)
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=days)
        async with _sync_lock:
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    token = await _access_token(client)
                    by_day = await _fetch_window(client, token, start, end)
            except Exception as err:
                log.exception("google health sync failed")
                return {"error": f"Google Health sync failed: {err}", "_status": 502}

            existing = db.query_entities(
                "FitbitDaily", {"owner_email": OWNER_EMAIL, "source": "google_health"}
            )
            existing_by_date = {e.get("date"): e for e in existing}
            created = updated = 0
            for day_key, data in by_day.items():
                if not data:
                    continue
                record = {**data, "date": day_key, "source": "google_health", "owner_email": OWNER_EMAIL}
                if day_key in existing_by_date:
                    db.update_entity("FitbitDaily", existing_by_date[day_key]["id"], record)
                    updated += 1
                else:
                    db.create_entity("FitbitDaily", record)
                    created += 1
        set_config_value("google_health_last_sync", _iso_now())
        return {"success": True, "created": created, "updated": updated, "days_synced": len([d for d in by_day.values() if d])}

    return {"error": "Unknown action", "_status": 400}
