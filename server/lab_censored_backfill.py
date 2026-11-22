"""Restore what a lab printed, on rows extracted before the extractor kept it.

Lab rows extracted before ``original_value`` existed lost two kinds of text:

- A printed "<55" (below the assay's reporting limit) was stored as the bare
  number 55. Beside a 55 cutoff that reads as a borderline result.
- A placeholder such as "IN PROGRESS" was stored as the number 0.

This one-time repair re-reads each such row's source PDF text. When the lab
printed a comparison sign directly before that same number on the test's line,
it stores the printed form ("<55") in ``original_value``. When a row holds 0
and the test's line printed a pending status instead of a result, it stores
that status and marks the row invalid, which keeps it out of the Visit Report,
Clinician Brief and Health Overview.

It never changes ``value`` and never deletes a row. Rows from images or from
PDFs without a text layer, and rows a person has approved or edited, are left
alone. Every repaired row carries ``original_value_source`` = ``SOURCE``, so
the repair can be found and undone.
"""

from __future__ import annotations

import json
import logging
import math
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from . import db, lab_audit
from .config import DATA_DIR, MIGRATION_BACKUP_DIR, OWNER_EMAIL

log = logging.getLogger("glucopilot.lab_censored_backfill")

SOURCE = "source-text-backfill/1"
SETTING_KEY = "lab_censored_backfill_v1"
RECORDS_DIR = DATA_DIR / "records"
# Of the name's distinctive words, the share the printed line must contain.
MIN_NAME_MATCH = 0.6

_WORD = re.compile(r"[a-z0-9]+")
# A number printed as a result, with any comparison sign before it. Numbers
# glued to letters, dots, colons or slashes (dates, "1:5" titers, "25-OH"
# inside a name) are not results.
_RESULT = re.compile(r"(?<![\w.])(<=|>=|=<|=>|≤|≥|<|>)?\s?(-?\d+(?:,\d{3})*(?:\.\d+)?)(?![\w.:/])")
_GENERIC = {"ab", "abs", "antibody", "antibodies", "level", "serum", "plasma", "total", "the", "and", "of", "test", "w"}
# Printed where a result would be when there is none yet (or none at all).
_PENDING = re.compile(
    r"[\s),.:]*(in progress|pending|test not performed|not performed|tnp|qns|cancell?ed)\b",
    re.IGNORECASE,
)


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _name_words(test_name: str) -> list[str]:
    words = _words(test_name)
    distinctive = [word for word in words if word not in _GENERIC]
    return list(dict.fromkeys(distinctive or words))


def _same_number(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-6, abs_tol=1e-9)


def _line_results(text: str, test_name: str) -> list[tuple[float, str, Any]]:
    """(name score, kind, result) for each line that names the test.

    A line names the test when it holds most of the name's distinctive words.
    Its result is what follows the last of those words: a pending status
    ("status", "IN PROGRESS"), else the first number ("number", (sign, text)).
    """
    wanted = _name_words(test_name)
    if not wanted:
        return []
    found = []
    for line in text.splitlines():
        lowered = line.lower()
        present = set(_words(lowered)) & set(wanted)
        score = len(present) / len(wanted)
        if score < MIN_NAME_MATCH:
            continue
        end = 0
        for word in present:
            match = re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", lowered)
            if match:
                end = max(end, match.end())
        if status := _PENDING.match(line, end):
            found.append((score, "status", status[1].upper()))
        elif result := _RESULT.search(line, end):
            found.append((score, "number", (result[1] or "", result[2])))
    return found


def printed_value(text: str, test_name: str, value: float) -> str | None:
    """The comparison-signed value ("<55") the document prints for this test,
    or None when it prints the plain number, prints it nowhere, or the lines
    that best match the name disagree."""
    matching = [
        (score, f"{sign}{number}" if sign else "")
        for score, kind, result in _line_results(text, test_name)
        if kind == "number"
        for sign, number in [result]
        if _same_number(float(number.replace(",", "")), value)
    ]
    if not matching:
        return None
    best = max(score for score, _ in matching)
    found = {printed for score, printed in matching if score == best}
    # A plain "55" on an equally good line contradicts a "<55": leave it.
    if len(found) == 1 and (printed := next(iter(found))):
        return printed
    return None


def printed_status(text: str, test_name: str) -> str | None:
    """The pending status ("IN PROGRESS") the document prints in place of this
    test's result, or None when the best-matching lines print a number or
    disagree."""
    lines = _line_results(text, test_name)
    if not lines:
        return None
    best = max(score for score, _, _ in lines)
    found = {(kind, result) for score, kind, result in lines if score == best}
    if len(found) == 1 and (only := next(iter(found)))[0] == "status":
        return only[1]
    return None


def _pdf_text(path: Path) -> str:
    try:
        completed = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError) as err:
        log.warning("pdftotext failed for %s: %s", path.name, err)
        return ""
    return completed.stdout if completed.returncode == 0 else ""


def _needs_repair(lab: dict[str, Any]) -> bool:
    if lab.get("original_value") is not None or lab.get("value_text") is not None:
        return False
    if str(lab.get("verification_status") or "").lower() in {"approved", "edited", "rejected"}:
        return False
    try:
        return math.isfinite(float(lab.get("value")))
    except (TypeError, ValueError):
        return False


def find_repairs(records_dir: Path | None = None) -> dict[str, Any]:
    """The rows the repair would restore, without writing anything."""
    records_dir = records_dir or RECORDS_DIR
    labs_by_record: dict[str, list[dict[str, Any]]] = {}
    for lab in db.query_entities("LabResult", {"owner_email": OWNER_EMAIL}):
        if _needs_repair(lab) and lab.get("record_id"):
            labs_by_record.setdefault(lab["record_id"], []).append(lab)
    repairs: list[dict[str, Any]] = []
    scanned = 0
    for record in db.query_entities("MedicalRecord", {"owner_email": OWNER_EMAIL}):
        labs = labs_by_record.get(record["id"])
        stored = str(record.get("stored_as") or "")
        if not labs or not stored.lower().endswith(".pdf"):
            continue
        path = records_dir / stored
        if not path.is_file():
            continue
        text = _pdf_text(path)
        if not text.strip():
            continue
        scanned += 1
        for lab in labs:
            name, value = str(lab.get("test_name") or ""), float(lab["value"])
            repair = {"id": lab["id"], "test_name": lab.get("test_name"), "value": lab["value"]}
            if printed := printed_value(text, name, value):
                repairs.append({**repair, "printed": printed})
            elif value == 0 and (status := printed_status(text, name)):
                repairs.append({**repair, "printed": status, "not_a_result": True})
    return {
        "candidates": sum(len(labs) for labs in labs_by_record.values()),
        "documents_read": scanned,
        "repairs": repairs,
    }


def apply_repairs(repairs: list[dict[str, Any]]) -> int:
    restored = 0
    with db.connect() as connection:
        for repair in repairs:
            patch: dict[str, Any] = {"original_value": repair["printed"], "original_value_source": SOURCE}
            if repair.get("not_a_result"):
                patch["validation_status"] = "invalid"
                patch["validation_issues"] = [lab_audit.not_a_result_issue(repair["printed"])]
            if db.update_entity("LabResult", repair["id"], patch, connection=connection):
                restored += 1
    return restored


def ensure_backfilled() -> dict[str, Any] | None:
    """Run the repair once per database. Backs the database up first when
    there is anything to change; a failed backup leaves the repair for the
    next start."""
    if db.get_setting(SETTING_KEY):
        return None
    found = find_repairs()
    summary: dict[str, Any] = {
        "ran_at": int(time.time()),
        "candidates": found["candidates"],
        "documents_read": found["documents_read"],
        "restored": 0,
    }
    if found["repairs"]:
        from .backup import create_verified_backup

        backup_path, _ = create_verified_backup(DATA_DIR, MIGRATION_BACKUP_DIR, reason="pre-lab-censored-backfill")
        summary["backup"] = backup_path.name
        summary["restored"] = apply_repairs(found["repairs"])
    db.set_setting(SETTING_KEY, json.dumps(summary))
    log.info("lab below-limit values restored: %s", summary)
    return summary


if __name__ == "__main__":  # python -m server.lab_censored_backfill  (dry run)
    result = find_repairs()
    for repair in result["repairs"]:
        print(f"{repair['test_name']}: {repair['value']} -> {repair['printed']}")
    print(f"{len(result['repairs'])} of {result['candidates']} rows; {result['documents_read']} documents read")
