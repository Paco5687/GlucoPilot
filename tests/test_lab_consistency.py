"""The Visit Report, Clinician Brief and Companion read a lab result the same way.

They share one synonym table (``lab_names``) for which results are the same
test, and one rule (``lab_audit.effective_flag``) for the flag a value and its
reference range support. Synthetic data only.
"""

from __future__ import annotations

import pytest

from server import lab_names
from server.clinician_briefs import _display
from server.evidence_bundle import _explicit_stance
from server.lab_names import _LAB_SYNONYMS, analyte_key, canonical_lab_name
from server.report import _lab_flag as report_flag

pytestmark = pytest.mark.risk_critical


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
        ("TgAb", "Thyroglobulin Antibodies"),
        ("T3 Reverse, LC/MS/MS", "Reverse T3 (RT3)"),
        ("Total WBCs", "WBC"),
        ("Hemoglobin, Female", "Hemoglobin"),
        ("Vitamin D (25-OH)", "Vitamin D, 25-Hydroxy"),
        ("Cholesterol/HDL Ratio", "T. Chol/HDL Ratio"),
    ],
)
def test_lab_spellings_of_one_analyte_match(first, second):
    assert analyte_key(first) == analyte_key(second)


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
    assert analyte_key(first) != analyte_key(second)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("TSH", "Thyroid Stimulating Hormone"),
        ("Vitamin D, 25-OH", "25-Hydroxyvitamin D"),
        ("Prolactin", "PRL"),
        ("eGFR", "Estimated GFR"),
        ("HbA1c", "Hemoglobin A1c, Total"),
    ],
)
def test_report_synonyms_are_one_analyte_for_the_companion_too(first, second):
    assert analyte_key(first) == analyte_key(second)


def test_every_listed_synonym_matches_its_test_and_tests_stay_distinct():
    keys = {}
    for canonical, aliases in _LAB_SYNONYMS.items():
        key = analyte_key(canonical)
        assert all(analyte_key(alias) == key for alias in aliases), canonical
        assert canonical_lab_name(canonical) == canonical
        assert key not in keys, f"{canonical} and {keys.get(key)} share a key"
        keys[key] = canonical


def test_the_brief_and_report_name_tests_from_the_same_table():
    assert lab_names.canonical_lab_name("Thyroid peroxidase Ab") == "TPO antibodies"
    lab = {"test_name": "HbA1c", "value": 5.4, "unit": "%"}
    assert _display({"entity_type": "LabResult", "data": lab})["label"] == "Hemoglobin A1c"


def _lab(flag, value, **reference):
    return {"test_name": "Urine albumin/creatinine ratio", "value": value, "unit": "mg/g", "flag": flag, **reference}


@pytest.mark.parametrize(
    ("lab", "flag", "stance"),
    [
        # A stored flag its own range contradicts: the range decides everywhere.
        (_lab("high", 9, reference_range="<30"), "normal", "reassuring"),
        (_lab("normal", 45, reference_low=0, reference_high=30), "high", "opposing"),
        # A stored flag the range supports stands everywhere.
        (_lab("high", 45, reference_range="<30"), "high", "opposing"),
        (_lab("normal", 9, reference_range="<30"), "normal", "reassuring"),
        # No usable range: the stored flag stands.
        (_lab("high", 9), "high", "opposing"),
    ],
)
def test_report_brief_and_companion_agree_on_a_lab_flag(lab, flag, stance):
    assert report_flag(lab) == flag
    assert _display({"entity_type": "LabResult", "data": lab})["flag"].lower() == flag
    assert _explicit_stance("LabResult", lab)[0] == stance


def test_companion_says_when_the_range_overrules_the_source_flag():
    stance, reason = _explicit_stance("LabResult", _lab("high", 9, reference_range="<30"))
    assert stance == "reassuring"
    assert reason == "The value is normal against its own reference range; the source's high flag contradicts that range."
    stance, reason = _explicit_stance("LabResult", _lab("high", 45, reference_range="<30"))
    assert reason == "Source explicitly labels this result high."


def test_a_result_without_a_stored_flag_still_has_no_explicit_stance():
    assert _explicit_stance("LabResult", _lab("", 45, reference_range="<30")) == (None, None)


def test_only_lab_results_are_rejudged_against_a_range():
    other = {"value": 9, "reference_range": "<30", "flag": "high"}
    assert _explicit_stance("Observation", other) == (
        "opposing", "Source explicitly labels this result high."
    )
