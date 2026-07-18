"""One-time cleanup of cross-source duplicate glucose readings.

Before global insert-time dedup existed, the same physiological reading could
be stored by multiple sources (pump CSV, Nightscout, official Dexcom API) at
slightly different timestamps. This collapses every ±240 s cluster down to one
reading, keeping the highest-fidelity source.

Usage:
    python -m server.dedup_readings            # dry run: report only
    python -m server.dedup_readings --apply    # actually delete duplicates

Priority (kept first): dexcom_share > dexcom > nightscout > csv > glooko.
"""

import sys
from collections import Counter
from datetime import datetime

from . import db
from .config import OWNER_EMAIL

PRIORITY = {"dexcom_share": 0, "dexcom": 1, "nightscout": 2, "csv": 3, "glooko": 4}
TOLERANCE = 240  # seconds


def find_duplicates() -> tuple[int, list[str], Counter]:
    rows = db.query_entities("GlucoseReading", {"owner_email": OWNER_EMAIL}, "timestamp", 1000000)
    parsed = []
    for r in rows:
        try:
            epoch = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            continue
        parsed.append((epoch, r["id"], r.get("source") or "unknown"))
    parsed.sort()

    to_delete: list[str] = []
    removed_by_source: Counter = Counter()

    def flush(cluster: list) -> None:
        if len(cluster) <= 1:
            return
        keep = min(cluster, key=lambda x: (PRIORITY.get(x[2], 9), x[0]))
        for item in cluster:
            if item[1] != keep[1]:
                to_delete.append(item[1])
                removed_by_source[item[2]] += 1

    cluster = [parsed[0]] if parsed else []
    for item in parsed[1:]:
        if item[0] - cluster[-1][0] <= TOLERANCE:
            cluster.append(item)
        else:
            flush(cluster)
            cluster = [item]
    flush(cluster)
    return len(parsed), to_delete, removed_by_source


def main() -> None:
    apply = "--apply" in sys.argv
    db.init_db()
    total, to_delete, by_source = find_duplicates()
    print(f"Total readings: {total}")
    print(f"Duplicate readings in ±{TOLERANCE}s clusters: {len(to_delete)}")
    print(f"Would remove by source: {dict(by_source)}")
    if not apply:
        print("Dry run only. Re-run with --apply to delete.")
        return
    for rid in to_delete:
        db.delete_entity("GlucoseReading", rid)
    print(f"Deleted {len(to_delete)} duplicates. Remaining: {total - len(to_delete)}")


if __name__ == "__main__":
    main()
