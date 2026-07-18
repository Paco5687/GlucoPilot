"""Shared glucose-reading persistence with global cross-source dedup.

One physiological reading can arrive from several sources at slightly
different timestamps: Dexcom Share sees it first (real-time), Nightscout
relays the same Share feed seconds later, and the official Dexcom API delivers
it again an hour later. CGM readings are 5 minutes apart, so anything within
±240 s of an existing reading — regardless of source — is the same reading.
"""

import bisect
from datetime import datetime
from typing import Any

from . import db
from .config import OWNER_EMAIL

READING_TOLERANCE = 240  # seconds


def _epoch(ts: str) -> float | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def persist_readings_deduped(mapped: list[dict[str, Any]], tolerance: int = READING_TOLERANCE) -> tuple[int, int]:
    """Insert readings unless one from ANY source exists within the tolerance.
    Returns (created, skipped)."""
    existing = db.query_entities("GlucoseReading", {"owner_email": OWNER_EMAIL}, "timestamp", 1000000)
    stamps = sorted(e for e in (_epoch(r.get("timestamp", "")) for r in existing) if e is not None)

    created = skipped = 0
    for m in sorted(mapped, key=lambda x: x["timestamp"]):
        epoch = _epoch(m["timestamp"])
        if epoch is None:
            skipped += 1
            continue
        i = bisect.bisect_left(stamps, epoch)
        if any(0 <= j < len(stamps) and abs(stamps[j] - epoch) <= tolerance for j in (i - 1, i)):
            skipped += 1
            continue
        db.create_entity("GlucoseReading", m)
        bisect.insort(stamps, epoch)
        created += 1
    return created, skipped
