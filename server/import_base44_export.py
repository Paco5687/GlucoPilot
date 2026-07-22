"""Import Base44 entity-export CSVs into the entity store, deduplicating
against everything already present.

Usage:
    python -m server.import_base44_export /legacy/GlucoseReading_export.csv /legacy/Treatment_export.csv
    python -m server.import_base44_export /legacy/OuraDaily_export.csv /legacy/OuraHeartRate_export.csv

The entity type is inferred from the filename. Dedup rules (measured against
real data):
  - GlucoseReading: skip if any kept reading lies within ±240 s — CGM readings
    are 5 min apart, and cross-source clock offset between the pump CSV and
    Nightscout is under a minute, so this drops true duplicates only.
  - Treatment: skip if a kept treatment of the same type lies within ±90 s.
  - OuraDaily: skip if the date already exists.
  - OuraHeartRate: skip if any kept sample lies within ±60 s (5-min buckets).
Idempotent: re-running skips everything it already imported.
"""

import bisect
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .config import OWNER_EMAIL
from .readings import persist_readings_deduped

READING_TOLERANCE = 240  # seconds
TREATMENT_TOLERANCE = 90
HEARTRATE_TOLERANCE = 60

# Base44 export bookkeeping columns that must not become entity data.
META_COLUMNS = {"id", "created_date", "updated_date", "created_by", "created_by_id", "is_sample", "owner_email"}

READING_FIELDS = ("source", "value", "trend", "timestamp")
TREATMENT_FLOAT_FIELDS = ("amount", "glucose", "percent", "duration", "absolute")
TREATMENT_STR_FIELDS = ("source", "type", "event_type", "insulin_type", "glucose_type", "notes", "ns_id", "preBolus", "timestamp")


def _epoch(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _iso(ts: str) -> str:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ToleranceIndex:
    """Sorted timestamp index answering 'is anything kept within N seconds?'."""

    def __init__(self, initial: list[float]):
        self.items = sorted(initial)

    def near(self, t: float, tolerance: float) -> bool:
        i = bisect.bisect_left(self.items, t)
        for j in (i - 1, i):
            if 0 <= j < len(self.items) and abs(self.items[j] - t) <= tolerance:
                return True
        return False

    def add(self, t: float) -> None:
        bisect.insort(self.items, t)


def import_readings(path: Path) -> tuple[int, int]:
    with open(path, newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("timestamp") and r.get("value") and r.get("is_sample") != "true"]
    mapped = [
        {
            "value": round(float(row["value"])),
            "timestamp": _iso(row["timestamp"]),
            "trend": row.get("trend") or "Unknown",
            "source": row.get("source") or "csv",
            "owner_email": OWNER_EMAIL,
        }
        for row in rows
    ]
    return persist_readings_deduped(mapped, READING_TOLERANCE)


def import_treatments(path: Path) -> tuple[int, int]:
    existing = db.query_entities("Treatment", {"owner_email": OWNER_EMAIL}, "timestamp", 1000000)
    indexes: dict[str, ToleranceIndex] = {}
    for r in existing:
        if r.get("timestamp"):
            indexes.setdefault(r.get("type") or "other", ToleranceIndex([])).add(_epoch(r["timestamp"]))

    with open(path, newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("timestamp") and r.get("is_sample") != "true"]
    rows.sort(key=lambda r: r["timestamp"])

    to_create, skipped = [], 0
    for r in rows:
        ttype = r.get("type") or "other"
        t = _epoch(r["timestamp"])
        index = indexes.setdefault(ttype, ToleranceIndex([]))
        if index.near(t, TREATMENT_TOLERANCE):
            skipped += 1
            continue
        index.add(t)
        record = {"owner_email": OWNER_EMAIL}
        for field in TREATMENT_STR_FIELDS:
            value = (r.get(field) or "").strip()
            if value:
                record[field] = value
        for field in TREATMENT_FLOAT_FIELDS:
            value = (r.get(field) or "").strip()
            if value:
                try:
                    record[field] = float(value)
                except ValueError:
                    pass
        record["timestamp"] = _iso(r["timestamp"])
        record.setdefault("type", "other")
        record.setdefault("source", "csv")
        to_create.append(record)
    for i in range(0, len(to_create), 1000):
        db.bulk_create_entities("Treatment", to_create[i : i + 1000])
    return len(to_create), skipped


def _clean_row(r: dict) -> dict:
    """Drop export meta columns and empty values; coerce numerics."""
    record = {}
    for key, raw in r.items():
        if key in META_COLUMNS:
            continue
        value = (raw or "").strip()
        if value == "":
            continue
        if value in ("true", "false"):
            record[key] = value == "true"
            continue
        try:
            n = float(value)
            record[key] = int(n) if n.is_integer() else n
        except ValueError:
            record[key] = value
    record["owner_email"] = OWNER_EMAIL
    return record


def import_oura_daily(path: Path) -> tuple[int, int]:
    existing_dates = {
        r.get("date") for r in db.query_entities("OuraDaily", {"owner_email": OWNER_EMAIL}, "date", 1000000)
    }
    with open(path, newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("date") and r.get("is_sample") != "true"]
    to_create, skipped = [], 0
    seen = set(existing_dates)
    for r in rows:
        if r["date"] in seen:
            skipped += 1
            continue
        seen.add(r["date"])
        record = _clean_row(r)
        record["date"] = r["date"]  # keep as string even if it parsed numeric
        to_create.append(record)
    for i in range(0, len(to_create), 1000):
        db.bulk_create_entities("OuraDaily", to_create[i : i + 1000])
    return len(to_create), skipped


def import_oura_heartrate(path: Path) -> tuple[int, int]:
    existing = db.query_entities("OuraHeartRate", {"owner_email": OWNER_EMAIL}, "timestamp", 1000000)
    index = ToleranceIndex([_epoch(r["timestamp"]) for r in existing if r.get("timestamp")])
    with open(path, newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("timestamp") and r.get("bpm") and r.get("is_sample") != "true"]
    rows.sort(key=lambda r: r["timestamp"])
    to_create, skipped = [], 0
    for r in rows:
        t = _epoch(r["timestamp"])
        if index.near(t, HEARTRATE_TOLERANCE):
            skipped += 1
            continue
        index.add(t)
        record = _clean_row(r)
        record["timestamp"] = _iso(r["timestamp"])
        record.setdefault("source", "oura")
        to_create.append(record)
    for i in range(0, len(to_create), 1000):
        db.bulk_create_entities("OuraHeartRate", to_create[i : i + 1000])
    return len(to_create), skipped


def main() -> None:
    args = [Path(a) for a in sys.argv[1:]]
    if not args:
        sys.exit(
            "Usage: python -m server.import_base44_export <export.csv> [...]\n"
            "Supported: GlucoseReading, Treatment, OuraDaily, OuraHeartRate exports (matched by filename)."
        )
    db.init_db()
    for path in args:
        if not path.is_file():
            sys.exit(f"Not found: {path}")
        name = path.name.lower()
        if "heartrate" in name or "heart_rate" in name:
            created, skipped = import_oura_heartrate(path)
            label = "HR samples"
        elif "ouradaily" in name or "oura_daily" in name:
            created, skipped = import_oura_daily(path)
            label = "Oura daily records"
        elif "glucose" in name or "reading" in name:
            created, skipped = import_readings(path)
            label = "readings"
        elif "treatment" in name:
            created, skipped = import_treatments(path)
            label = "treatments"
        else:
            sys.exit(f"Cannot infer entity type from filename: {path.name}")
        print(f"{path.name}: imported {created} {label}, skipped {skipped} duplicates")


if __name__ == "__main__":
    main()
