"""Same-draw lab duplicates and non-standard ("optimal range") reports.

Synthetic data only. The shape mirrors what one draw looks like when the
performing lab's report and a functional-medicine report both reach LabResult:
same date and value, different names, different ranges, conflicting flags.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from server import companion_evidence, db, lab_dedupe
from server.evidence_bundle import (
    EvidenceBundleQuery,
    EvidenceDomain,
    build_bundle,
    clear_bundle_cache,
)
from server.migrations import run_migrations

pytestmark = pytest.mark.risk_critical
OWNER = "owner@glucopilot.local"
DRAW = "2026-06-23"

# (lab name, optimal-report name, value, unit, lab range, optimal range, optimal flag)
SHARED = (
    ("Triiodothyronine (T3), Free", "Free T3", 2.4, "pg/mL", (2.0, 4.4), (2.8, 3.5), "low"),
    ("T4,Free(Direct)", "Free T4", 0.95, "ng/dL", (0.82, 1.77), (1.0, 1.5), "low"),
    ("Hemoglobin A1c", "HbA1c", 5.6, "%", (4.8, 5.6), (5.0, 5.5), "high"),
    ("TSH", "TSH", 1.57, "uIU/mL", (0.45, 4.5), (1.0, 3.0), "normal"),
    ("Calcium", "Calcium", 8.9, "mg/dL", (8.7, 10.2), (9.4, 10.1), "low"),
    ("Albumin", "Albumin", 4.3, "g/dL", (3.5, 5.5), (4.0, 5.0), "normal"),
)


def _row(row_id, record, name, value, unit, bounds, flag, *, collected=DRAW, **extra):
    return {
        "id": row_id,
        "record_id": record,
        "test_name": name,
        "value": value,
        "unit": unit,
        "reference_low": bounds[0] if bounds else None,
        "reference_high": bounds[1] if bounds else None,
        "flag": flag,
        "collected_date": collected,
        **extra,
    }


def _pair_rows(shared=SHARED):
    rows = []
    for index, (lab_name, opt_name, value, unit, lab_range, opt_range, opt_flag) in enumerate(shared):
        rows.append(_row(f"lab-{index}", "lab-report", lab_name, value, unit, lab_range, ""))
        rows.append(_row(f"opt-{index}", "optimal-report", opt_name, value, unit, opt_range, opt_flag))
    return rows


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Free T3", "Triiodothyronine (T3), Free"),
        ("Free T4", "T4,Free(Direct)"),
        ("HbA1c", "Hemoglobin A1c"),
        ("Reverse T3", "Reverse T3, Serum"),
        ("Cholesterol - Total", "Cholesterol, Total"),
        ("Alk Phos", "Alkaline Phosphatase"),
        ("Thyroglobulin Abs", "Thyroglobulin Antibody"),
        ("Vitamin D (25-OH)", "Vitamin D, 25-Hydroxy"),
        ("Cholesterol/HDL Ratio", "T. Chol/HDL Ratio"),
    ],
)
def test_lab_spellings_of_one_analyte_match(first, second):
    assert lab_dedupe.canonical_analyte(first) == lab_dedupe.canonical_analyte(second)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Bilirubin, Total", "Bilirubin, Direct"),
        ("Testosterone, Total", "Testosterone, Free"),
        ("Free T3", "Free T4"),
        ("Sjogren's Antibody (SS-A)", "Sjogren's Antibody (SS-B)"),
    ],
)
def test_different_analytes_stay_apart(first, second):
    assert lab_dedupe.canonical_analyte(first) != lab_dedupe.canonical_analyte(second)


def test_optimal_range_report_is_inferred_from_its_twins_and_never_dropped():
    rows = _pair_rows()
    nonstandard = lab_dedupe.nonstandard_range_records(rows)
    assert nonstandard == {"optimal-report"}
    annotations = lab_dedupe.resolve(rows, nonstandard_records=nonstandard)
    assert set(annotations) == {row["id"] for row in rows}
    for index in range(len(SHARED)):
        assert annotations[f"opt-{index}"]["duplicate_of"] == f"lab-{index}"
        assert annotations[f"opt-{index}"]["reference_basis"] == "nonstandard"
        assert "duplicate_of" not in annotations[f"lab-{index}"]
        assert annotations[f"lab-{index}"]["same_draw_count"] == 2
        assert annotations[f"lab-{index}"]["same_draw_record_ids"] == ["lab-report", "optimal-report"]


def test_too_few_twins_or_identical_copies_are_not_nonstandard():
    assert lab_dedupe.nonstandard_range_records(_pair_rows(SHARED[:4])) == set()
    copies = [
        {**row, "id": f"copy-{row['id']}", "record_id": "lab-report-copy"}
        for row in _pair_rows() if row["record_id"] == "lab-report"
    ]
    originals = [row for row in _pair_rows() if row["record_id"] == "lab-report"]
    assert lab_dedupe.nonstandard_range_records([*originals, *copies]) == set()


def test_kept_row_prefers_a_flag_that_agrees_with_its_own_range():
    rows = [
        _row("a", "report-a", "Ferritin", 28, "ng/mL", (16, 154), "low"),
        _row("b", "report-b", "Ferritin", 28, "ng/mL", (16, 154), "normal"),
    ]
    annotations = lab_dedupe.resolve(rows)
    assert annotations["a"]["duplicate_of"] == "b"


def test_conflicting_units_or_dates_are_different_results():
    rows = [
        _row("a", "report-a", "Vitamin B12", 500, "pg/mL", None, ""),
        _row("b", "report-b", "Vitamin B12", 500, "pmol/L", None, ""),
        _row("c", "report-c", "Vitamin B12", 500, "pg/mL", None, "", collected="2026-06-24"),
        _row("d", "report-d", "Vitamin B12", "not detected", "pg/mL", None, ""),
    ]
    annotations = lab_dedupe.resolve(rows)
    assert all("duplicate_of" not in annotation for annotation in annotations.values())


@pytest.fixture
def lab_database(tmp_path, monkeypatch):
    database = tmp_path / "data" / "app.sqlite3"
    database.parent.mkdir()
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(
        companion_evidence,
        "refresh_contradictions",
        lambda: {"enabled": True, "refreshed": False},
    )
    monkeypatch.setenv("RELATIONSHIP_READS_ENABLED", "false")
    monkeypatch.setenv("EVIDENCE_SET_READS_ENABLED", "false")
    clear_bundle_cache()
    records = {
        key: db.create_entity("MedicalRecord", {
            "owner_email": OWNER,
            "record_date": DRAW,
            "title": title,
            "filename": f"{key}.pdf",
            "doc_type": "lab_report",
        })["id"]
        for key, title in (("lab-report", "Synthetic Lab Panel"), ("optimal-report", "Synthetic Optimal Report"))
    }
    ids = {}
    for row in _pair_rows():
        row_id = row.pop("id")
        row["record_id"] = records[row["record_id"]]
        created = db.create_entity("LabResult", {
            **row,
            "owner_email": OWNER,
            "category": "Thyroid",
            "verification_status": "unverified",
            "validation_status": "valid",
            "parser_confidence": 0.95,
        })
        ids[row_id] = created["id"]
    only = _row("opt-only", records["optimal-report"], "Magnesium, RBC", 4.7, "mg/dL", (6.0, 6.8), "low")
    only.pop("id")
    ids["opt-only"] = db.create_entity("LabResult", {
        **only,
        "owner_email": OWNER,
        "category": "Minerals",
        "verification_status": "unverified",
        "validation_status": "valid",
    })["id"]
    return ids, records


def _labs_bundle():
    return build_bundle(EvidenceBundleQuery(
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 9, 30, tzinfo=timezone.utc),
        domains=[EvidenceDomain.LABS],
        question_intent="free T3 thyroid lab result",
        item_budget=40,
    ))


def test_bundle_marks_duplicates_and_attributes_the_optimal_flag(lab_database):
    ids, records = lab_database
    bundle = _labs_bundle()
    labs = {
        item["entity_id"]: item
        for item in bundle["evidence"]["direct_observations"]
        if item["entity_type"] == "LabResult"
    }
    assert len(labs) == 2 * len(SHARED) + 1           # nothing is dropped from the bundle
    kept, twin = labs[ids["lab-0"]], labs[ids["opt-0"]]
    assert twin["data"]["duplicate_of"] == ids["lab-0"]
    assert twin["data"]["reference_basis"] == "nonstandard"
    assert any("optimal" in limitation for limitation in twin["confidence"]["limitations"])
    assert kept["data"]["reference_basis"] == "standard"
    assert kept["data"]["nonstandard_flags"] == [{
        "flag": "low", "reference_low": 2.8, "reference_high": 3.5, "record_id": records["optimal-report"],
    }]
    documents = {link["record_id"] for link in kept["source_links"] if link["kind"] == "source_document"}
    assert documents == set(records.values())
    order = [item["entity_id"] for item in bundle["evidence"]["direct_observations"]]
    assert order.index(ids["lab-0"]) < order.index(ids["opt-0"])
    opposing = {entry["evidence_item_id"] for entry in bundle["evidence"]["opposing_evidence"]}
    assert f"entity:LabResult:{ids['opt-only']}" not in opposing
    assert f"entity:LabResult:{ids['opt-0']}" not in opposing


def _alias_for(public, entity_id):
    return next(item["alias"] for item in public["evidence_items"] if item["entity_id"] == entity_id)


def test_companion_sees_each_draw_once_and_only_attributed_optimal_flags(lab_database):
    ids, _records = lab_database
    public, reasoning = companion_evidence.build_context(
        "How is my thyroid? Is my free T3 low?", as_of=date(2026, 9, 30), refresh=False
    )
    entity_ids = {item["entity_id"] for item in public["evidence_items"]}
    assert ids["lab-0"] in entity_ids and ids["opt-0"] not in entity_ids
    alias = _alias_for(public, ids["lab-0"])
    item = next(item for item in reasoning["items"] if item["alias"] == alias)
    assert item["data"]["flag"] == ""
    assert "rates it low against its narrower 'optimal' range (2.8-3.5)" in item["data"]["flag_note"]
    assert item["nonstandard_flag"] == "low"

    reply, evidence = companion_evidence.finalize_reply(
        f"Your free T3 is low [{alias}].\n"
        f"The functional report rates your free T3 low against its optimal range [{alias}].",
        public, [], [],
    )
    assert "Your free T3 is low" not in reply
    assert "functional report rates your free T3 low" in reply
    assert evidence["omissions"]["count"] == 1

    # Attribution covers only a cited row that carries an optimal-range flag.
    tsh = _alias_for(public, ids["lab-3"])
    reply, evidence = companion_evidence.finalize_reply(
        f"Your TSH is 1.57, though a functional-medicine report notes it's below their optimal range [{tsh}].",
        public, [], [],
    )
    assert evidence["omissions"]["count"] == 1


def test_a_result_only_in_the_optimal_report_is_treated_as_unflagged(lab_database):
    ids, _records = lab_database
    public, reasoning = companion_evidence.build_context(
        "Is my RBC magnesium low? lab result", as_of=date(2026, 9, 30), refresh=False
    )
    alias = _alias_for(public, ids["opt-only"])
    item = next(item for item in reasoning["items"] if item["alias"] == alias)
    assert "flag" not in item["data"] and "reference_low" not in item["data"]
    assert "rates this low against its optimal range (6.0-6.8)" in item["data"]["flag_note"]
    public_item = next(item for item in public["evidence_items"] if item["alias"] == alias)
    assert public_item["flag"] is None and public_item["nonstandard_flag"] == "low"
    reply, evidence = companion_evidence.finalize_reply(
        f"Your RBC magnesium is low [{alias}].", public, [], []
    )
    assert evidence["omissions"]["count"] == 1
