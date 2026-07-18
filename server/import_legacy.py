"""One-time importer: legacy data.json (CSV-derived export) → entity store.

Usage:
    python -m server.import_legacy [path/to/data.json]

Idempotent: deletes all source="csv" GlucoseReading/Treatment rows first, then
re-imports. Legacy timestamps are naive local times; they are converted to UTC
using APP_TIMEZONE.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db
from .config import APP_TIMEZONE, OWNER_EMAIL, REPO_DIR

DEFAULT_PATH = REPO_DIR / "legacy" / "app" / "static" / "data.json"
BATCH = 1000


def _to_utc_iso(naive: str, tz: ZoneInfo) -> str | None:
    try:
        dt = datetime.fromisoformat(naive)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def run(path: Path) -> None:
    tz = ZoneInfo(APP_TIMEZONE)
    data = json.loads(path.read_text())
    db.init_db()

    deleted_r = db.delete_entities_where("GlucoseReading", {"source": "csv", "owner_email": OWNER_EMAIL})
    deleted_t = db.delete_entities_where("Treatment", {"source": "csv", "owner_email": OWNER_EMAIL})
    print(f"Cleared previous csv import: {deleted_r} readings, {deleted_t} treatments")

    readings = []
    for row in data.get("timeline", []):
        bg = row.get("bg")
        ts = _to_utc_iso(row.get("t"), tz)
        if bg is None or ts is None:
            continue
        readings.append(
            {
                "value": round(_num(bg)),
                "timestamp": ts,
                "trend": "Unknown",
                "source": "csv",
                "owner_email": OWNER_EMAIL,
            }
        )

    treatments = []

    def add(ttype: str, ts: str | None, **fields) -> None:
        if ts is None:
            return
        treatments.append(
            {"type": ttype, "timestamp": ts, "source": "csv", "owner_email": OWNER_EMAIL, **fields}
        )

    for e in data.get("bolusEvents", []):
        ts = _to_utc_iso(e.get("t"), tz)
        amount = _num(e.get("amount"))
        if amount <= 0:
            continue
        details = str(e.get("details") or "")
        add(
            "insulin",
            ts,
            event_type=e.get("description") or "Bolus",
            amount=amount,
            insulin_type="rapid",
            notes=details or None,
        )
        carbs_match = re.search(r"carbs\s+([0-9.]+)", details, re.IGNORECASE)
        carbs = _num(carbs_match.group(1)) if carbs_match else 0.0
        if carbs > 0:
            add("carb", ts, event_type="Carbs", amount=carbs)

    for e in data.get("basalEvents", []):
        ts = _to_utc_iso(e.get("t"), tz)
        desc = str(e.get("description") or "")
        details = str(e.get("details") or "")
        duration_match = re.search(r"Duration\s+([0-9.]+)", details, re.IGNORECASE)
        duration = _num(duration_match.group(1)) if duration_match else 0.0
        if re.search(r"suspend", desc, re.IGNORECASE):
            add("suspension", ts, event_type=desc, duration=duration or None, notes=details or None)
        elif re.search(r"temporary", desc, re.IGNORECASE):
            add(
                "tempbasal",
                ts,
                event_type=desc,
                absolute=_num(e.get("amount")),
                duration=duration or None,
                notes=details or None,
            )
        # Scheduled basal-rate changes are not treatments; skipped.

    for e in data.get("alarms", []):
        ts = _to_utc_iso(e.get("t"), tz)
        add("note", ts, event_type=e.get("description") or "Alarm", notes=str(e.get("details") or "") or None)

    for e in data.get("manualBg", []):
        ts = _to_utc_iso(e.get("t"), tz)
        glucose = _num(e.get("amount"))
        if glucose > 0:
            add("bg", ts, event_type="BG Check", glucose=glucose, glucose_type="Finger")

    treatments = [{k: v for k, v in t.items() if v is not None} for t in treatments]

    for i in range(0, len(readings), BATCH):
        db.bulk_create_entities("GlucoseReading", readings[i : i + BATCH])
    for i in range(0, len(treatments), BATCH):
        db.bulk_create_entities("Treatment", treatments[i : i + BATCH])

    print(f"Imported {len(readings)} glucose readings and {len(treatments)} treatments from {path}")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH
    if not target.is_file():
        sys.exit(f"data.json not found at {target}")
    run(target)
