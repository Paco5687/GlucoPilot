from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path

import pytest

from server import db
from server.dual_write_validation import (
    REPORT_NAMES,
    _evaluate,
    build_validation_reports,
    verify_report,
    write_validation_reports,
)
from server.migrations import run_migrations


pytestmark = pytest.mark.risk_critical
GOLDEN = Path(__file__).parent / "fixtures" / "golden"


def _without(row: dict, *keys: str) -> dict:
    return {key: value for key, value in row.items() if key not in keys}


@pytest.fixture
def validation_database(tmp_path, monkeypatch):
    database = tmp_path / "data" / "app.sqlite3"
    database.parent.mkdir()
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setenv("TYPED_TREATMENT_WRITES_ENABLED", "true")
    monkeypatch.setenv("TYPED_GLUCOSE_WRITES_ENABLED", "true")
    monkeypatch.setenv("TYPED_WEARABLE_WRITES_ENABLED", "true")

    treatments = json.loads(
        (GOLDEN / "clinical_edge_cases.json").read_text(encoding="utf-8")
    )["typed_treatments"]["rows"]
    db.bulk_create_entities("Treatment", [_without(row, "id") for row in treatments])

    glucose = json.loads(
        (GOLDEN / "typed_glucose.json").read_text(encoding="utf-8")
    )
    glucose_rows = db.bulk_create_entities(
        "GlucoseReading",
        [_without(row, "id") for row in glucose["glucose"]],
    )
    db.create_entity(
        "FingerstickReading",
        {
            **_without(glucose["fingerstick"], "id"),
            "cgm_reading_id": glucose_rows[0]["id"],
        },
    )

    wearables = json.loads(
        (GOLDEN / "typed_wearables.json").read_text(encoding="utf-8")
    )
    for row in wearables["daily"] + wearables["samples"]:
        db.create_entity(
            row["entity_type"],
            _without(row, "id", "entity_type"),
        )
    return database


def test_reports_are_value_free_checksum_protected_and_cutover_eligible(
    validation_database,
    tmp_path,
):
    reports = build_validation_reports(
        validation_database,
        generated_at="2026-07-23T00:00:00Z",
        samples=2,
    )
    assert set(reports) == set(REPORT_NAMES)
    assert all(verify_report(report) for report in reports.values())
    assert all(
        report["approval"]["decision"] == "eligible"
        and report["authority"] == {
            "legacy_reads_authoritative": True,
            "typed_reads_authoritative": False,
        }
        for report in reports.values()
    )

    encoded = json.dumps(reports, sort_keys=True)
    assert "owner@glucopilot.local" not in encoded
    assert "synthetic-treatment-" not in encoded
    assert "synthetic-glucose-" not in encoded
    assert "Synthetic comparison context" not in encoded

    output_dir = tmp_path / "private-evidence"
    summary = write_validation_reports(reports, output_dir)
    assert {item["filename"] for item in summary} == set(REPORT_NAMES.values())
    assert stat.S_IMODE(output_dir.stat().st_mode) == 0o700
    for filename in REPORT_NAMES.values():
        assert stat.S_IMODE((output_dir / filename).stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        write_validation_reports(reports, output_dir)


def test_synthetic_projection_drift_blocks_approval_and_tampering_is_detected(
    validation_database,
):
    with sqlite3.connect(validation_database) as connection:
        connection.execute(
            "UPDATE glucose_readings SET value_mg_dl=value_mg_dl+1 LIMIT 1"
        )
    reports = build_validation_reports(
        validation_database,
        generated_at="2026-07-23T00:00:00Z",
        samples=1,
    )
    glucose = reports["glucose"]
    assert glucose["approval"]["decision"] == "blocked"
    assert any(
        code.startswith("glucose:")
        for code in glucose["approval"]["failure_codes"]
    )
    assert verify_report(glucose)
    glucose["approval"]["decision"] = "eligible"
    assert not verify_report(glucose)


def test_glucose_exception_is_bounded_and_unknown_reasons_block():
    def component(total: int, unmappable: int, reasons: dict[str, int]) -> dict:
        mappable = total - unmappable
        return {
            "legacy_total": total,
            "mappable": mappable,
            "unmappable": unmappable,
            "unmappable_by_reason": reasons,
            "matched": mappable,
            "missing": 0,
            "mismatched": 0,
            "fingerprint_drift": 0,
            "extra": 0,
            "query": {
                "count_match": True,
                "checksum_match": True,
                "ordering_match": True,
                "aggregate_match": True,
            },
        }

    performance = {"within_tolerance": True}
    components = {
        "glucose": component(100, 1, {"value_out_of_range": 1}),
        "fingersticks": component(1, 0, {}),
    }
    _, failures = _evaluate("glucose", components, performance)
    assert failures == []

    components["glucose"]["unmappable_by_reason"] = {
        "timestamp_not_unambiguous": 1
    }
    _, failures = _evaluate("glucose", components, performance)
    assert "unexplained_unmappable_reasons" in failures
