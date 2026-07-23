from __future__ import annotations

import asyncio
from datetime import date

import pytest

from server import clinical_evidence, db, health_summary, report
from server.contradictions import SqliteContradictionRepository
from server.evidence_bundle import clear_bundle_cache
from server.migrations import run_migrations


pytestmark = pytest.mark.risk_critical
OWNER = "owner@glucopilot.local"


@pytest.fixture
def evidence_database(tmp_path, monkeypatch):
    database = tmp_path / "data" / "app.sqlite3"
    database.parent.mkdir()
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(
        clinical_evidence,
        "refresh_contradictions",
        lambda: {"enabled": True, "refreshed": False},
    )
    monkeypatch.setenv("RELATIONSHIP_READS_ENABLED", "false")
    monkeypatch.setenv("EVIDENCE_SET_READS_ENABLED", "false")
    clear_bundle_cache()

    record = db.create_entity(
        "MedicalRecord",
        {
            "owner_email": OWNER,
            "record_date": "2026-07-10",
            "title": "Synthetic source report",
            "filename": "source.pdf",
        },
    )
    lab = db.create_entity(
        "LabResult",
        {
            "owner_email": OWNER,
            "record_id": record["id"],
            "collected_date": "2026-07-10",
            "test_name": "Synthetic A1c",
            "value": 6.8,
            "unit": "%",
            "flag": "high",
            "parser_confidence": 0.99,
            "verification_status": "unverified",
            "validation_status": "valid",
            "source_page": 2,
        },
    )
    insurance = db.create_entity(
        "InsuranceInfo",
        {
            "owner_email": OWNER,
            "carrier": "Synthetic carrier",
            "member_id": "SYNTHETIC-MEMBER-ID-NOT-FOR-NARRATIVE",
        },
    )
    SqliteContradictionRepository().reconcile(
        [{
            "detection_key": "sha256:" + "a" * 64,
            "rule_id": "labs.synthetic_shared_context",
            "rule_version": "clinical-contradictions/1.0.0",
            "domain": "labs",
            "subject_type": "LabResult",
            "subject_key": lab["id"],
            "severity": "blocking",
            "explanation": "Synthetic lab sources disagree.",
            "left": {"label": "Source A", "value": 6.8},
            "right": {"label": "Source B", "value": 7.2},
            "context": {"synthetic": True},
        }],
        "sha256:" + "b" * 64,
    )
    yield {"record": record, "lab": lab, "insurance": insurance}
    clear_bundle_cache()


def test_shared_context_preserves_quality_conflicts_sources_and_data_through(
    evidence_database,
):
    quality = {
        "cgm": {
            "version": "data-quality/1.0.0",
            "ai_eligible": True,
            "coverage_status": "complete",
            "freshness_status": "current",
            "data_through": "2026-07-20",
            "limitations": [],
        }
    }
    public, reasoning = clinical_evidence.build_context(
        90,
        data_quality=quality,
        as_of=date(2026, 7, 20),
    )

    assert public["contract_version"] == "clinical-evidence-context/1.0.0"
    assert public["bundle"]["version"] == "2.0.0"
    assert [bundle["scope"] for bundle in public["bundles"]] == [
        "claims", "clinical", "labs_records"
    ]
    assert sum(bundle["budget"]["item_limit"] for bundle in public["bundles"]) == 250
    assert reasoning["bundle_id"] == public["bundle"]["id"]
    assert public["data_quality"] == [{
        "domain": "cgm",
        "version": "data-quality/1.0.0",
        "ai_eligible": True,
        "coverage_status": "complete",
        "freshness_status": "current",
        "data_through": "2026-07-20",
        "limitations": [],
    }]
    assert {item["domain"]: item["through"] for item in public["data_through"]}["cgm"] == (
        "2026-07-20"
    )
    assert public["contradictions"][0]["left"]["value"] == 6.8
    assert public["contradictions"][0]["right"]["value"] == 7.2
    assert public["sources"]["links"]

    lab_item = next(
        item for item in public["evidence_items"]
        if item["entity_id"] == evidence_database["lab"]["id"]
    )
    assert lab_item["confidence"]["label"] == "unverified"
    assert lab_item["confidence"]["score"] == 0.99
    assert lab_item["confidence"]["clinically_verified"] is False
    assert "not been approved" in lab_item["confidence"]["limitations"][0]
    encoded = str({"public": public, "reasoning": reasoning})
    assert evidence_database["insurance"]["id"] not in encoded
    assert "SYNTHETIC-MEMBER-ID-NOT-FOR-NARRATIVE" not in encoded


def test_generated_narrative_can_only_link_selected_bundle_evidence(evidence_database):
    public, reasoning = clinical_evidence.build_context(
        90,
        data_quality={},
        as_of=date(2026, 7, 20),
    )
    selected = next(
        item for item in reasoning["items"]
        if item["entity_id"] == evidence_database["lab"]["id"]
    )
    linked = clinical_evidence.link_generated_narrative(
        {
            "headline": "Synthetic evidence-linked narrative",
            "evidence_item_ids": [selected["id"], "invented:evidence:id"],
        },
        reasoning,
    )

    assert linked["evidence_item_ids"] == [selected["id"]]
    assert linked["evidence_bundle_id"] == public["bundle"]["id"]
    assert linked["evidence_links"]
    assert all(link["href"].startswith("/api/") for link in linked["evidence_links"])


def test_overview_and_visit_report_use_the_same_shared_context_adapter(
    evidence_database, monkeypatch
):
    public = {
        "contract_version": clinical_evidence.CONTEXT_VERSION,
        "bundle": {"id": "urn:synthetic:shared-bundle", "version": "2.0.0"},
        "data_quality": [],
        "data_through": [],
        "contradictions": [{"id": "contr_shared", "severity": "blocking"}],
        "sources": {"links": [], "returned": 0, "truncated": False},
        "claims": [],
        "evidence_items": [],
        "missing_data_caveats": [],
    }
    reasoning = {
        "bundle_id": "urn:synthetic:shared-bundle",
        "items": [],
        "contradictions": public["contradictions"],
    }
    calls = []

    def shared(days, *, data_quality, as_of):
        calls.append({"days": days, "data_quality": data_quality, "as_of": as_of})
        return public, reasoning

    monkeypatch.setattr(health_summary, "build_clinical_evidence", shared)
    monkeypatch.setattr(report, "build_clinical_evidence", shared)
    monkeypatch.setattr(
        report,
        "_glucose",
        lambda *_args: {"available": False, "days": 0, "quality": {"ai_eligible": False}},
    )
    monkeypatch.setattr(
        report,
        "_cycle",
        lambda *_args: {"available": False, "quality": {"ai_eligible": False}},
    )
    monkeypatch.setattr(
        report,
        "_insulin",
        lambda *_args: {
            "available": False,
            "quality": {"ai_eligible": False},
            "nutrition_quality": {"ai_eligible": False},
        },
    )
    monkeypatch.setattr(
        report,
        "_wellness",
        lambda *_args: {
            "oura": None,
            "fitbit": None,
            "quality": {"oura": None, "fitbit": None},
        },
    )
    monkeypatch.setattr(report, "_labs", lambda: {"available": False, "flagged": []})
    monkeypatch.setattr(
        health_summary,
        "_wearable_trends",
        lambda: {"metrics": {}, "quality": {"ai_eligible": False}},
    )
    monkeypatch.setattr(health_summary, "_labs_snapshot", lambda: ([], []))
    monkeypatch.setattr(health_summary.profile, "get_profile", lambda: {})

    async def narrative(_payload):
        return None

    monkeypatch.setattr(report, "_narrative", narrative)

    overview = health_summary._build_context()
    visit = asyncio.run(report.visit_report(report.ReportBody(days=90)))

    assert overview["evidence_context"] is public
    assert visit["evidence_context"] is public
    assert visit["contradictions"] == {
        "unresolved": public["contradictions"],
        "counts": {"unresolved": 1, "blocking": 1},
    }
    assert visit["glucose"]["fingerstick_reconciliation"]["paired"] == 0
    assert "separate observations" in visit["glucose"]["fingerstick_reconciliation"]["semantics"]
    assert [call["days"] for call in calls] == [90, 90]
