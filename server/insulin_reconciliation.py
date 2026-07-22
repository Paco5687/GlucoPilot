"""Source-aware pump insulin reconciliation.

Pump-reported daily totals and totals calculated from event streams are kept
separate.  In particular, Glooko ``scheduled_basals`` records describe the
programmed schedule, not confirmed delivery, so they are never used to claim a
complete calculated total.  Tandem basal delivery intervals may produce a
calculated total only when they cover the complete local day without conflicts.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .typed_treatments import parse_pump_daily_total


RECONCILIATION_VERSION = "pump-insulin-reconciliation/1.0.0"
DELIVERED_COVERAGE_THRESHOLD = 0.99
BOLUS_DUPLICATE_WINDOW_SECONDS = 90
_SOURCE_PRIORITY = {"tandem": 0, "nightscout": 1, "glooko": 2, "csv": 3, "legacy": 4}


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _instant(value: Any, tz: ZoneInfo) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(timezone.utc)


def _local_day(instant: datetime, tz: ZoneInfo) -> str:
    return instant.astimezone(tz).date().isoformat()


def _day_bounds(day: str, tz: ZoneInfo) -> tuple[datetime, datetime]:
    local_date = date.fromisoformat(day)
    start = datetime.combine(local_date, time.min, tzinfo=tz)
    end = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _days_between(start: str, end: str) -> list[str]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        return []
    return [(first + timedelta(days=offset)).isoformat() for offset in range((last - first).days + 1)]


def _source(row: dict[str, Any]) -> str:
    return str(row.get("source") or "legacy").strip().lower() or "legacy"


def _source_rank(source: str) -> tuple[int, str]:
    return (_SOURCE_PRIORITY.get(source, 100), source)


def _record_key(row: dict[str, Any], fields: Iterable[str]) -> tuple[Any, ...]:
    source = _source(row)
    provider_id = str(row.get("ns_id") or row.get("source_record_id") or "").strip()
    if provider_id:
        return (source, "provider", provider_id)
    return (source, "content", *(row.get(field) for field in fields))


def _data_version(rows: list[dict[str, Any]]) -> str:
    payload = [
        {
            "id": row.get("id"),
            "source": _source(row),
            "source_record_id": row.get("ns_id") or row.get("source_record_id"),
            "timestamp": row.get("timestamp"),
            "updated_date": row.get("updated_date"),
            "type": row.get("type"),
            "event_type": row.get("event_type"),
            "amount": row.get("amount"),
            "duration": row.get("duration"),
            "absolute": row.get("absolute"),
            "notes": row.get("notes"),
        }
        for row in rows
    ]
    payload.sort(key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"), default=str))
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _reported_totals(rows: list[dict[str, Any]], tz: ZoneInfo) -> tuple[dict[str, Any], int]:
    by_day_source: dict[str, dict[str, list[dict[str, Any]]]] = {}
    seen: set[tuple[Any, ...]] = set()
    duplicates = 0
    for row in rows:
        if str(row.get("type") or "").lower() != "insulin":
            continue
        if str(row.get("event_type") or "").strip().lower() != "daily total":
            continue
        parsed = parse_pump_daily_total(row.get("notes"))
        occurred = _instant(row.get("timestamp"), tz)
        if parsed is None or occurred is None:
            continue
        key = _record_key(row, ("timestamp", "notes"))
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        # Daily Total is a day-labeled pump/export record.  Its established
        # contract uses the ISO date label directly; treating its timestamp as
        # an event instant shifts historical exports to the prior local day.
        day_label = str(row.get("timestamp") or "")[:10]
        try:
            day = date.fromisoformat(day_label).isoformat()
        except ValueError:
            day = _local_day(occurred, tz)
        by_day_source.setdefault(day, {}).setdefault(_source(row), []).append(parsed)

    output: dict[str, Any] = {}
    for day, by_source in by_day_source.items():
        candidates = []
        for source, values in sorted(by_source.items(), key=lambda item: _source_rank(item[0])):
            fields_complete = all(value["completeness"] == "complete" for value in values)
            total_units = round(sum(float(value["total_units"]) for value in values), 3)
            basal_units = (
                round(sum(float(value["basal_units"] or 0) for value in values), 3)
                if fields_complete
                else None
            )
            bolus_units = (
                round(sum(float(value["bolus_units"] or 0) for value in values), 3)
                if fields_complete
                else None
            )
            component_difference = (
                round(float(basal_units) + float(bolus_units) - total_units, 3)
                if fields_complete
                else None
            )
            components_match = component_difference is not None and abs(component_difference) <= 0.05
            complete = fields_complete and components_match
            candidates.append(
                {
                    "source": source,
                    "total_units": total_units,
                    "basal_units": basal_units,
                    "bolus_units": bolus_units,
                    "fields_complete": fields_complete,
                    "components_match": components_match,
                    "component_difference_units": component_difference,
                    "complete": complete,
                    "records": len(values),
                }
            )
        complete_candidates = [candidate for candidate in candidates if candidate["complete"]]
        distinct = {
            (candidate["total_units"], candidate["basal_units"], candidate["bolus_units"])
            for candidate in complete_candidates
        }
        conflict = len(distinct) > 1
        selected = complete_candidates[0] if complete_candidates and not conflict else None
        output[day] = {
            "selected": selected,
            "candidates": candidates,
            "conflict": conflict,
        }
    return output, duplicates


def _boluses(rows: list[dict[str, Any]], tz: ZoneInfo) -> tuple[dict[str, dict[str, float | int]], int]:
    candidates: list[tuple[datetime, float, str, dict[str, Any]]] = []
    seen: set[tuple[Any, ...]] = set()
    duplicates = 0
    for row in rows:
        if str(row.get("type") or "").lower() != "insulin":
            continue
        if str(row.get("event_type") or "").strip().lower() == "daily total":
            continue
        amount = _number(row.get("amount"))
        occurred = _instant(row.get("timestamp"), tz)
        if amount is None or amount == 0 or occurred is None:
            continue
        key = _record_key(row, ("timestamp", "event_type", "amount", "insulin_type"))
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        candidates.append((occurred, amount, _source(row), row))

    # Exact/same-dose records from different connectors within the established
    # cross-source tolerance describe one dose.  Same-source nearby doses remain
    # separate because split/extended boluses can legitimately be close together.
    selected: list[tuple[datetime, float, str, dict[str, Any]]] = []
    for candidate in sorted(candidates, key=lambda value: (value[0], _source_rank(value[2]))):
        occurred, amount, source, _ = candidate
        duplicate_index = next(
            (
                index
                for index, prior in enumerate(selected)
                if prior[2] != source
                and abs((occurred - prior[0]).total_seconds()) <= BOLUS_DUPLICATE_WINDOW_SECONDS
                and abs(amount - prior[1]) <= 0.01
            ),
            None,
        )
        if duplicate_index is None:
            selected.append(candidate)
            continue
        duplicates += 1
        if _source_rank(source) < _source_rank(selected[duplicate_index][2]):
            selected[duplicate_index] = candidate

    by_day: dict[str, dict[str, float | int]] = {}
    for occurred, amount, _, _ in selected:
        day = _local_day(occurred, tz)
        entry = by_day.setdefault(day, {"units": 0.0, "events": 0})
        entry["units"] = float(entry["units"]) + amount
        entry["events"] = int(entry["events"]) + 1
    return {
        day: {"units": round(float(value["units"]), 3), "events": int(value["events"])}
        for day, value in by_day.items()
    }, duplicates


def _basal_class(source: str) -> str:
    if source == "tandem":
        return "delivered"
    if source == "glooko":
        return "scheduled"
    return "reported_adjustment"


def _basal_segments(
    rows: list[dict[str, Any]], tz: ZoneInfo
) -> tuple[dict[str, dict[str, list[tuple[datetime, datetime, float]]]], int, int]:
    output: dict[str, dict[str, list[tuple[datetime, datetime, float]]]] = {}
    seen: set[tuple[Any, ...]] = set()
    duplicates = invalid = 0
    for row in rows:
        treatment_type = str(row.get("type") or "").lower()
        if treatment_type not in {"tempbasal", "suspension"}:
            continue
        started = _instant(row.get("timestamp"), tz)
        duration_minutes = _number(row.get("duration"))
        rate = _number(row.get("absolute"))
        if treatment_type == "suspension" and rate is None:
            rate = 0.0
        if started is None or duration_minutes is None or duration_minutes <= 0 or rate is None:
            invalid += 1
            continue
        key = _record_key(row, ("timestamp", "type", "duration", "absolute", "percent"))
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        ended = started + timedelta(minutes=duration_minutes)
        classification = _basal_class(_source(row))
        cursor = started.astimezone(tz).date()
        final = (ended - timedelta(microseconds=1)).astimezone(tz).date()
        while cursor <= final:
            day = cursor.isoformat()
            day_start, day_end = _day_bounds(day, tz)
            clipped_start, clipped_end = max(started, day_start), min(ended, day_end)
            if clipped_start < clipped_end:
                output.setdefault(day, {}).setdefault(classification, []).append(
                    (clipped_start, clipped_end, rate)
                )
            cursor += timedelta(days=1)
    return output, duplicates, invalid


def _integrate(
    segments: list[tuple[datetime, datetime, float]], day: str, tz: ZoneInfo
) -> dict[str, Any]:
    day_start, day_end = _day_bounds(day, tz)
    day_seconds = (day_end - day_start).total_seconds()
    if not segments:
        return {"units": 0.0, "coverage_pct": 0.0, "conflict": False, "overlap_seconds": 0}
    boundaries = sorted({point for start, end, _ in segments for point in (start, end)})
    units = coverage = overlap = 0.0
    conflict = False
    for left, right in zip(boundaries, boundaries[1:], strict=False):
        active = [rate for start, end, rate in segments if start < right and end > left]
        if not active:
            continue
        seconds = (right - left).total_seconds()
        coverage += seconds
        distinct_rates = {round(rate, 6) for rate in active}
        if len(active) > 1:
            overlap += seconds
        if len(distinct_rates) > 1:
            conflict = True
            continue
        units += next(iter(distinct_rates)) * seconds / 3600
    return {
        "units": round(units, 3),
        "coverage_pct": round(coverage / day_seconds * 100, 1) if day_seconds else 0.0,
        "conflict": conflict,
        "overlap_seconds": round(overlap),
    }


def reconcile_treatments(
    treatments: list[dict[str, Any]],
    tz: ZoneInfo,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Return deterministic daily and aggregate pump insulin reconciliation."""
    reported, reported_duplicates = _reported_totals(treatments, tz)
    bolus, bolus_duplicates = _boluses(treatments, tz)
    basal, basal_duplicates, invalid_basal = _basal_segments(treatments, tz)
    observed_days = sorted(set(reported) | set(bolus) | set(basal))
    if start_date is not None and end_date is not None:
        days = _days_between(start_date, end_date)
    else:
        days = observed_days

    reconciled_days = []
    for day in days:
        report = reported.get(day, {"selected": None, "candidates": [], "conflict": False})
        delivered = _integrate(basal.get(day, {}).get("delivered", []), day, tz)
        scheduled = _integrate(basal.get(day, {}).get("scheduled", []), day, tz)
        adjustments = _integrate(basal.get(day, {}).get("reported_adjustment", []), day, tz)
        delivered_complete = (
            delivered["coverage_pct"] >= DELIVERED_COVERAGE_THRESHOLD * 100
            and not delivered["conflict"]
        )
        daily_bolus = bolus.get(day, {"units": 0.0, "events": 0})
        calculated_total = round(delivered["units"] + float(daily_bolus["units"]), 3) if delivered_complete else None
        selected_report = report["selected"]
        discrepancy = None
        if selected_report is not None and calculated_total is not None:
            difference = round(calculated_total - selected_report["total_units"], 3)
            discrepancy = {
                "units": difference,
                "absolute_units": abs(difference),
                "percent_of_reported": round(abs(difference) / selected_report["total_units"] * 100, 1)
                if selected_report["total_units"]
                else None,
                "matches_rounding": abs(difference) <= 0.05,
            }

        if selected_report is not None:
            completeness = "complete_reported"
        elif delivered_complete:
            completeness = "complete_calculated"
        elif report["conflict"]:
            completeness = "conflicting_reported_totals"
        elif report["candidates"]:
            completeness = "incomplete_reported_total"
        elif delivered["coverage_pct"] > 0:
            completeness = "partial_delivered_basal"
        elif scheduled["coverage_pct"] > 0:
            completeness = "scheduled_basal_only"
        elif float(daily_bolus["units"]) > 0:
            completeness = "bolus_only"
        else:
            completeness = "no_insulin_data"

        reconciled_days.append(
            {
                "date": day,
                "completeness": completeness,
                "pump_reported": report,
                "calculated": {
                    "total_units": calculated_total,
                    "bolus_units": daily_bolus["units"],
                    "bolus_events": daily_bolus["events"],
                    "delivered_basal_units": delivered["units"],
                    "delivered_basal_coverage_pct": delivered["coverage_pct"],
                    "delivered_basal_conflict": delivered["conflict"],
                },
                "scheduled_basal": {
                    "units": scheduled["units"],
                    "coverage_pct": scheduled["coverage_pct"],
                    "conflict": scheduled["conflict"],
                },
                "reported_basal_adjustments": {
                    "units": adjustments["units"],
                    "coverage_pct": adjustments["coverage_pct"],
                    "conflict": adjustments["conflict"],
                },
                "discrepancy": discrepancy,
            }
        )

    complete_reported = [
        day for day in reconciled_days if day["pump_reported"]["selected"] is not None
    ]
    complete_calculated = [
        day for day in reconciled_days if day["calculated"]["total_units"] is not None
    ]
    incomplete = [
        day
        for day in reconciled_days
        if day["pump_reported"]["selected"] is None and day["calculated"]["total_units"] is None
    ]
    activity_days = [day["date"] for day in reconciled_days if day["completeness"] != "no_insulin_data"]
    limitations = []
    if any(day["scheduled_basal"]["coverage_pct"] > 0 for day in reconciled_days):
        limitations.append("Glooko basal records are scheduled/programmed rates, not confirmed delivery.")
    if any(day["completeness"] in {"bolus_only", "partial_delivered_basal"} for day in reconciled_days):
        limitations.append("Days without complete delivered basal coverage do not have a calculated TDD.")
    if any(day["pump_reported"]["conflict"] for day in reconciled_days):
        limitations.append("Conflicting pump-reported totals are shown separately and excluded from summaries.")
    if any(day["completeness"] == "incomplete_reported_total" for day in reconciled_days):
        limitations.append("Pump-reported totals with missing or inconsistent components are excluded from summaries.")
    if invalid_basal:
        limitations.append("Basal records without both duration and an absolute rate are excluded from calculated delivery.")
    discrepancies = [
        {
            "date": day["date"],
            "pump_reported_units": day["pump_reported"]["selected"]["total_units"],
            "calculated_units": day["calculated"]["total_units"],
            **day["discrepancy"],
        }
        for day in reconciled_days
        if day["discrepancy"] and not day["discrepancy"]["matches_rounding"]
    ]
    reported_sources: dict[str, int] = {}
    for day in complete_reported:
        source = day["pump_reported"]["selected"]["source"]
        reported_sources[source] = reported_sources.get(source, 0) + 1

    return {
        "algorithm_version": RECONCILIATION_VERSION,
        "input_data_version": _data_version(treatments),
        "days": reconciled_days,
        "summary": {
            "pump_reported_avg_tdd": round(
                sum(day["pump_reported"]["selected"]["total_units"] for day in complete_reported)
                / len(complete_reported),
                1,
            )
            if complete_reported
            else None,
            "pump_reported_days": len(complete_reported),
            "pump_reported_sources": dict(sorted(reported_sources.items())),
            "calculated_avg_tdd": round(
                sum(day["calculated"]["total_units"] for day in complete_calculated)
                / len(complete_calculated),
                1,
            )
            if complete_calculated
            else None,
            "calculated_days": len(complete_calculated),
            "incomplete_days": len(incomplete),
            "latest_complete_date": max(
                [day["date"] for day in complete_reported + complete_calculated], default=None
            ),
            "latest_activity_date": max(activity_days, default=None),
            "calculated_source": "tandem_delivered" if complete_calculated else None,
            "discrepancy_days": len(discrepancies),
            "discrepancies": discrepancies,
            "duplicates_suppressed": reported_duplicates + bolus_duplicates + basal_duplicates,
            "invalid_basal_segments": invalid_basal,
            "limitations": limitations,
        },
    }
