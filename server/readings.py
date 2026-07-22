"""Shared glucose-reading persistence with global cross-source dedup.

One physiological reading can arrive from several sources at slightly
different timestamps: Dexcom Share sees it first (real-time), Nightscout
relays the same Share feed seconds later, and the official Dexcom API delivers
it again an hour later. CGM readings are 5 minutes apart, so anything within
±240 s of an existing reading — regardless of source — is the same reading.
"""

from typing import Any

from .repositories import get_repositories
from .typed_glucose import READING_TOLERANCE_SECONDS

READING_TOLERANCE = READING_TOLERANCE_SECONDS


def persist_readings_deduped(mapped: list[dict[str, Any]], tolerance: int = READING_TOLERANCE) -> tuple[int, int]:
    """Insert readings unless one from ANY source exists within the tolerance.
    Returns (created, skipped)."""
    created, skipped = get_repositories().glucose.create_deduplicated(
        mapped,
        tolerance_seconds=tolerance,
    )
    return len(created), skipped
