"""Same-draw lab duplicates and non-standard reference ranges.

One blood draw often reaches LabResult twice: the performing lab's report and
a second document that re-reports the same values, sometimes judged against
its own narrower "optimal" ranges (a functional-medicine report). The two rows
then disagree on the flag even though the measurement is identical.

Nothing here writes or deletes. Rows are grouped by draw (collection date,
analyte and value), one row per group is kept for display and reasoning, and
the others point at it with ``duplicate_of``. A report whose ranges are
consistently narrower than the performing lab's for the same draws is marked
``nonstandard``: its flags measure distance from an optimal range, not from
the lab's reference range, so they are not treated as high or low.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any

from .lab_audit import effective_flag
from .lab_names import analyte_key

ALGORITHM_VERSION = "lab-same-draw/1.1.0"
MIN_RANGE_COMPARISONS = 5
MIN_NARROWER_SHARE = 0.8
STANDARD = "standard"
NONSTANDARD = "nonstandard"

_VERIFIED = {"approved", "edited"}


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(str(value).strip().lstrip("<>=~").replace(",", ""))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _unit(row: dict[str, Any]) -> str:
    return re.sub(r"\s+", "", str(row.get("unit") or "")).lower().replace("μ", "u").replace("µ", "u")


def same_draw_key(row: dict[str, Any]) -> tuple[str, str, float] | None:
    """(collection date, analyte, value), or None when the row cannot be
    matched safely: no date, no analyte, or a non-numeric value."""
    collected = str(row.get("collected_date") or "")[:10]
    analyte = analyte_key(row.get("test_name"))
    value = _number(row.get("value"))
    if not collected or not analyte or value is None:
        return None
    return collected, analyte, value


def _groups(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    by_key: dict[tuple[str, str, float], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = same_draw_key(row)
        if key is not None:
            by_key[key].append(row)
    groups = []
    for members in by_key.values():
        if len(members) < 2:
            continue
        # Same date, analyte and value but different units are different results.
        by_unit: dict[str, list[dict[str, Any]]] = defaultdict(list)
        unitless = []
        for row in members:
            (by_unit[_unit(row)] if _unit(row) else unitless).append(row)
        if len(by_unit) > 1:
            groups.extend(group for group in by_unit.values() if len(group) > 1)
            continue
        merged = [*next(iter(by_unit.values()), []), *unitless]
        if len(merged) > 1:
            groups.append(merged)
    return groups


def _range(row: dict[str, Any]) -> tuple[float, float] | None:
    low, high = _number(row.get("reference_low")), _number(row.get("reference_high"))
    if low is None or high is None or high <= low:
        return None
    return low, high


def nonstandard_range_records(rows: list[dict[str, Any]]) -> set[str]:
    """Reports whose reference ranges are narrower than another report's for
    the same draws, in at least MIN_NARROWER_SHARE of MIN_RANGE_COMPARISONS or
    more comparisons. A performing lab and an "optimal range" report diverge
    this way on almost every shared result; two copies of one lab report do not."""
    narrower: dict[str, int] = defaultdict(int)
    compared: dict[str, int] = defaultdict(int)
    for group in _groups(rows):
        for row in group:
            mine = _range(row)
            if mine is None:
                continue
            for other in group:
                if other.get("record_id") == row.get("record_id"):
                    continue
                theirs = _range(other)
                if theirs is None or theirs == mine:
                    continue
                record = str(row.get("record_id") or "")
                compared[record] += 1
                if mine[1] - mine[0] < theirs[1] - theirs[0]:
                    narrower[record] += 1
    return {
        record
        for record, count in compared.items()
        if record and count >= MIN_RANGE_COMPARISONS and narrower[record] / count >= MIN_NARROWER_SHARE
    }


def _flag_consistent(row: dict[str, Any]) -> bool:
    """Whether the row's stored flag survives its own value and reference
    range (see ``lab_audit.effective_flag``, which the Visit Report and
    Clinician Brief use too). A row that calls 28 low against 16-154 was
    mis-extracted."""
    return effective_flag(row)[1] is None


def _preference(row: dict[str, Any], nonstandard: set[str]) -> tuple[Any, ...]:
    verification = str(row.get("verification_status") or "").lower()
    return (
        str(row.get("record_id") or "") in nonstandard,
        verification not in _VERIFIED,
        str(row.get("validation_status") or "").lower() == "invalid",
        not _flag_consistent(row),
        _range(row) is None,
        str(row.get("record_id") or ""),
        str(row.get("id") or ""),
    )


def resolve(
    rows: list[dict[str, Any]],
    *,
    nonstandard_records: set[str] | frozenset[str] = frozenset(),
) -> dict[str, dict[str, Any]]:
    """Annotations by row id. Every row gets ``reference_basis``; a row in a
    same-draw group also gets ``same_draw_count``, and every row but the kept
    one gets ``duplicate_of`` (the kept row's id). Rows are never dropped here;
    the caller decides whether a duplicate is shown."""
    nonstandard = set(nonstandard_records)
    annotations = {
        str(row.get("id")): {
            "reference_basis": NONSTANDARD if str(row.get("record_id") or "") in nonstandard else STANDARD,
        }
        for row in rows
        if row.get("id")
    }
    for group in _groups([row for row in rows if row.get("id")]):
        kept = min(group, key=lambda row: _preference(row, nonstandard))
        kept_id = str(kept["id"])
        records = sorted({str(row.get("record_id") or "") for row in group} - {""})
        for row in group:
            annotation = annotations[str(row["id"])]
            annotation["same_draw_count"] = len(group)
            annotation["same_draw_record_ids"] = records
            if str(row["id"]) != kept_id:
                annotation["duplicate_of"] = kept_id
    return annotations
