"""Programmed pump settings — the pump's own carb ratios, ISF, targets and basal.

Source: Glooko's `GET {base}/api/v3/devices_and_settings`, fetched by the
Glooko sync once a day (see glooko._sync_pump_settings). The response shape,
reduced to what is read here (values are illustrative):

    {
      "activePumpGuid": "<guid>",
      "deviceSettings": {"pumps": {"<guid>": {"<syncTimestamp>": {
          "generalSettings": {"activeInsulinTime": 3.0},           # hours
          "basalSettings": {"maxBasalRate": 3.0, "activeBasalProgram": "Weekday"},
          "bolusSettings": {"maxBolus": 10.0, "bolusCalculatorMinBg": 70},
          "profilesBolus": [{
              "isfSegments":                   {"data": [<segment>, ...]},
              "targetBgSegments":              {"data": [...]},
              "insulinToCarbRatioSegments":    {"data": [...]},
              "bgCorrectionThresholdSegments": {"data": [...]},
          }],
          "pumpProfilesBasal": [{"name": "Weekday", "data": [...]}],
          "graphData": ..., "additionalGraphData": ...,           # ignored
      }}}},
    }

    <segment> = {"segmentStart": 6.5, "duration": 5.5, "value": 10}   # hours

The snapshot key is Glooko's syncTimestamp, a genuine UTC instant (unlike the
pump-stream timestamps, which are local wall clock labelled Z).

Glooko records a snapshot only occasionally, so the newest one can be months
old even though the pump still runs it. Every snapshot is stored with its own
timestamp and shown with its age; nothing here presents a snapshot as live.
History is kept (deduped on device guid + snapshot time) so setting changes can
be compared over time. `/api/v2/pumps/settings` carries the same history in
snake_case; it adds nothing over v3 and is not fetched.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends

from . import db
from .auth import require_login
from .config import APP_TIMEZONE, OWNER_EMAIL

log = logging.getLogger("glucopilot.pump_settings")

router = APIRouter(dependencies=[Depends(require_login)])

ENTITY_TYPE = "PumpSettings"
MAX_SNAPSHOTS = 500
_IGNORED_KEYS = {"graphData", "additionalGraphData"}

# Segment schedules: stored field, Glooko key in profilesBolus[0], label, unit.
SCHEDULES = (
    ("carb_ratio_segments", "insulinToCarbRatioSegments", "Carb ratio", "g/U"),
    ("isf_segments", "isfSegments", "Correction factor (ISF)", "mg/dL/U"),
    ("target_segments", "targetBgSegments", "Target glucose", "mg/dL"),
    ("correction_threshold_segments", "bgCorrectionThresholdSegments", "Correct above", "mg/dL"),
)
_BG_SCHEDULES = {"isf_segments", "target_segments", "correction_threshold_segments"}
SCALARS = (
    ("active_insulin_time_hours", "Active insulin time", "h"),
    ("max_bolus_units", "Max bolus", "U"),
    ("max_basal_rate_units_per_hour", "Max basal rate", "U/h"),
    ("bolus_calculator_min_bg", "Bolus calculator minimum glucose", "mg/dL"),
)
_COMPARED_FIELDS = (
    *(field for field, _key, _label, _unit in SCHEDULES),
    *(field for field, _label, _unit in SCALARS),
    "active_basal_program",
    "basal_programs",
)
_FIELD_LABELS = {
    **{field: label for field, _key, label, _unit in SCHEDULES},
    **{field: label for field, label, _unit in SCALARS},
    "active_basal_program": "Active basal program",
    "basal_programs": "Basal programs",
}


# ── parsing ──────────────────────────────────────────────────────────────


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN guard


def _first(record: dict, *keys: str) -> Any:
    for key in keys:
        if record.get(key) is not None:
            return record[key]
    return None


def _parse_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clock(hours: float) -> str:
    minutes = int(round(hours * 60))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _segment_value(raw: Any, *, bg: bool) -> float | None:
    if isinstance(raw, dict):
        raw = _first(raw, "value", "target", "rate", "amount")
    value = _num(raw)
    if value is None:
        return None
    if bg and value > 1000:  # Glooko scales some glucose values by 100
        value = value / 100
    return round(value, 3)


def _segments(raw: Any, *, bg: bool = False) -> list[dict[str, Any]]:
    """[{segmentStart h, duration h, value}] → sorted HH:MM segments."""
    if isinstance(raw, dict):
        raw = _first(raw, "data", "segments")
    if not isinstance(raw, list):
        return []
    segments = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        start = _num(_first(item, "segmentStart", "start", "startTime"))
        duration = _num(item.get("duration"))
        value = _segment_value(_first(item, "value", "rate", "amount"), bg=bg)
        if start is None or value is None or not 0 <= start < 24:
            continue
        segments.append({"start_hours": start, "duration_hours": duration, "value": value})
    segments.sort(key=lambda segment: segment["start_hours"])
    for index, segment in enumerate(segments):
        if segment["duration_hours"] is None or segment["duration_hours"] <= 0:
            following = segments[index + 1]["start_hours"] if index + 1 < len(segments) else 24.0
            segment["duration_hours"] = following - segment["start_hours"]
        end = min(segment["start_hours"] + segment["duration_hours"], 24.0)
        segment["start"] = _clock(segment["start_hours"])
        segment["end"] = _clock(end)
    return segments


def _program_name(value: Any) -> str | None:
    if isinstance(value, dict):
        value = _first(value, "name", "profileName", "programName")
    if value is None or isinstance(value, bool):
        return None
    return str(value).strip() or None


def _basal_programs(raw: Any, active: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        if any(key in raw for key in ("data", "segments")):
            entries = [raw]
        else:
            entries = [
                {"name": key, **value} if isinstance(value, dict) else {"name": key, "data": value}
                for key, value in raw.items()
            ]
    elif isinstance(raw, list):
        entries = raw
    else:
        entries = []
    active_name = _program_name(active)
    active_index = int(active) if isinstance(active, int) and not isinstance(active, bool) else None
    programs = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        segments = _segments(_first(entry, "data", "segments", "basalSegments"))
        if not segments:
            continue
        name = _program_name(entry) or f"Program {index + 1}"
        programs.append({
            "name": name,
            "active": (active_name is not None and name == active_name) or active_index == index,
            "segments": segments,
            "units_per_day": round(sum(s["value"] * s["duration_hours"] for s in segments), 2),
        })
    if len(programs) == 1 and active is None:
        programs[0]["active"] = True
    return programs


def _settings_hash(row: dict[str, Any]) -> str:
    compared = {field: row.get(field) for field in _COMPARED_FIELDS}
    encoded = json.dumps(compared, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def clean_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in snapshot.items() if key not in _IGNORED_KEYS}


def map_snapshot(
    snapshot: dict[str, Any],
    *,
    device_guid: str,
    snapshot_key: str,
) -> dict[str, Any] | None:
    """One Glooko snapshot → a PumpSettings row, or None if it holds nothing usable."""
    if not isinstance(snapshot, dict):
        return None
    taken = _parse_utc(snapshot_key) or _parse_utc(_first(snapshot, "syncTimestamp", "updatedAt"))
    if taken is None:
        return None
    general = snapshot.get("generalSettings") if isinstance(snapshot.get("generalSettings"), dict) else {}
    basal = snapshot.get("basalSettings") if isinstance(snapshot.get("basalSettings"), dict) else {}
    bolus = snapshot.get("bolusSettings") if isinstance(snapshot.get("bolusSettings"), dict) else {}
    profiles = snapshot.get("profilesBolus")
    profile = profiles[0] if isinstance(profiles, list) and profiles and isinstance(profiles[0], dict) else {}

    active_program = basal.get("activeBasalProgram")
    row: dict[str, Any] = {
        field: _segments(profile.get(key), bg=field in _BG_SCHEDULES)
        for field, key, _label, _unit in SCHEDULES
    }
    row.update({
        "active_insulin_time_hours": _num(general.get("activeInsulinTime")),
        "max_bolus_units": _num(bolus.get("maxBolus")),
        "max_basal_rate_units_per_hour": _num(basal.get("maxBasalRate")),
        "bolus_calculator_min_bg": _num(bolus.get("bolusCalculatorMinBg")),
        "basal_programs": _basal_programs(snapshot.get("pumpProfilesBasal"), active_program),
    })
    active = next((program for program in row["basal_programs"] if program["active"]), None)
    row["active_basal_program"] = active["name"] if active else _program_name(active_program)
    if not any(row[field] for field, *_rest in SCHEDULES) and not row["basal_programs"]:
        return None

    snapshot_at = _iso(taken)
    model = _first(snapshot, "deviceModel", "pumpModel", "model", "deviceType")
    row.update({
        "owner_email": OWNER_EMAIL,
        "source": "glooko",
        "device_guid": device_guid,
        "device_model": str(model) if model else None,
        "snapshot_key": snapshot_key,
        "snapshot_at": snapshot_at,
        "dedupe_key": f"{device_guid}:{snapshot_at}",
        "is_latest": False,
    })
    row["settings_hash"] = _settings_hash(row)
    row["title"] = f"Programmed pump settings recorded {snapshot_at[:10]}"
    row["summary"] = _summary(row)
    row["details"] = _basal_details(row)
    return row


def snapshots_from_payload(payload: Any) -> tuple[str | None, list[tuple[str, dict[str, Any]]]]:
    """(active pump guid, [(snapshot key, snapshot), ...]) for the active pump only."""
    if not isinstance(payload, dict):
        return None, []
    settings = payload.get("deviceSettings") if isinstance(payload.get("deviceSettings"), dict) else {}
    pumps = settings.get("pumps") if isinstance(settings.get("pumps"), dict) else {}
    guid = payload.get("activePumpGuid") or settings.get("activePumpGuid")
    if not guid and len(pumps) == 1:
        guid = next(iter(pumps))
    snapshots = pumps.get(guid) if guid else None
    if not isinstance(snapshots, dict):
        return (str(guid) if guid else None), []
    return str(guid), [
        (str(key), clean_snapshot(value))
        for key, value in snapshots.items()
        if isinstance(value, dict)
    ]


# ── text for the Companion evidence item ─────────────────────────────────


def _fmt(value: Any) -> str:
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


def _segment_text(segments: list[dict[str, Any]]) -> str:
    return "; ".join(f"{s['start']}-{s['end']} {_fmt(s['value'])}" for s in segments)


def _summary(row: dict[str, Any]) -> str:
    parts = [
        f"Pump settings as programmed in a Glooko snapshot recorded {row['snapshot_at'][:10]}"
        " (a past snapshot, not a live reading)."
    ]
    for field, _key, label, unit in SCHEDULES:
        if row.get(field):
            parts.append(f"{label} {unit}: {_segment_text(row[field])}.")
    for field, label, unit in SCALARS:
        if row.get(field) is not None:
            parts.append(f"{label} {_fmt(row[field])} {unit}.")
    return " ".join(parts)


def _basal_details(row: dict[str, Any]) -> str | None:
    texts = [
        f"Basal program \"{program['name']}\"{' (active)' if program['active'] else ''} U/h: "
        f"{_segment_text(program['segments'])} ({_fmt(program['units_per_day'])} U/day)."
        for program in sorted(row.get("basal_programs") or [], key=lambda p: not p["active"])
    ]
    return " ".join(texts) or None


# ── persistence ──────────────────────────────────────────────────────────


def _rows() -> list[dict[str, Any]]:
    return db.query_entities(ENTITY_TYPE, {"owner_email": OWNER_EMAIL}, "snapshot_at", MAX_SNAPSHOTS * 2)


def persist(device_guid: str | None, mapped: list[dict[str, Any]]) -> dict[str, int]:
    """Store unseen snapshots and move the is_latest mark to the active pump's
    newest one. Stored snapshots are never rewritten: they are history."""
    rows = _rows()
    seen = {row.get("dedupe_key") for row in rows}
    created = skipped = 0
    for row in sorted(mapped, key=lambda item: item["snapshot_at"])[-MAX_SNAPSHOTS:]:
        if row["dedupe_key"] in seen:
            skipped += 1
            continue
        rows.append(db.create_entity(ENTITY_TYPE, row))
        seen.add(row["dedupe_key"])
        created += 1
    candidates = [row for row in rows if device_guid and row.get("device_guid") == device_guid] or rows
    latest = max(candidates, key=lambda row: row.get("snapshot_at") or "", default=None)
    for row in rows:
        should = latest is not None and row["id"] == latest["id"]
        if bool(row.get("is_latest")) != should:
            db.update_entity(ENTITY_TYPE, row["id"], {"is_latest": should})
    return {"created": created, "skipped": skipped}


# ── read side ────────────────────────────────────────────────────────────


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(db.config_value("app_timezone", APP_TIMEZONE))
    except Exception:
        return ZoneInfo(APP_TIMEZONE)


def _age_days(snapshot_at: str, today: date) -> int | None:
    taken = _parse_utc(snapshot_at)
    return (today - taken.astimezone(_tz()).date()).days if taken else None


def _changes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Snapshots whose settings differ from the one before, newest first."""
    changes = []
    previous = None
    for row in rows:
        if previous is not None and row.get("settings_hash") != previous.get("settings_hash"):
            changed = [
                _FIELD_LABELS[field] for field in _COMPARED_FIELDS
                if row.get(field) != previous.get(field)
            ]
            changes.append({
                "snapshot_at": row.get("snapshot_at"),
                "previous_snapshot_at": previous.get("snapshot_at"),
                "changed": changed,
            })
        previous = row
    return list(reversed(changes))


def _public(row: dict[str, Any], today: date) -> dict[str, Any]:
    return {
        "snapshot_at": row.get("snapshot_at"),
        "as_of": str(row.get("snapshot_at") or "")[:10],
        "age_days": _age_days(row.get("snapshot_at"), today),
        "device_model": row.get("device_model"),
        "source": row.get("source"),
        "schedules": [
            {"key": field, "label": label, "unit": unit, "segments": row.get(field) or []}
            for field, _key, label, unit in SCHEDULES
            if row.get(field)
        ],
        "scalars": [
            {"key": field, "label": label, "unit": unit, "value": row.get(field)}
            for field, label, unit in SCALARS
            if row.get(field) is not None
        ],
        "basal_programs": row.get("basal_programs") or [],
        "active_basal_program": row.get("active_basal_program"),
    }


def view(today: date | None = None) -> dict[str, Any]:
    today = today or datetime.now(_tz()).date()
    rows = _rows()
    latest = next((row for row in rows if row.get("is_latest")), None)
    if latest is None and rows:
        latest = rows[-1]
    device_rows = [row for row in rows if latest and row.get("device_guid") == latest.get("device_guid")]
    return {
        "available": latest is not None,
        "latest": _public(latest, today) if latest else None,
        "snapshot_count": len(device_rows),
        "first_snapshot_at": device_rows[0].get("snapshot_at") if device_rows else None,
        "changes": _changes(device_rows)[:20],
        "last_fetched_at": db.get_setting("glooko_pump_settings_fetched_at") or None,
    }


def report_block() -> dict[str, Any] | None:
    """Latest programmed settings for the Visit Report, with their as-of date."""
    current = view()
    if not current["available"]:
        return None
    return {**current["latest"], "snapshot_count": current["snapshot_count"], "changes": current["changes"][:5]}


@router.get("/api/pump-settings")
def get_pump_settings():
    return view()
