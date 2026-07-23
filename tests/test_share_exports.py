"""P8 export allowlists, preview parity, role policy, and leak prevention."""

import base64
import json

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from server import db
from server.migrations import run_migrations
from server.evidence_bundle import EvidenceDomain
from server.share_exports import ENTITY_FIELDS, MODES, POLICY_VERSION, RESEARCH_FIELDS


pytestmark = pytest.mark.risk_critical
OWNER = "owner@glucopilot.local"


def _session(role):
    payload = base64.b64encode(json.dumps({
        "logged_in": True,
        "role": role,
        "provider_name": "Synthetic Provider",
    }).encode())
    return TimestampSigner("test-secret-key").sign(payload).decode()


@pytest.fixture
def export_api(tmp_path, monkeypatch):
    database = tmp_path / "data" / "app.sqlite3"
    database.parent.mkdir()
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    db.create_entity("Diagnosis", {
        "owner_email": OWNER,
        "name": "Synthetic condition",
        "status": "active",
        "notes": (
            "Contact private@example.test; token=synthetic-secret "
            "https://secret.example.test/path"
        ),
        "internal_id": "must-not-leak",
    })
    db.create_entity("Medication", {
        "owner_email": OWNER,
        "name": "Synthetic medication",
        "dose": "1 synthetic unit",
        "frequency": "daily",
        "rx_id": "rx-must-not-leak",
        "employer": "private-employer",
    })
    db.create_entity("InsuranceInfo", {
        "owner_email": OWNER,
        "member_id": "member-must-not-leak",
        "rx_bin": "bin-must-not-leak",
    })
    db.create_entity("GlucoseReading", {
        "owner_email": OWNER,
        "timestamp": "2026-07-20T12:00:00Z",
        "value": 123,
        "source": "synthetic",
        "access_token": "token-must-not-leak",
    })

    from server.main import app

    with TestClient(app) as client:
        yield client


def _preview(client, mode, role="admin"):
    client.cookies.set("session", _session(role))
    response = client.post(
        "/api/share-exports/preview",
        json={"mode": mode, "days": 90},
    )
    client.cookies.clear()
    return response


def test_every_mode_has_explicit_policy_and_private_entities_have_field_allowlists():
    assert set(MODES) == {
        "full_private", "clinician", "emergency",
        "anonymized_research", "demo",
    }
    assert all(policy["roles"] and policy["watermark"] for policy in MODES.values())
    assert set(ENTITY_FIELDS) == {"full_private", "emergency"}
    assert all(fields for policy in ENTITY_FIELDS.values() for fields in policy.values())
    assert "InsuranceInfo" not in ENTITY_FIELDS["full_private"]


@pytest.mark.parametrize("mode", sorted(MODES))
def test_preview_request_produces_byte_equivalent_download(export_api, mode):
    preview = _preview(export_api, mode)
    assert preview.status_code == 200
    body = preview.json()
    export_api.cookies.set("session", _session("admin"))
    download = export_api.post(
        "/api/share-exports/download",
        json=body["request"],
    )
    export_api.cookies.clear()

    assert download.status_code == 200
    assert download.json() == body["export"]
    assert download.headers["cache-control"] == "no-store"
    assert "attachment;" in download.headers["content-disposition"]
    assert preview.headers["cache-control"] == "no-store"
    assert body["export"]["policy"]["version"] == POLICY_VERSION
    assert body["export"]["policy"]["watermark"]
    assert body["export"]["policy"]["expires_at"]


def test_full_private_rows_contain_only_explicitly_allowed_fields(export_api):
    body = _preview(export_api, "full_private").json()["export"]
    entities = body["content"]["entities"]

    assert entities["Diagnosis"]
    assert entities["Medication"]
    assert entities["GlucoseReading"]
    for entity_type, rows in entities.items():
        for row in rows:
            assert set(row) <= set(ENTITY_FIELDS["full_private"][entity_type])
            assert not {"id", "created_date", "updated_date"} & set(row)


def test_full_private_window_limits_observations_but_keeps_longitudinal_records(
    export_api,
):
    db.create_entity("Diagnosis", {
        "owner_email": OWNER,
        "name": "Synthetic longstanding condition",
        "diagnosed_date": "2020-01-01",
        "status": "active",
    })
    db.create_entity("GlucoseReading", {
        "owner_email": OWNER,
        "timestamp": "2020-01-01T12:00:00Z",
        "value": 222,
        "source": "synthetic-old",
    })

    content = _preview(export_api, "full_private").json()["export"]["content"]

    assert any(
        row["name"] == "Synthetic longstanding condition"
        for row in content["entities"]["Diagnosis"]
    )
    assert all(
        row.get("source") != "synthetic-old"
        for row in content["entities"]["GlucoseReading"]
    )


@pytest.mark.parametrize("mode", sorted(MODES))
def test_synthetic_secrets_identifiers_and_urls_never_leak(export_api, mode):
    response = _preview(export_api, mode)
    assert response.status_code == 200
    encoded = json.dumps(response.json(), sort_keys=True).lower()
    for forbidden in (
        "private@example.test",
        "synthetic-secret",
        "secret.example.test",
        "must-not-leak",
        "private-employer",
        "owner@glucopilot.local",
        "insuranceinfo",
    ):
        assert forbidden not in encoded
    assert '"href"' not in encoded
    assert '"url"' not in encoded


def test_provider_can_export_clinician_and_emergency_only(export_api, monkeypatch):
    monkeypatch.setattr(
        "server.share_exports.build_brief",
        lambda mode, days, **kwargs: {
            "mode_label": "Synthetic clinician",
            "window": {"days": days},
            "language": {"clinical": "Synthetic safety language."},
            "privacy": {"policy": "synthetic-minimum-necessary"},
            "evidence_bundle": {"version": "synthetic-bundle/1"},
            "sections": {"questions": ["Synthetic question?"]},
        },
    )
    assert _preview(export_api, "clinician", "provider").status_code == 200
    assert _preview(export_api, "emergency", "provider").status_code == 200
    for mode in ("full_private", "anonymized_research", "demo"):
        assert _preview(export_api, mode, "provider").status_code == 403


def test_clinician_export_minimizes_nested_sections_and_internal_ids(
    export_api,
    monkeypatch,
):
    evidence = {
        "id": "private-evidence-id",
        "entity_id": "private-entity-id",
        "entity_type": "LabResult",
        "title": "Synthetic result",
        "observed_at": "2026-07-20",
        "description": "Synthetic minimum-necessary description.",
        "source_links": [{"href": "https://secret.example.test/result"}],
    }
    monkeypatch.setattr(
        "server.share_exports.build_brief",
        lambda mode, days, **kwargs: {
            "mode_label": "Synthetic clinician",
            "window": {"days": days},
            "language": {"clinical": "Synthetic safety language."},
            "privacy": {"policy": "synthetic-minimum-necessary"},
            "evidence_bundle": {
                "id": "private-bundle-id",
                "version": "synthetic-bundle/1",
            },
            "sections": {
                "concerns": [evidence],
                "reassuring_evidence": [{
                    "evidence": evidence,
                    "evidence_item_id": "private-evidence-id",
                    "reason": "Synthetic source explicitly marks this normal.",
                }],
                "opposing_evidence": [{
                    "evidence_item_id": "private-evidence-id",
                    "reason": "Synthetic opposing reason.",
                }],
                "limitations": [{
                    "code": "synthetic",
                    "message": "Synthetic limitation.",
                    "evidence_item_id": "private-evidence-id",
                }],
                "contradictions": [{
                    "id": "private-contradiction-id",
                    "rule": "synthetic_rule",
                    "severity": "warning",
                    "explanation": "Synthetic contradiction.",
                    "left": {"entity_id": "private-left-id"},
                }],
                "questions": ["Synthetic question?"],
            },
        },
    )

    content = _preview(export_api, "clinician").json()["export"]["content"]
    encoded = json.dumps(content, sort_keys=True)

    assert content["sections"]["concerns"] == [{
        "entity_type": "LabResult",
        "title": "Synthetic result",
        "observed_at": "2026-07-20",
        "description": "Synthetic minimum-necessary description.",
    }]
    assert content["sections"]["reassuring_evidence"][0]["evidence"]["title"] == (
        "Synthetic result"
    )
    assert content["evidence_bundle_version"] == "synthetic-bundle/1"
    for private in (
        "private-evidence-id",
        "private-entity-id",
        "private-bundle-id",
        "private-contradiction-id",
        "private-left-id",
        "secret.example.test",
    ):
        assert private not in encoded


def test_demo_is_synthetic_and_does_not_inherit_live_values(export_api):
    body = _preview(export_api, "demo").json()["export"]
    encoded = json.dumps(body).lower()
    assert body["content"]["synthetic"] is True
    assert "synthetic condition" not in encoded
    assert "synthetic medication" not in encoded


def test_research_export_uses_bounded_evidence_bundle_and_relative_time(
    export_api,
    monkeypatch,
):
    captured = {}

    def synthetic_bundle(query):
        captured["query"] = query
        return {
            "bundle_version": "synthetic-bundle/1",
            "confidence": {
                "label": "not_assessed",
                "score": None,
                "method": None,
                "note": "Synthetic shared confidence.",
            },
            "evidence": {
                "direct_observations": [{
                    "entity_type": "OuraDaily",
                    "entity_id": "private-internal-id",
                    "observed_at": query.start.isoformat(),
                    "data": {
                        "sleep_score": 88,
                        "email": "private@example.test",
                        "access_token": "synthetic-secret",
                    },
                    "confidence": {
                        "label": "high",
                        "score": 0.9,
                        "method": "synthetic",
                    },
                }],
                "derived_metrics": [],
            },
            "missing_data_caveats": [],
            "budget": {"truncated": False},
        }

    monkeypatch.setattr("server.share_exports.build_bundle", synthetic_bundle)
    body = _preview(export_api, "anonymized_research").json()["export"]["content"]

    query = captured["query"]
    assert query.domains == (EvidenceDomain.ANALYTICS, EvidenceDomain.WEARABLES)
    assert query.item_budget == 250
    assert set(query.normalized_entity_types) == set(RESEARCH_FIELDS)
    assert (query.end - query.start).days == 90
    assert body["bundle_version"] == "synthetic-bundle/1"
    assert body["observations"] == [{
        "observation_type": "OuraDaily",
        "day_offset": 0,
        "values": {"sleep_score": 88},
        "confidence": {
            "label": "high",
            "score": 0.9,
            "method": "synthetic",
        },
    }]
    assert "private-internal-id" not in json.dumps(body)


def test_download_rejects_a_snapshot_that_changed_after_preview(export_api):
    preview = _preview(export_api, "full_private").json()
    diagnosis = db.query_entities("Diagnosis", {"owner_email": OWNER})[0]
    db.update_entity(
        "Diagnosis",
        diagnosis["id"],
        {"notes": "Synthetic data changed after preview."},
    )

    export_api.cookies.set("session", _session("admin"))
    response = export_api.post(
        "/api/share-exports/download",
        json=preview["request"],
    )
    export_api.cookies.clear()

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "export data changed after preview; generate a new preview"
    )


def test_preview_metadata_is_server_generated_and_required_for_download(export_api):
    export_api.cookies.set("session", _session("admin"))
    forged = export_api.post(
        "/api/share-exports/preview",
        json={
            "mode": "demo",
            "days": 90,
            "generated_at": "2026-07-20T12:00:00Z",
        },
    )
    unpreviewed = export_api.post(
        "/api/share-exports/download",
        json={"mode": "demo", "days": 90},
    )
    export_api.cookies.clear()

    assert forged.status_code == 400
    assert unpreviewed.status_code == 400


def test_download_rejects_expired_or_future_preview_metadata(export_api):
    preview = _preview(export_api, "demo").json()["request"]
    export_api.cookies.set("session", _session("admin"))
    expired = export_api.post(
        "/api/share-exports/download",
        json={**preview, "generated_at": "2020-01-01T00:00:00Z"},
    )
    future = export_api.post(
        "/api/share-exports/download",
        json={**preview, "generated_at": "2100-01-01T00:00:00Z"},
    )
    export_api.cookies.clear()

    assert expired.status_code == 410
    assert future.status_code == 400


def test_export_endpoints_require_authentication(export_api):
    response = export_api.post(
        "/api/share-exports/preview",
        json={"mode": "demo", "days": 90},
    )
    assert response.status_code == 401
