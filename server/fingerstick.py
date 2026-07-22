"""Fingerstick (BGM) readings + CGM-vs-fingerstick discrepancy tracking.

A finger-prick is the ground-truth glucose at a moment; the Dexcom reads
interstitial fluid and can differ substantially (lag, compression, calibration) —
Emily has seen gaps up to ~60 mg/dL. We store each fingerstick, match it to the
CGM value at that time, and record the delta so the discrepancy itself becomes a
measurable signal. On the chart these show as manual 'correction points' — they
do NOT replace or override the CGM trace.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db
from .config import OWNER_EMAIL

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


def _nearest_cgm(ts_iso: str) -> tuple[float | None, str | None, str | None]:
    ts = _parse(ts_iso)
    if ts is None:
        return None, None, None
    lo = (ts - timedelta(minutes=MATCH_WINDOW_MIN)).isoformat(timespec="seconds").replace("+00:00", "Z")
    hi = (ts + timedelta(minutes=MATCH_WINDOW_MIN)).isoformat(timespec="seconds").replace("+00:00", "Z")
    rows = db.query_entities(
        "GlucoseReading", {"owner_email": OWNER_EMAIL, "timestamp": {"$gte": lo, "$lte": hi}}, "timestamp", 500
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
        return None, None, None
    return float(best["value"]), best.get("timestamp"), best.get("source")


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
        cgm, cgm_ts, cgm_source = _nearest_cgm(ts_iso)
        delta = round(cgm - value, 1) if cgm is not None else None
        rec = db.create_entity("FingerstickReading", {
            "timestamp": ts_iso,
            "value": value,
            "cgm_value": cgm,
            "cgm_timestamp": cgm_ts,
            "cgm_source": cgm_source,
            "delta": delta,          # CGM minus fingerstick: + = CGM reads high
            "note": (body.get("note") or "").strip(),
            "source": "manual",
            "owner_email": OWNER_EMAIL,
        })
        return {"ok": True, "reading": rec}

    if action == "delete":
        rid = body.get("id")
        rows = db.query_entities("FingerstickReading", {"id": rid, "owner_email": OWNER_EMAIL}, limit=1)
        if rows:
            db.delete_entity("FingerstickReading", rid)
        return {"ok": True}

    if action == "list":
        days = min(int(body.get("days") or 30), 365)
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds").replace("+00:00", "Z")
        rows = db.query_entities(
            "FingerstickReading", {"owner_email": OWNER_EMAIL, "timestamp": {"$gte": since}}, "-timestamp", 3000
        )
        return {"readings": rows}

    if action == "stats":
        rows = db.query_entities("FingerstickReading", {"owner_email": OWNER_EMAIL}, "-timestamp", 5000)
        paired = [r for r in rows if r.get("delta") is not None]
        if not paired:
            return {"count": len(rows), "paired": 0}
        deltas = [r["delta"] for r in paired]
        abs_deltas = [abs(d) for d in deltas]
        mean_delta = sum(deltas) / len(deltas)
        worst = max(paired, key=lambda r: abs(r["delta"]))
        return {
            "count": len(rows),
            "paired": len(paired),
            "mean_delta": round(mean_delta, 1),
            "mean_abs_delta": round(sum(abs_deltas) / len(abs_deltas), 1),
            "max_abs_delta": max(abs_deltas),
            "worst": {"timestamp": worst.get("timestamp"), "fingerstick": worst.get("value"),
                      "cgm": worst.get("cgm_value"), "delta": worst.get("delta")},
            # CGM reads high/low vs meter (mean); >3 mg/dL to call a bias
            "bias": "cgm_high" if mean_delta > 3 else "cgm_low" if mean_delta < -3 else "balanced",
        }

    return {"error": "Unknown action", "_status": 400}
