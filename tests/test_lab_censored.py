"""A result a lab prints as "<55" is below its reporting limit, not 55.

Every reader shows it as reported, the flag rule never calls it borderline or
high against its own cutoff, and a one-time repair restores the sign on rows
extracted before ``original_value`` existed. Synthetic data only.
"""

from __future__ import annotations

import json

import pytest

from server import clinical_evidence, companion_evidence, db, health_summary, lab_censored_backfill, records, report
from server.clinician_briefs import _display
from server.lab_audit import censored, display_value, effective_flag, range_flag
from server.migrations import run_migrations

pytestmark = pytest.mark.risk_critical
OWNER = "owner@glucopilot.local"


@pytest.fixture
def lab_db(tmp_path, monkeypatch):
    database = tmp_path / "app.sqlite3"
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setenv("CANONICAL_TIME_ENABLED", "false")
    return database


def _row(entity_id):
    return next(row for row in db.query_entities("LabResult") if row["id"] == entity_id)


def _ganglionic(**extra):
    return {
        "test_name": "Synthetic Ganglionic AChR Ab", "value": 55, "original_value": "<55",
        "unit": "pmol/L", "collected_date": "2026-06-05", "category": "Synthetic",
        "owner_email": OWNER, **extra,
    }


# ── reading the printed value ─────────────────────────────────────────────


@pytest.mark.parametrize(("lab", "expected"), [
    ({"value": 55, "original_value": "<55"}, ("<", 55.0)),
    ({"value": 0.3, "original_value": "<0.30"}, ("<", 0.3)),
    ({"value": 0.2, "original_value": "< 0.2"}, ("<", 0.2)),
    ({"value": 60, "original_value": ">=60"}, ("≥", 60.0)),
    ({"value": 60, "original_value": "≥ 60"}, ("≥", 60.0)),
    ({"value": 1.0, "original_value": "<1.0 NEG"}, ("<", 1.0)),
    ({"value": None, "value_text": "<5"}, ("<", 5.0)),
    ({"value": 101, "original_value": "101 H"}, None),
    ({"value": 5, "original_value": "1:5"}, None),
    ({"value": "negative", "original_value": "NEGATIVE"}, None),
    ({"value": 55}, None),
])
def test_censored_reads_the_sign_the_lab_printed(lab, expected):
    assert censored(lab) == expected


def test_display_value_keeps_the_sign_and_leaves_plain_results_alone():
    assert display_value({"value": 55, "original_value": "<55"}) == "<55"
    assert display_value({"value": 0.3, "original_value": "<0.30"}) == "<0.3"
    assert display_value({"value": 6.8, "original_value": "6.8"}) == 6.8
    assert display_value({"value": 6.8}) == 6.8


@pytest.mark.parametrize(("row", "expected"), [
    # "<55" against a "<55" cutoff is negative, not high and not borderline.
    ({"value": 55, "original_value": "<55", "reference_range": "<55 pmol/L"}, ("normal", None)),
    ({"value": 55, "original_value": "<55", "reference_low": 0, "reference_high": 55, "flag": "normal"}, ("normal", None)),
    ({"value": 55, "original_value": "<55", "reference_range": "<55", "flag": "high"}, ("normal", "high")),
    # A "<x" over the upper limit is undecided: the stored flag stands.
    ({"value": 80, "original_value": "<80", "reference_high": 55, "flag": "high"}, ("high", None)),
    ({"value": 55, "original_value": "≤55", "reference_range": "<55", "flag": ""}, ("", None)),
    # Older extractions stored a "<0.2" limit as a lower bound: never "low".
    ({"value": 0.2, "original_value": "< 0.2", "reference_low": 0.2, "flag": "normal"}, ("normal", None)),
    ({"value": 0.9, "original_value": "<0.90", "reference_low": 0.9, "reference_high": 1.09, "flag": "normal"}, ("normal", None)),
    # Above-limit results.
    ({"value": 100, "original_value": ">100", "reference_low": 0.4, "reference_high": 4.5, "flag": ""}, ("high", None)),
    ({"value": 60, "original_value": ">60", "reference_range": ">59", "flag": ""}, ("normal", None)),
    ({"value": 3, "original_value": ">3", "reference_low": 0.4, "reference_high": 4.5, "flag": "normal"}, ("normal", None)),
    # A plain 30 against "<30" is still high.
    ({"value": 30, "reference_range": "< 30", "flag": ""}, ("high", None)),
])
def test_effective_flag_places_a_below_limit_result_by_its_limit(row, expected):
    assert effective_flag(row) == expected


def test_range_flag_without_a_range_is_none_for_a_below_limit_result():
    assert range_flag({"value": 4, "original_value": "<4"}) is None


# ── readers ──────────────────────────────────────────────────────────────


def test_companion_and_clinical_context_show_the_printed_value():
    item = {"entity_type": "LabResult", "data": _ganglionic(reference_low=0, reference_high=55, flag="normal")}

    for reasoning in (companion_evidence._reasoning_data(item), clinical_evidence._reasoning_data(item)):
        assert reasoning["value"] == "<55"
        assert "below the assay's reporting limit, not a measured 55" in reasoning["value_note"]
        assert reasoning["reference_high"] == 55

    plain = {"entity_type": "LabResult", "data": {"test_name": "Synthetic TSH", "value": 2.1, "original_value": "2.1"}}
    assert companion_evidence._reasoning_data(plain)["value"] == 2.1
    assert "value_note" not in companion_evidence._reasoning_data(plain)


def test_clinician_brief_prints_the_sign():
    shown = _display({"entity_type": "LabResult", "data": _ganglionic(reference_high=55)})
    assert shown["value"] == "<55"
    assert shown["flag"].lower() == "normal"


def test_visit_report_shows_below_limit_results_without_a_trend(lab_db):
    db.create_entity("LabResult", _ganglionic(collected_date="2026-03-01"))
    db.create_entity("LabResult", _ganglionic(reference_range="<55 pmol/L"))
    db.create_entity("LabResult", {
        "test_name": "Hemoglobin A1c", "value": 4.0, "original_value": "<4.0", "unit": "%",
        "collected_date": "2026-05-01", "category": "Synthetic", "owner_email": OWNER,
    })
    db.create_entity("LabResult", {
        "test_name": "Hemoglobin A1c", "value": 5.1, "original_value": "5.1", "unit": "%",
        "collected_date": "2026-08-01", "category": "Synthetic", "owner_email": OWNER,
    })

    labs = report._labs()
    tests = {entry["test_name"]: entry for entries in labs["categories"].values() for entry in entries}

    entry = tests["Synthetic Ganglionic AChR Ab"]
    assert (entry["value"], entry["value_display"], entry["flag"], entry["trend"]) == (55, "<55", "normal", None)
    assert [point["value_display"] for point in entry["history"]] == ["<55", "<55"]
    assert entry not in labs["flagged"]
    # A below-limit A1c has no number to compare with GMI.
    assert report._a1c_points(labs) == [{"date": "2026-08-01", "value": 5.1}]


def test_health_overview_reports_the_sign_and_skips_its_trend(lab_db):
    for day, value in (("2026-01-01", "<11"), ("2026-03-01", "<11"), ("2026-06-01", "<11")):
        db.create_entity("LabResult", {
            "test_name": "Synthetic Neural Ab", "value": 11, "original_value": value, "flag": "high",
            "collected_date": day, "owner_email": OWNER,
        })

    flagged, trends = health_summary._labs_snapshot()

    assert flagged[0]["value"] == "<11"
    assert trends == []


def test_record_summary_note_places_below_limit_results_by_their_limit():
    labs = [
        {"test_name": "Synthetic Ab A", "value": 55, "original_value": "<55", "reference_low": 0, "reference_high": 55},
        # An old "<14" range stored as a lower bound cannot make "<10" low.
        {"test_name": "Synthetic Ab B", "value": 10, "original_value": "<10", "reference_low": 14},
        {"test_name": "Synthetic Ab C", "value": 100, "original_value": ">100", "reference_low": 0, "reference_high": 4.5},
    ]

    note, out_count, evaluated = records._computed_range_note(labs)

    assert (out_count, evaluated) == (1, 2)
    assert "Synthetic Ab C >100" in note
    assert "Synthetic Ab A" not in note


# ── one-time repair of older rows ─────────────────────────────────────────

QUEST_LIKE = """
   VGCC TYPE P/Q AB                                             <30     Reference Range: <30 pmol/L
   ACETYLCHOLINE RECEPTOR GANGLIONIC (ALPHA                     <55     Reference Range: <55 pmol/L
   3) AB
   ACETYLCHOLINE RECEPTOR BINDING ANTIBODY                     <0.30     nmol/L
   VITAMIN D, 25-OH, TOTAL                                        27 L  Reference Range: 30-100 ng/mL
   RHEUMATOID FACTOR                                             <10   Reference Range: <14 IU/mL
   ANNA1 (HU) AB, LB                                              <11   Reference Range: <11
   ANNA1 (HU) AB, IFA                                       NEGATIVE    Reference Range: NEGATIVE
   VOLTAGE GATED POTASSIUM CHANNEL (VGKC)                IN PROGRESS
   Glycemic control for adults with diabetes: <7.0
"""


@pytest.mark.parametrize(("name", "value", "expected"), [
    ("Acetylcholine Receptor Ganglionic (Alpha 3) AB", 55, "<55"),
    ("VGCC Type P/Q AB", 30, "<30"),
    ("Acetylcholine Receptor Binding Antibody", 0.3, "<0.30"),
    ("Rheumatoid Factor", 10, "<10"),
    ("ANNA1 (HU) AB, LB", 11, "<11"),
    # Printed plainly: nothing to restore.
    ("Vitamin D, 25-OH, Total", 27, None),
    # The number on the matching line differs.
    ("Acetylcholine Receptor Ganglionic (Alpha 3) AB", 54, None),
    # A different test's "<7.0" in prose is not this result.
    ("ALT (SGPT)", 7, None),
    # A qualitative result stored as 0 is not repaired here.
    ("ANNA1 (HU) AB, IFA", 0, None),
])
def test_printed_value_finds_the_sign_on_the_tests_own_line(name, value, expected):
    assert lab_censored_backfill.printed_value(QUEST_LIKE, name, value) == expected


@pytest.mark.parametrize(("name", "expected"), [
    ("Voltage Gated Potassium Channel (VGKC) AB", "IN PROGRESS"),
    # A qualitative result is a result: not this repair's business.
    ("ANNA1 (HU) AB, IFA", None),
    ("VGCC Type P/Q AB", None),
])
def test_printed_status_finds_a_pending_placeholder(name, expected):
    assert lab_censored_backfill.printed_status(QUEST_LIKE, name) == expected


def test_printed_value_leaves_a_result_printed_both_ways():
    text = "INSULIN AUTOANTIBODY     <0.4\nINSULIN AUTOANTIBODY     0.4\n"
    assert lab_censored_backfill.printed_value(text, "Insulin Autoantibody", 0.4) is None


def test_backfill_restores_signs_once_and_leaves_other_rows(lab_db, tmp_path, monkeypatch):
    records_dir = tmp_path / "records"
    records_dir.mkdir()
    (records_dir / "synthetic.pdf").write_bytes(b"%PDF-1.4 synthetic")
    (records_dir / "photo.png").write_bytes(b"png")
    monkeypatch.setattr(lab_censored_backfill, "RECORDS_DIR", records_dir)
    monkeypatch.setattr(lab_censored_backfill, "_pdf_text", lambda path: QUEST_LIKE)
    backups = []
    monkeypatch.setattr(
        "server.backup.create_verified_backup",
        lambda *args, **kwargs: (backups.append(kwargs.get("reason")) or tmp_path / "backup-1", {}),
    )

    pdf = db.create_entity("MedicalRecord", {"stored_as": "synthetic.pdf", "owner_email": OWNER})
    photo = db.create_entity("MedicalRecord", {"stored_as": "photo.png", "owner_email": OWNER})

    def lab(record, name, value, **extra):
        return db.create_entity("LabResult", {
            "test_name": name, "value": value, "record_id": record["id"], "owner_email": OWNER, **extra,
        })

    ganglionic = lab(pdf, "Acetylcholine Receptor Ganglionic (Alpha 3) AB", 55, reference_low=0, reference_high=55)
    vitamin_d = lab(pdf, "Vitamin D, 25-OH, Total", 27)
    reviewed = lab(pdf, "VGCC Type P/Q AB", 30, verification_status="approved")
    current = lab(pdf, "Rheumatoid Factor", 10, original_value="10")
    from_photo = lab(photo, "Insulin Autoantibody", 0.4)
    pending = lab(pdf, "Voltage Gated Potassium Channel (VGKC) AB", 0.0, unit="pmol/L", collected_date="2026-06-05")
    negative = lab(pdf, "ANNA1 (HU) AB, IFA", 0.0)

    summary = lab_censored_backfill.ensure_backfilled()

    assert summary["restored"] == 2
    assert backups == ["pre-lab-censored-backfill"]
    fixed = _row(ganglionic["id"])
    assert (fixed["value"], fixed["original_value"], fixed["original_value_source"]) == (
        55, "<55", lab_censored_backfill.SOURCE,
    )
    assert display_value(fixed) == "<55"
    placeholder = _row(pending["id"])
    assert (placeholder["value"], placeholder["original_value"], placeholder["validation_status"]) == (
        0.0, "IN PROGRESS", "invalid",
    )
    reasoning = companion_evidence._reasoning_data({"entity_type": "LabResult", "data": placeholder})
    assert reasoning["value"] == "IN PROGRESS"
    assert "no result was reported" in reasoning["value_note"]
    tests = [entry["test_name"] for entries in report._labs()["categories"].values() for entry in entries]
    assert "Voltage Gated Potassium Channel (VGKC) AB" not in tests
    for untouched in (vitamin_d, reviewed, from_photo, negative):
        assert "original_value" not in _row(untouched["id"])
    assert _row(current["id"])["original_value"] == "10"
    assert json.loads(db.get_setting(lab_censored_backfill.SETTING_KEY))["restored"] == 2

    # Runs once per database.
    assert lab_censored_backfill.ensure_backfilled() is None
    assert backups == ["pre-lab-censored-backfill"]


def test_backfill_with_nothing_to_restore_takes_no_backup(lab_db, tmp_path, monkeypatch):
    monkeypatch.setattr(lab_censored_backfill, "RECORDS_DIR", tmp_path)
    monkeypatch.setattr(
        "server.backup.create_verified_backup",
        lambda *args, **kwargs: pytest.fail("no backup needed"),
    )

    summary = lab_censored_backfill.ensure_backfilled()

    assert (summary["restored"], "backup" in summary) == (0, False)
