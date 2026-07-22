from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from server import companion, db, fingerstick, glooko, nightscout, patterns, readings, records, report
from server.data_contracts import DstResolution, LocalTimeContext, PartialTime, TimeBasis, TimePrecision
from server.migrations import run_migrations
from server.repositories import LegacyRepositoryCatalog
from server.unit_of_work import unit_of_work

pytestmark = pytest.mark.risk_critical

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "golden" / "clinical_edge_cases.json"


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def golden_database(tmp_path, monkeypatch) -> Path:
    database = tmp_path / "app.sqlite3"
    run_migrations(database)
    monkeypatch.setattr(db, "DB_PATH", database)
    return database


def _seed(entity_type: str, rows: list[dict]) -> None:
    db.bulk_create_entities(entity_type, rows)


def _entry(rows: list[dict], test_name: str) -> dict:
    return next(row for row in rows if row["test_name"] == test_name)


def test_fixture_is_explicitly_synthetic_and_public_safe(golden):
    encoded = json.dumps(golden, sort_keys=True)
    assert golden["synthetic"] is True
    assert golden["subject"]["id"].startswith("synthetic-")
    assert "Emily" not in encoded

    emails = set(re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", encoded, re.I))
    assert emails == {"owner@glucopilot.local"}
    assert set(re.findall(r"https?://[^\"/]+", encoded)) == {
        "https://reference-a.example.invalid",
        "https://reference-b.example.invalid",
    }
    assert not re.search(
        r"access[_-]?token|refresh[_-]?token|api[_-]?key|client[_-]?secret|password",
        encoded,
        re.I,
    )


def test_source_parsers_match_the_golden_normalization_contract(golden):
    fixture = golden["parser"]
    for case in fixture["glooko_readings"]:
        mapped = glooko._map_reading(case["input"])
        if case["expected_value"] is None:
            assert mapped is None
        else:
            assert mapped["value"] == case["expected_value"]
            assert mapped["source"] == "glooko"
            assert mapped["timestamp"].endswith("Z")

    bolus = glooko._map_bolus(fixture["glooko_bolus"]["input"])
    assert len(bolus) == 2
    for actual, expected in zip(bolus, fixture["glooko_bolus"]["expected"], strict=True):
        assert {key: actual[key] for key in expected} == expected
        assert actual["timestamp"] == "2026-01-15T13:00:00.000Z"

    basal = glooko._map_basal(fixture["glooko_basal"]["input"])
    expected_basal = fixture["glooko_basal"]["expected"]
    assert {key: basal[key] for key in expected_basal} == expected_basal

    combined = nightscout.map_treatment(fixture["nightscout_combined_treatment"]["input"])
    expected_combined = fixture["nightscout_combined_treatment"]["expected"]
    assert {key: combined[key] for key in expected_combined} == expected_combined


def test_cross_source_readings_are_deduplicated_globally(golden, golden_database):
    fixture = golden["source_overlap"]
    _seed("GlucoseReading", fixture["existing_readings"])

    created, skipped = readings.persist_readings_deduped(fixture["incoming_readings"])

    expected = fixture["expected"]
    assert (created, skipped) == (expected["created"], expected["skipped"])
    stored = db.query_entities("GlucoseReading", sort="timestamp")
    assert [row["timestamp"] for row in stored] == expected["stored_timestamps"]


def test_fingerstick_disagreement_keeps_both_measurements_and_delta(golden, golden_database):
    fixture = golden["fingerstick_disagreement"]
    _seed(
        "GlucoseReading",
        [
            {
                **row,
                "source": "synthetic-cgm",
                "owner_email": golden["subject"]["owner_email"],
            }
            for row in fixture["cgm"]
        ],
    )

    response = asyncio.run(fingerstick.handle({"action": "add", **fixture["fingerstick"]}))
    stored = response["reading"]
    expected = fixture["expected"]
    assert stored["value"] == fixture["fingerstick"]["value"]
    assert stored["cgm_value"] == expected["matched_cgm"]
    assert stored["delta"] == expected["delta"]

    stats = asyncio.run(fingerstick.handle({"action": "stats"}))
    assert stats["paired"] == 1
    assert stats["bias"] == expected["bias"]
    assert stats["max_abs_delta"] == expected["delta"]


def test_isolated_compression_low_does_not_become_a_recurring_pattern(
    golden,
    golden_database,
):
    fixture = golden["compression_low"]
    anchor = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    readings = []
    for day_index in range(fixture["days"]):
        day = anchor - timedelta(days=day_index)
        for hour in fixture["hours_utc"]:
            value = fixture["baseline_value"]
            artifact = fixture["artifact"]
            if day_index == artifact["day_index"] and hour == artifact["hour_utc"]:
                value = artifact["cgm_value"]
            readings.append(
                {
                    "timestamp": day.replace(hour=hour).isoformat().replace("+00:00", "Z"),
                    "value": value,
                    "source": "synthetic-cgm",
                    "owner_email": golden["subject"]["owner_email"],
                }
            )
    assert len(readings) == fixture["expected"]["reading_count"]
    _seed("GlucoseReading", readings)

    outcome = asyncio.run(patterns.analyze())

    assert outcome["patternsFound"] == 0
    assert [row["type"] for row in outcome["patterns"]] == fixture["expected"]["pattern_types"]
    assert db.query_entities("Pattern") == []


def test_lab_normalization_deduplicates_repeated_extraction(golden):
    fixture = golden["lab_extraction"]
    normalized = records._normalize_lab_results(fixture["extracted"], fixture["record_id"])
    expected = fixture["expected"]

    assert len(normalized) == expected["count"]
    assert [row["test_name"] for row in normalized] == expected["names"]
    assert normalized[0]["reference_high"] == expected["first_reference_high"]
    assert normalized[-1]["flag"] == expected["measurement_flag"]
    assert all(row["record_id"] == fixture["record_id"] for row in normalized)


def test_report_snapshot_preserves_missing_pump_cycle_lab_range_and_dst_semantics(
    golden,
    golden_database,
):
    owner = golden["subject"]["owner_email"]
    dst = golden["dst_fall_back"]
    _seed(
        "GlucoseReading",
        [{**row, "source": "synthetic-cgm", "owner_email": owner} for row in dst["readings"]],
    )
    _seed("Treatment", golden["missing_pump_data"]["treatments"])
    _seed(
        "PeriodLog",
        [{**row, "owner_email": owner} for row in golden["cycle_effects"]["period_logs"]],
    )
    _seed("LabResult", golden["conflicting_lab_ranges"]["labs"])

    glucose = report._glucose(ZoneInfo(dst["timezone"]), "2026-10-31T00:00:00Z")
    expected_glucose = dst["expected"]
    summary_keys = (
        "available",
        "readings",
        "days",
        "avg",
        "gmi",
        "cv",
        "std",
        "tir",
        "tbr70",
        "tbr54",
        "tar180",
        "tar250",
    )
    assert {key: glucose[key] for key in summary_keys} == {key: expected_glucose[key] for key in summary_keys}
    repeated_hour = glucose["agp"][expected_glucose["local_hour"]]
    assert repeated_hour == {
        "hour": 1,
        "p5": expected_glucose["p5"],
        "p25": expected_glucose["p25"],
        "p50": expected_glucose["p50"],
        "p75": expected_glucose["p75"],
        "p95": expected_glucose["p95"],
    }

    pump_fixture = golden["missing_pump_data"]
    insulin_snapshot = report._insulin(
        ZoneInfo("UTC"),
        "2026-04-01T00:00:00Z",
        pump_fixture["glucose_days"],
    )
    assert {key: insulin_snapshot[key] for key in pump_fixture["expected"]} == pump_fixture["expected"]

    cycle_fixture = golden["cycle_effects"]
    cycle_snapshot = report._cycle(
        ZoneInfo("UTC"),
        "2026-06-01T00:00:00Z",
        {"daily": cycle_fixture["glucose_daily"]},
    )
    assert cycle_snapshot["cycles_detected"] == cycle_fixture["expected"]["cycles_detected"]
    assert cycle_snapshot["avg_cycle_length"] == cycle_fixture["expected"]["avg_cycle_length"]
    assert cycle_snapshot["source"] == cycle_fixture["expected"]["source"]
    assert cycle_snapshot["per_phase"]["menstrual"] == cycle_fixture["expected"]["menstrual"]
    assert cycle_snapshot["per_phase"]["follicular"] == cycle_fixture["expected"]["follicular"]

    lab_fixture = golden["conflicting_lab_ranges"]
    lab_snapshot = report._labs()
    a1c = _entry(lab_snapshot["categories"]["Metabolic"], "Synthetic A1c")
    tsh = _entry(lab_snapshot["categories"]["Thyroid"], "Synthetic TSH")
    assert (a1c["trend"], a1c["reference_high"]) == (
        lab_fixture["expected"]["a1c_trend"],
        lab_fixture["expected"]["a1c_reference_high"],
    )
    assert (tsh["trend"], tsh["reference_high"]) == (
        lab_fixture["expected"]["tsh_trend"],
        lab_fixture["expected"]["tsh_reference_high"],
    )
    assert sorted(row["test_name"] for row in lab_snapshot["flagged"]) == sorted(
        lab_fixture["expected"]["flagged_tests"]
    )


def test_dst_ambiguity_requires_an_explicit_offset_from_the_fixture(golden):
    fixture = golden["dst_fall_back"]
    for offset, resolution in (
        (fixture["earlier_offset"], DstResolution.AMBIGUOUS_EARLIER_OFFSET),
        (fixture["later_offset"], DstResolution.AMBIGUOUS_LATER_OFFSET),
    ):
        timestamp = PartialTime(
            value=fixture["ambiguous_local"],
            precision=TimePrecision.MINUTE,
            basis=TimeBasis.SOURCE_REPORTED,
            local_context=LocalTimeContext(
                timezone=fixture["timezone"],
                utc_offset=offset,
                dst_resolution=resolution,
            ),
        )
        assert timestamp.local_context.utc_offset == offset

    with pytest.raises(ValidationError, match="explicit UTC offset"):
        LocalTimeContext(
            timezone=fixture["timezone"],
            dst_resolution=DstResolution.AMBIGUOUS_EARLIER_OFFSET,
        )


def test_companion_prompt_and_repository_preserve_evidence(golden, golden_database):
    fixture = golden["companion_evidence"]
    prompt = companion._reply_prompt(
        fixture["user_message"],
        fixture["dossier"],
        fixture["memories"],
        fixture["history"],
        fixture["sources"],
    )

    assert '"avg": 123' in prompt
    assert fixture["memories"][0]["content"] in prompt
    for index, source in enumerate(fixture["sources"], 1):
        assert f"[{index}] {source['title']} ({source['source']}) — {source['url']}" in prompt
        assert source["snippet"] in prompt
    assert "cite inline as [1], [2]" in prompt

    message = db.create_entity(
        "ChatMessage",
        {
            "owner_email": golden["subject"]["owner_email"],
            "role": "assistant",
            "content": "Synthetic evidence response.",
            "sources": fixture["sources"],
        },
    )
    evidence = LegacyRepositoryCatalog().evidence.for_claim(
        golden["subject"]["owner_email"],
        "ChatMessage",
        message["id"],
    )
    assert [reference.value for reference in evidence] == fixture["sources"]
    assert all(reference.evidence_kind == "external_source" for reference in evidence)


def test_golden_multi_table_write_rolls_back_completely(golden, golden_database):
    fixture = golden["rollback"]
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        with unit_of_work() as work:
            work.repositories.entity("HealthSummary").create(fixture["health_summary"])
            db.set_setting(
                fixture["setting"]["key"],
                fixture["setting"]["value"],
                connection=work.connection,
            )
            work.commit()
            raise RuntimeError("synthetic rollback")

    with sqlite3.connect(golden_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == fixture["expected_entity_count"]
        assert (
            connection.execute("SELECT COUNT(*) FROM app_settings").fetchone()[0] == fixture["expected_setting_count"]
        )
