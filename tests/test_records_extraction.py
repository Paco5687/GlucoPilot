"""Regressions for the 2026-09-17 medical-record extraction failure.

A lab image produced a stored summary reading "All values are within the
reference ranges" while the extracted rows themselves sat far outside their
extracted ranges (0.421 vs 0.027-0.106), and a related upload stored rows with
no test name and no collection date. These tests pin the deterministic
out-of-range computation, the name-repair re-prompt, and the needs_review
status for extractions that still come back anonymous or undated.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from server import db, records
from server.migrations import run_migrations

pytestmark = pytest.mark.risk_critical
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "golden" / "record_extraction_failure.json"


@pytest.fixture(scope="module")
def cases() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def records_db(tmp_path, monkeypatch):
    database = tmp_path / "app.sqlite3"
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    return database


def _stored_image(tmp_path: Path) -> Path:
    stored = tmp_path / "record.jpg"
    stored.write_bytes(b"synthetic-jpeg-bytes")
    return stored


def _record() -> dict:
    return db.create_entity(
        "MedicalRecord",
        {"filename": "synthetic-labs.jpg", "owner_email": records.OWNER_EMAIL, "status": "processing"},
    )


def _run_store(record: dict, stored: Path) -> dict:
    return asyncio.run(records._extract_and_store(record, stored, stored.suffix))


def test_range_status_is_deterministic():
    assert records._range_status(0.421, 0.027, 0.106) == "high"
    assert records._range_status(0.5, 1.0, 3.0) == "low"
    assert records._range_status(0.05, 0.02, 0.09) == "in_range"
    assert records._range_status(7.2, None, None) is None
    assert records._range_status(None, 0.02, 0.09) is None
    assert records._range_status("not-a-number", 0.02, 0.09) is None
    # One-sided ranges still evaluate.
    assert records._range_status(5.0, None, 4.0) == "high"
    assert records._range_status(5.0, 4.0, None) == "in_range"


def test_computed_note_counts_out_of_range_values(cases):
    case = cases["summary_contradiction"]
    labs = records._normalize_lab_results(case["extracted"], "synthetic-record")
    note, out_count, evaluated = records._computed_range_note(labs)

    assert out_count == case["expected_out_of_range"]
    assert evaluated == case["expected_evaluated"]
    assert f"{out_count} of {evaluated}" in note
    assert "OUT OF RANGE" in note
    assert "Synthetic Metabolite A 0.421 ng/mg (ref 0.027–0.106)" in note
    assert "Synthetic Metabolite C" in note
    # In-range and range-less rows are not listed as out of range.
    assert "Synthetic Metabolite B" not in note
    assert "Synthetic Ratio D" not in note


def test_computed_note_when_everything_is_in_range():
    labs = [{"test_name": "Synthetic B", "value": 0.05, "reference_low": 0.02, "reference_high": 0.09}]
    note, out_count, evaluated = records._computed_range_note(labs)
    assert (out_count, evaluated) == (0, 1)
    assert "within range" in note
    note, out_count, evaluated = records._computed_range_note(
        [{"test_name": "No range", "value": 1.0, "reference_low": None, "reference_high": None}]
    )
    assert (note, out_count, evaluated) == ("", 0, 0)


def test_stored_summary_cannot_contradict_extracted_values(cases, records_db, tmp_path, monkeypatch):
    """The exact 2026-09-17 shape: model summary says all-normal, values do not."""
    case = copy.deepcopy(cases["summary_contradiction"]["extracted"])

    async def fake_extract(images, page_numbers, repair_note=""):
        return copy.deepcopy(case)

    monkeypatch.setattr(records, "_extract", fake_extract)
    record = _run_store(_record(), _stored_image(tmp_path))

    assert record["status"] == "processed"
    assert record["out_of_range_count"] == 2
    assert record["range_evaluated_count"] == 3
    # The model's wrong claim is still present but can no longer stand alone:
    # the deterministic verdict is part of the same stored summary.
    assert "All values are within the reference ranges" in record["summary"]
    assert "2 of 3 results with reference ranges are OUT OF RANGE" in record["summary"]
    assert "Synthetic Metabolite A 0.421" in record["summary"]
    labs = db.query_entities("LabResult", {"record_id": record["id"], "owner_email": records.OWNER_EMAIL})
    assert len(labs) == 4
    assert all((lab.get("test_name") or "").strip() for lab in labs)
    assert all((lab.get("collected_date") or "").strip() for lab in labs)


def test_unnamed_rows_trigger_repair_prompt_and_needs_review(cases, records_db, tmp_path, monkeypatch):
    """Rows with values but no names are re-prompted once; if they stay
    anonymous the record is flagged needs_review instead of storing silently."""
    broken = cases["missing_names_and_dates"]["extracted"]
    calls: list[str] = []

    async def fake_extract(images, page_numbers, repair_note=""):
        calls.append(repair_note)
        return copy.deepcopy(broken)

    monkeypatch.setattr(records, "_extract", fake_extract)
    record = _run_store(_record(), _stored_image(tmp_path))

    assert len(calls) == 2
    assert calls[0] == ""
    assert "no test name" in calls[1]
    assert record["status"] == "needs_review"
    assert "1 extracted value(s) had no test name" in record["error"]
    assert "no collection date" in record["error"]
    # The anonymous row was not stored; the named-but-undated row was.
    labs = db.query_entities("LabResult", {"record_id": record["id"], "owner_email": records.OWNER_EMAIL})
    assert [lab["test_name"] for lab in labs] == ["Synthetic Metabolite B"]


def test_repair_pass_recovers_names_and_record_processes_cleanly(cases, records_db, tmp_path, monkeypatch):
    broken = cases["missing_names_and_dates"]["extracted"]
    repaired = cases["repaired"]["extracted"]
    calls: list[str] = []

    async def fake_extract(images, page_numbers, repair_note=""):
        calls.append(repair_note)
        return copy.deepcopy(repaired if repair_note else broken)

    monkeypatch.setattr(records, "_extract", fake_extract)
    record = _run_store(_record(), _stored_image(tmp_path))

    assert len(calls) == 2
    assert record["status"] == "processed"
    assert record["error"] == ""
    labs = db.query_entities("LabResult", {"record_id": record["id"], "owner_email": records.OWNER_EMAIL})
    assert sorted(lab["test_name"] for lab in labs) == ["Synthetic Metabolite A", "Synthetic Metabolite B"]
    assert all(lab["collected_date"] == "2026-09-14" for lab in labs)
    assert "1 of 2 results with reference ranges are OUT OF RANGE" in record["summary"]


def test_unnamed_result_count_ignores_zero_values_and_named_rows():
    part = {
        "lab_results": [
            {"test_name": "", "original_test_name": "", "original_value": "0", "value": 0},
            {"test_name": "Named", "value": 1.2},
            {"test_name": "", "original_value": ""},
        ],
        "measurements": [{"name": "", "value": 4.2}, {"name": "Spleen length", "value": 13.1}],
    }
    # Zero is a real value; empty value with no name is noise, not a lost result.
    assert records._unnamed_result_count(part) == 2
