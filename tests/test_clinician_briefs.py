"""P6 specialty relevance, PHI minimization, and language safeguards."""

import base64
import json
from datetime import datetime, timezone

import pytest
from itsdangerous import TimestampSigner

from server import clinician_briefs
from server.evidence_bundle import EvidenceDomain


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
        _item("MedicalRecord", "records", data={"title": "Synthetic hematology report"}),
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
    assert brief["privacy"]["policy"] == "specialty_minimum_necessary/1.0.0"
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


def test_every_specialist_mode_uses_bounded_evidence_bundle_query(synthetic_sources):
    for mode in clinician_briefs.MODE_CONFIG:
        brief = clinician_briefs.build_brief(mode, 30)
        assert brief["mode"] == mode
        query = synthetic_sources[-1]
        assert 1 <= query.item_budget <= 150
        assert set(query.domains) == set(clinician_briefs.MODE_CONFIG[mode]["domains"])
        assert all(isinstance(domain, EvidenceDomain) for domain in query.domains)
        assert query.normalized_entity_types
        assert not (
            set(query.normalized_entity_types)
            & clinician_briefs.HIGH_FREQUENCY_RAW_TYPES
        )


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
