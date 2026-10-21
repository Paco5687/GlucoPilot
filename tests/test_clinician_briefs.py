"""P6 specialty relevance, PHI minimization, and language safeguards."""

import base64
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from itsdangerous import TimestampSigner

from server import clinician_briefs
from server.evidence_bundle import MAX_ITEM_BUDGET, EvidenceDomain


pytestmark = pytest.mark.risk_critical


def _item(entity_type, domain, *, item_id=None, data=None, source=True, confidence=None):
    entity_id = item_id or entity_type.lower()
    return {
        "id": f"entity:{entity_type}:{entity_id}",
        "entity_type": entity_type,
        "entity_id": entity_id,
        "domain": domain,
        "observed_at": "2026-07-01T12:00:00Z",
        "data": data or {"title": f"Synthetic {entity_type}"},
        "confidence": confidence or {"label": "medium", "score": 0.7},
        "source_links": [{
            "kind": "normalized_entity",
            "entity_type": entity_type,
            "entity_id": entity_id,
            "href": f"/api/evidence/source/{entity_type}/{entity_id}",
        }] if source else [],
        "claim": None,
    }


def _bundle(items):
    return {
        "bundle_id": "urn:synthetic:bundle",
        "bundle_version": "2.5.0",
        "data_version": {"input_hash": "sha256:" + "a" * 64},
        "query": {},
        "evidence": {
            "derived_metrics": [item for item in items if item["entity_type"] in {"Pattern", "ActivityPositionEffect", "ManagementBurdenSummary"}],
            "direct_observations": [item for item in items if item["entity_type"] not in {"Pattern", "ActivityPositionEffect", "ManagementBurdenSummary", "MedicalRecord"}],
            "documents": [item for item in items if item["entity_type"] == "MedicalRecord"],
            "relationships": [],
            "reassuring_evidence": [{"evidence_item_id": items[0]["id"], "reason": "Synthetic reassuring evidence."}] if items else [],
            "opposing_evidence": [],
        },
        "contradictions": [{
            "id": "contradiction-synthetic",
            "severity": "warning",
            "explanation": "Synthetic values disagree.",
        }],
        "missing_data_caveats": [{
            "code": "missing_domain",
            "domain": "labs",
            "message": "Synthetic source is incomplete.",
        }],
    }


@pytest.fixture
def synthetic_sources(monkeypatch):
    items = [
        _item("LabResult", "labs", data={"test_name": "Synthetic ferritin", "flag": "low"}),
        _item("LabResult", "labs", item_id="unrelated", data={"test_name": "Synthetic kidney marker"}),
        _item("MedicalRecord", "records", data={"title": "Synthetic hematology report", "doc_type": "lab_report"}),
        _item("GlucoseReading", "glucose"),
        _item("InsuranceInfo", "clinical", data={"title": "Synthetic insurance"}),
        _item(
            "Pattern",
            "analytics",
            data={"title": "Synthetic iron pattern"},
            confidence={
                "label": "low",
                "score": 0.35,
                "discovery_status": "exploratory",
            },
        ),
        _item("ManagementBurdenSummary", "analytics"),
    ]
    calls = []

    def fake_bundle(query):
        calls.append(query)
        return _bundle(items)

    monkeypatch.setattr(clinician_briefs, "build_bundle", fake_bundle)
    monkeypatch.setattr(clinician_briefs, "_source_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(clinician_briefs, "_timezone", lambda: ZoneInfo("UTC"))
    monkeypatch.setattr(
        clinician_briefs,
        "hypothesis_report",
        lambda: [{
            "id": "hypothesis-synthetic",
            "title": "Possible synthetic iron issue",
            "description": "Tentative hematology observation.",
            "suggested_verification": "Review synthetic labs.",
            "status": "under_review",
        }],
    )
    return calls


def test_hematology_omits_irrelevant_phi_and_keeps_source_links(synthetic_sources):
    brief = clinician_briefs.build_brief("hematology", 90)
    appendix_types = {item["entity_type"] for item in brief["appendix"]}

    assert appendix_types >= {"LabResult", "MedicalRecord", "Pattern", "ManagementBurdenSummary"}
    assert "GlucoseReading" not in appendix_types
    assert "InsuranceInfo" not in appendix_types
    assert not any(item["entity_id"] == "unrelated" for item in brief["appendix"])
    lab = next(item for item in brief["sections"]["labs_imaging"] if item["entity_type"] == "LabResult")
    assert lab["source_links"][0]["href"].startswith("/api/evidence/source/")
    assert brief["privacy"]["policy"] == "specialty_minimum_necessary/1.1.0"
    assert brief["privacy"]["always_omitted_entity_types"] == ["InsuranceInfo"]


def test_unconfirmed_hypothesis_and_exploratory_pattern_are_not_overstated(synthetic_sources):
    brief = clinician_briefs.build_brief("hematology", 90)

    hypothesis = brief["sections"]["hypotheses"][0]
    assert hypothesis["semantic_class"] == "unconfirmed_hypothesis_not_diagnosis"
    assert hypothesis["definitive_allowed"] is False
    assert "not a diagnosis" in hypothesis["display_label"].lower()
    pattern = brief["sections"]["objective_patterns"][0]
    assert pattern["evidence_strength"]["status"] == "exploratory"
    assert "limited evidence" in pattern["evidence_strength"]["lead"]
    assert pattern["evidence_strength"]["definitive_allowed"] is False
    assert pattern["evidence_strength"]["causal_allowed"] is False


def test_unevaluated_hypothesis_is_not_presented_as_evidence_against(
    synthetic_sources,
    monkeypatch,
):
    monkeypatch.setattr(
        clinician_briefs,
        "hypothesis_report",
        lambda: [{
            "id": "legacy-suspected:synthetic",
            "title": "Synthetic legacy question",
            "description": "Previously recorded without governed evidence.",
            "suggested_verification": "Discuss whether evaluation is warranted.",
            "status": "proposed",
            "evidence_state": "not_evaluated",
            "evidence_state_label": "Not evaluated",
            "legacy": True,
        }],
    )

    hypothesis = clinician_briefs.build_brief("clinician", 90)["sections"]["hypotheses"][0]

    assert hypothesis["semantic_class"] == "unevaluated_hypothesis_not_diagnosis"
    assert hypothesis["definitive_allowed"] is False
    assert hypothesis["display_label"].startswith("Unconfirmed — not a diagnosis.")
    assert "Not evaluated — no supporting or opposing evidence recorded" in hypothesis["display_label"]
    assert hypothesis["status_label"] == "Unconfirmed"


def test_every_specialist_mode_uses_bounded_evidence_bundle_query(synthetic_sources):
    for mode in clinician_briefs.MODE_CONFIG:
        synthetic_sources.clear()
        brief = clinician_briefs.build_brief(mode, 30)
        assert brief["mode"] == mode
        assert synthetic_sources
        mode_domains = set(clinician_briefs.MODE_CONFIG[mode]["domains"])
        for query in synthetic_sources:
            assert 1 <= query.item_budget <= MAX_ITEM_BUDGET
            assert set(query.domains) <= mode_domains
            assert all(isinstance(domain, EvidenceDomain) for domain in query.domains)
            assert query.normalized_entity_types
            assert not (
                set(query.normalized_entity_types)
                & (clinician_briefs.HIGH_FREQUENCY_RAW_TYPES | {"Treatment"})
            )
        # The standing record has its own query, so it never competes for budget.
        standing = [
            query for query in synthetic_sources
            if "Diagnosis" in query.normalized_entity_types
        ]
        assert len(standing) == 1
        assert set(standing[0].normalized_entity_types) <= {
            "Allergy", "Diagnosis", "HealthProfile", "Medication",
        }


def _session(role):
    payload = base64.b64encode(json.dumps({
        "logged_in": True,
        "role": role,
        "provider_name": "Synthetic Provider",
    }).encode())
    return TimestampSigner("test-secret-key").sign(payload).decode()


def test_provider_can_generate_read_only_brief(client, monkeypatch):
    monkeypatch.setattr(
        clinician_briefs,
        "build_brief",
        lambda mode, days: {
            "mode": mode,
            "window": {"days": days},
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    client.cookies.set("session", _session("provider"))
    response = client.post(
        "/api/briefs/clinician",
        json={"mode": "endocrinology", "days": 90},
    )
    client.cookies.clear()

    assert response.status_code == 200
    assert response.json()["mode"] == "endocrinology"


def test_brief_reports_bounded_source_scan_failure_as_413(client, monkeypatch):
    def rejected_brief(mode, days):
        raise clinician_briefs.EvidenceBundleError(
            "query matches more than the bounded source-row limit"
        )

    monkeypatch.setattr(clinician_briefs, "build_brief", rejected_brief)
    client.cookies.set("session", _session("admin"))
    response = client.post(
        "/api/briefs/clinician",
        json={"mode": "clinician", "days": 90},
    )
    client.cookies.clear()

    assert response.status_code == 413
    assert "bounded source-row limit" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Integration: the real Evidence Bundle over a synthetic patient


@pytest.fixture
def patient(tmp_path, monkeypatch):
    from server import db
    from server.evidence_bundle import clear_bundle_cache
    from server.migrations import run_migrations

    import synthetic_brief_patient

    database = tmp_path / "data" / "app.sqlite3"
    database.parent.mkdir()
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setenv("RELATIONSHIP_READS_ENABLED", "false")
    monkeypatch.setenv("EVIDENCE_SET_READS_ENABLED", "false")
    monkeypatch.setattr(clinician_briefs, "_timezone", lambda: ZoneInfo("UTC"))
    clear_bundle_cache()
    ids = synthetic_brief_patient.seed(synthetic_brief_patient.window_end())
    yield ids, synthetic_brief_patient.window_end()
    clear_bundle_cache()


def _labels(items):
    return [item["display"]["label"] for item in items]


def _printed(brief):
    """Everything a clinician sees, excluding the policy text that names what is omitted."""
    return json.dumps({"sections": brief["sections"], "appendix": brief["appendix"]}, default=str)


def test_core_records_are_always_included_alongside_raw_row_volume(patient):
    ids, end = patient
    brief = clinician_briefs.build_brief("clinician", 90, end=end)
    sections = brief["sections"]

    assert _labels(sections["problems"]) == ["Autoimmune thyroiditis", "Type 1 diabetes"]
    assert _labels(sections["medications"]) == ["Insulin aspart (pump)", "Levothyroxine", "Vitamin D3"]
    assert sections["medications"][-1]["display"]["status"] == "supplement"
    assert _labels(sections["allergies"]) == ["Penicillin"]
    assert sections["weight"][0]["entity_type"] == "WeightTrend"

    # 40 tests drawn three times: one row per test, the most recent draw.
    labs = sections["labs"]
    assert len(labs) == 40
    newest = max(lab["display"]["date"] for lab in labs)
    assert {lab["display"]["date"] for lab in labs} == {newest}
    assert all(lab["data"]["result_count"] == 3 for lab in labs)

    # Raw pump and CGM rows are summarized, never listed.
    listed = {item["entity_type"] for item in brief["appendix"]}
    assert not listed & {"Treatment", "InsulinResponseEvent", "GlucoseReading", "OuraDaily"}
    assert [item["entity_type"] for item in sections["glucose_insulin"]] == ["GlucoseSummary", "InsulinSummary"]
    insulin = sections["glucose_insulin"][1]["data"]
    assert insulin["boluses_per_day"] == 3.0
    assert insulin["carbs_g_per_day"] == 160
    assert insulin["automated_mode_pct"] == 75
    assert insulin["manual_temp_basals"] == 30


def test_concerns_route_abnormal_labs_weight_loss_lows_and_reported_symptoms(patient):
    ids, end = patient
    concerns = clinician_briefs.build_brief("clinician", 90, end=end)["sections"]["concerns"]
    by_kind = {}
    for concern in concerns:
        by_kind.setdefault(concern["concern"]["kind"], []).append(concern)

    assert set(by_kind) == {"abnormal_lab", "weight_loss", "hypoglycemia", "reported_symptoms"}
    assert sorted(_labels(by_kind["abnormal_lab"])) == ["Hemoglobin A1c", "TSH"]
    tsh = next(item for item in by_kind["abnormal_lab"] if item["display"]["label"] == "TSH")
    assert "TSH 6.2 mIU/L — High, reference 0.4–4.5" in tsh["concern"]["reason"]
    weight = by_kind["weight_loss"][0]
    assert weight["data"]["loss_from_peak_kg"] == 18.6
    assert "18.6 kg (41 lb)" in weight["concern"]["reason"]
    lows = by_kind["hypoglycemia"][0]
    assert lows["data"]["tbr70_pct"] > clinician_briefs.TBR70_TARGET_PCT
    assert lows["data"]["episodes"] == 10
    assert "below 54" in lows["concern"]["reason"]
    symptoms = by_kind["reported_symptoms"][0]
    assert symptoms["entity_id"] == ids["symptoms"]
    assert "palpitations" in symptoms["concern"]["reason"]


def test_every_item_prints_a_readable_label_and_its_values(patient):
    _, end = patient
    brief = clinician_briefs.build_brief("clinician", 90, end=end)

    tsh = next(lab for lab in brief["sections"]["labs"] if lab["display"]["label"] == "TSH")
    assert tsh["display"].items() >= {
        "value": "6.2", "unit": "mIU/L", "reference": "0.4–4.5", "flag": "High",
    }.items()
    assert tsh["display"]["date"]
    a1c = next(lab for lab in brief["sections"]["labs"] if lab["display"]["label"] == "Hemoglobin A1c")
    assert a1c["display"]["status"] == "Unverified extraction"
    raw_names = {"Treatment", "InsulinResponseEvent", "LabResult", "MedicalRecord", "Insight", "Pattern"}
    for item in brief["appendix"]:
        assert item["display"]["label"] not in raw_names
    summary = brief["sections"]["glucose_insulin"][0]["display"]
    assert summary["value"].startswith("In range ")
    assert "low episodes" in summary["detail"]
    burden = brief["sections"]["management_burden"][0]["display"]
    assert burden["label"] == "Management burden"
    assert "min/day of active management" in burden["detail"]
    # Abnormal results lead the lab table.
    assert [lab["display"]["flag"] for lab in brief["sections"]["labs"][:2]] == ["High", "High"]


def test_insurance_and_non_clinical_documents_are_excluded(patient):
    ids, end = patient
    brief = clinician_briefs.build_brief("clinician", 90, end=end)

    documents = {item["entity_id"] for item in brief["sections"]["documents"]}
    assert documents == {ids["lab_record"], ids["imaging"]}
    assert brief["privacy"]["omitted_documents"] == {"insurance": 2, "non_clinical": 2}
    printed = _printed(brief).lower()
    for text in ("insurance", "member id", "rx bin", "bible", "supplement protocol"):
        assert text not in printed
    assert "insurance documents" in brief["privacy"]["note"].lower()


def test_patterns_are_deduplicated_in_clinician_voice_without_bad_spo2(patient):
    _, end = patient
    brief = clinician_briefs.build_brief("clinician", 90, end=end)
    patterns = brief["sections"]["objective_patterns"]

    assert _labels(patterns).count("Sleep score lines up with time in range") == 1
    assert _labels(patterns).count("Mornings run high") == 1
    for item in patterns:
        text = f"{item['display']['label']} {item['display']['detail']}"
        assert not re.search(r"\byou(r)?\b", text, re.IGNORECASE)
        assert item["data"].get("is_active") is not False
    assert not any("spo2" in label.lower() for label in _labels(patterns))
    codes = {limitation["code"] for limitation in brief["sections"]["limitations"]}
    assert "spo2_device_error" in codes
    wearables = brief["sections"]["wearables_cycle"][0]["data"]
    assert wearables["spo2_implausible_days"] == 10
    assert wearables["spo2_pct"] == 96.0


def test_unconfirmed_conditions_are_labelled_and_left_out_of_primary_care(patient):
    _, end = patient
    clinician = clinician_briefs.build_brief("clinician", 90, end=end)
    titles = [item["title"] for item in clinician["sections"]["hypotheses"]]

    assert "Polycystic ovary syndrome" in titles
    assert "Suspected Graves disease" in titles
    assert "Suspected Graves disease" not in _labels(clinician["sections"]["problems"])
    for hypothesis in clinician["sections"]["hypotheses"]:
        assert hypothesis["display_label"].startswith("Unconfirmed — not a diagnosis")
        assert hypothesis["definitive_allowed"] is False

    primary = clinician_briefs.build_brief("primary_care", 90, end=end)
    assert primary["sections"]["hypotheses"] == []
    assert "Suspected Graves disease" not in _printed(primary)
    assert "unconfirmed_conditions_omitted" in {
        limitation["code"] for limitation in primary["sections"]["limitations"]
    }


def test_specialty_mode_keeps_core_records_and_filters_labs(patient):
    _, end = patient
    brief = clinician_briefs.build_brief("endocrinology", 90, end=end)

    assert _labels(brief["sections"]["problems"]) == ["Autoimmune thyroiditis", "Type 1 diabetes"]
    assert _labels(brief["sections"]["allergies"]) == ["Penicillin"]
    assert sorted(_labels(brief["sections"]["labs"])) == ["Hemoglobin A1c", "TSH"]


@pytest.mark.parametrize(("text", "headline", "expected"), [
    ("Your sleep score lines up with your time in range", True, "Sleep score lines up with time in range"),
    ("On nights you sleep better, your next-day time in range is higher.", False,
     "On nights they sleep better, their next-day time in range is higher."),
    ("Lower SpO2 days line up with higher glucose for you.", False,
     "Lower SpO2 days line up with higher glucose for them."),
    ("You're running high after dinner; worth discussing with your care team.", False,
     "They're running high after dinner; worth discussing with their care team."),
])
def test_patient_facing_wording_is_rewritten_for_a_clinician(text, headline, expected):
    assert clinician_briefs.clinician_voice(text, headline=headline) == expected


@pytest.mark.parametrize(("data", "reason"), [
    ({"doc_type": "other", "title": "Insurance card front"}, "insurance"),
    ({"doc_type": "insurance_card", "title": "Card scan"}, "insurance"),
    ({"doc_type": "visit_summary", "title": "Scan", "summary": "Member ID 123, group number 9"}, "insurance"),
    ({"doc_type": "other", "title": "Favorite verses"}, "non_clinical"),
    ({"doc_type": "", "title": "Unprocessed upload"}, "non_clinical"),
    ({"doc_type": "visit_summary", "title": "Daily supplement protocol"}, "non_clinical"),
    ({"doc_type": "lab_report", "title": "Vitamin panel", "summary": "Supplement regimen review"}, None),
    ({"doc_type": "imaging_report", "title": "Thyroid ultrasound"}, None),
])
def test_document_exclusion_policy(data, reason):
    assert clinician_briefs.document_exclusion(data) == reason
