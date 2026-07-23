"""Fingerstick readings and time-bounded CGM comparison capture.

The sources remain separate observations. A fixed CGM snapshot is attached to a
new fingerstick when one is available nearby; it never replaces the CGM trace.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import OWNER_EMAIL
from .glucose_reconciliation import (
    ReconciliationInputError,
    capture_context,
    pair_fields,
    summarize,
)
from .repositories import get_repositories

log = logging.getLogger("glucopilot.fingerstick")

MATCH_WINDOW_MIN = 15  # match a fingerstick to the nearest CGM reading within ±15 min


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _nearest_cgm(ts_iso: str) -> dict[str, Any] | None:
    ts = _parse(ts_iso)
    if ts is None:
        return None
    lo = (ts - timedelta(minutes=MATCH_WINDOW_MIN)).isoformat(timespec="seconds").replace("+00:00", "Z")
    hi = (ts + timedelta(minutes=MATCH_WINDOW_MIN)).isoformat(timespec="seconds").replace("+00:00", "Z")
    rows = get_repositories().glucose.query(
        {"owner_email": OWNER_EMAIL, "timestamp": {"$gte": lo, "$lte": hi}},
        "timestamp",
        500,
    )
    best, best_diff = None, None
    for r in rows:
        rt = _parse(r.get("timestamp"))
        if rt is None or r.get("value") is None:
            continue
        diff = abs((rt - ts).total_seconds())
        if best_diff is None or diff < best_diff:
            best, best_diff = r, diff
    if best is None:
        return None
    return {
        "value": float(best["value"]),
        "timestamp": best.get("timestamp"),
        "source": best.get("source"),
        "id": best.get("id"),
        "trend": best.get("trend"),
    }


async def handle(body: dict[str, Any]) -> dict[str, Any]:
    action = body.get("action", "list")

    if action == "add":
        try:
            value = float(body["value"])
        except (KeyError, TypeError, ValueError):
            return {"error": "A numeric fingerstick value is required.", "_status": 400}
        if not (10 <= value <= 800):
            return {"error": "Fingerstick value out of range (10–800 mg/dL).", "_status": 400}
        ts_iso = body.get("timestamp") or _now_iso()
        if _parse(ts_iso) is None:
            return {"error": "A valid fingerstick timestamp is required.", "_status": 400}
        try:
            context = capture_context(body)
        except ReconciliationInputError as error:
            return {"error": str(error), "_status": 400}
        pair = pair_fields(value, ts_iso, _nearest_cgm(ts_iso))
        rec = get_repositories().fingersticks.create(
            {
                "timestamp": ts_iso,
                "value": value,
                **pair,
                **context,
                "note": (body.get("note") or "").strip(),
                "source": "manual",
                "owner_email": OWNER_EMAIL,
            }
        )
        return {"ok": True, "reading": rec}

    if action == "delete":
        rid = body.get("id")
        repository = get_repositories().fingersticks
        row = repository.get(rid) if rid else None
        if row and row.get("owner_email") == OWNER_EMAIL:
            repository.delete(rid)
        return {"ok": True}

    if action == "list":
        days = min(int(body.get("days") or 30), 365)
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds").replace("+00:00", "Z")
        rows = get_repositories().fingersticks.query(
            {"owner_email": OWNER_EMAIL, "timestamp": {"$gte": since}}, "-timestamp", 3000
        )
        return {"readings": rows}

    if action == "stats":
        rows = get_repositories().fingersticks.query(
            {"owner_email": OWNER_EMAIL}, "-timestamp", 5000
        )
        result = summarize(rows)
        paired = [
            row
            for row in rows
            if row.get("value") is not None and row.get("cgm_value") is not None
        ]
        if paired:
            worst = max(
                paired,
                key=lambda row: abs(float(row["cgm_value"]) - float(row["value"])),
            )
            worst_delta = round(float(worst["cgm_value"]) - float(worst["value"]), 1)
            result["worst"] = {
                "timestamp": worst.get("timestamp"),
                "fingerstick": worst.get("value"),
                "cgm": worst.get("cgm_value"),
                "delta": worst_delta,
            }
            # Compatibility label for existing clients; persistent_bias is the
            # sample-size-aware field for new consumers.
            mean_delta = float(result["mean_delta"])
            result["bias"] = (
                "cgm_high" if mean_delta > 3 else "cgm_low" if mean_delta < -3 else "balanced"
            )
        return result

    return {"error": "Unknown action", "_status": 400}
