"""Private, value-free validation reports for typed-domain read cutovers.

Legacy JSON remains authoritative. This tool compares the typed projections,
captures representative query latency, and writes checksum-protected reports.
It never enables typed reads and never emits row identifiers or clinical values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import DB_PATH
from .typed_glucose import compare_glucose_stores
from .typed_treatments import compare_treatment_stores
from .typed_wearables import compare_wearable_stores


CONTRACT_VERSION = "dual-write-validation/1.0.0"
REPORT_NAMES = {
    "treatments": "treatments-cutover-report.json",
    "glucose": "glucose-cutover-report.json",
    "wearables": "wearables-cutover-report.json",
}
POLICIES: dict[str, dict[str, Any]] = {
    "treatments": {
        "allowed_unmappable_reasons": {},
        "maximum_unmappable_ratio": 0.0,
        "maximum_duplicate_source_identities": 0,
    },
    "glucose": {
        # Historical imports may contain values rejected by the strict 20–600
        # mg/dL contract. They remain available from authoritative legacy JSON.
        "allowed_unmappable_reasons": {"value_out_of_range": "legacy_compatibility"},
        "maximum_unmappable_ratio": 0.01,
        "maximum_duplicate_source_identities": 0,
    },
    "wearables": {
        "allowed_unmappable_reasons": {},
        "maximum_unmappable_ratio": 0.0,
        "maximum_duplicate_source_identities": 0,
    },
}

_DOMAIN_COMPONENTS = {
    "treatments": ("treatments",),
    "glucose": ("glucose", "fingersticks"),
    "wearables": (
        "OuraDaily",
        "FitbitDaily",
        "OuraHeartRate",
        "FitbitHeartRate",
    ),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _checksum(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * percentile) - 1)
    return ordered[index]


def _measure(connection: sqlite3.Connection, sql: str, samples: int) -> dict[str, float]:
    durations: list[float] = []
    connection.execute(sql).fetchall()
    for _ in range(max(1, samples)):
        started = time.perf_counter()
        connection.execute(sql).fetchall()
        durations.append((time.perf_counter() - started) * 1000)
    return {
        "p50_ms": round(statistics.median(durations), 3),
        "p95_ms": round(_percentile(durations, 0.95), 3),
    }


_BENCHMARKS = {
    "treatments": (
        """
        SELECT id FROM entities
        WHERE type='Treatment'
        ORDER BY json_extract(data, '$.timestamp'), id LIMIT 500
        """,
        """
        SELECT entity_id FROM typed_treatments
        ORDER BY occurred_at, entity_id LIMIT 500
        """,
    ),
    "glucose": (
        """
        SELECT id FROM entities
        WHERE type IN ('GlucoseReading','FingerstickReading')
        ORDER BY COALESCE(json_extract(data, '$.timestamp'),
                          json_extract(data, '$.date')), id LIMIT 500
        """,
        """
        SELECT entity_id FROM (
            SELECT entity_id, observed_at FROM glucose_readings
            UNION ALL
            SELECT entity_id, observed_at FROM fingerstick_readings
        )
        ORDER BY observed_at, entity_id LIMIT 500
        """,
    ),
    "wearables": (
        """
        SELECT id FROM entities
        WHERE type IN ('OuraDaily','FitbitDaily','OuraHeartRate','FitbitHeartRate')
        ORDER BY COALESCE(json_extract(data, '$.timestamp'),
                          json_extract(data, '$.date')), id LIMIT 500
        """,
        """
        SELECT entity_id FROM (
            SELECT entity_id, observed_at FROM wearable_samples
            UNION ALL
            SELECT entity_id, observed_date AS observed_at FROM wearable_daily
        )
        ORDER BY observed_at, entity_id LIMIT 500
        """,
    ),
}


def benchmark_domains(database: Path = DB_PATH, *, samples: int = 7) -> dict[str, Any]:
    """Return representative legacy/typed latency without returning query rows."""
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        results = {}
        for domain, (legacy_sql, typed_sql) in _BENCHMARKS.items():
            legacy = _measure(connection, legacy_sql, samples)
            typed = _measure(connection, typed_sql, samples)
            threshold = max(50.0, legacy["p95_ms"] * 2 + 5)
            results[domain] = {
                "sample_count": max(1, samples),
                "legacy": legacy,
                "typed": typed,
                "typed_p95_threshold_ms": round(threshold, 3),
                "within_tolerance": typed["p95_ms"] <= threshold,
            }
        return results
    finally:
        connection.close()


def _component_checks(component: dict[str, Any]) -> dict[str, bool]:
    query = component["query"]
    return {
        "all_mappable_rows_match": all(
            component[key] == 0
            for key in ("missing", "mismatched", "fingerprint_drift", "extra")
        )
        and component["matched"] == component["mappable"],
        "query_count_matches": bool(query["count_match"]),
        "query_checksum_matches": bool(query["checksum_match"]),
        "query_ordering_matches": bool(query["ordering_match"]),
        "query_aggregate_matches": bool(query["aggregate_match"]),
    }


def _evaluate(
    domain: str,
    components: dict[str, dict[str, Any]],
    performance: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    policy = POLICIES[domain]
    total = sum(component["legacy_total"] for component in components.values())
    unmappable = sum(component["unmappable"] for component in components.values())
    reasons: dict[str, int] = {}
    checks: dict[str, dict[str, bool]] = {}
    failures: list[str] = []
    for name, component in components.items():
        checks[name] = _component_checks(component)
        failures.extend(
            f"{name}:{check}"
            for check, passed in checks[name].items()
            if not passed
        )
        for reason, count in component["unmappable_by_reason"].items():
            reasons[reason] = reasons.get(reason, 0) + count

    unexplained = sorted(set(reasons) - set(policy["allowed_unmappable_reasons"]))
    ratio = unmappable / total if total else 0.0
    if unexplained:
        failures.append("unexplained_unmappable_reasons")
    if ratio > policy["maximum_unmappable_ratio"]:
        failures.append("unmappable_ratio_exceeds_tolerance")
    duplicate_count = (
        components["treatments"].get("duplicate_source_identities", 0)
        if domain == "treatments"
        else 0
    )
    if duplicate_count > policy["maximum_duplicate_source_identities"]:
        failures.append("duplicate_source_identity_exceeds_tolerance")
    if not performance["within_tolerance"]:
        failures.append("query_latency_exceeds_tolerance")

    outcome = {
        "component_checks": checks,
        "unmappable": {
            "total": unmappable,
            "ratio": round(ratio, 8),
            "by_reason": dict(sorted(reasons.items())),
            "unexplained_reasons": unexplained,
        },
        "deduplication": {
            "accepted_legacy_entity_set_preserved": all(
                component["matched"] == component["mappable"]
                and component["missing"] == 0
                and component["extra"] == 0
                for component in components.values()
            ),
            "duplicate_source_identities": duplicate_count,
            "provider_overlap_policy": (
                "preserve_distinct_provider_observations"
                if domain == "wearables"
                else "repository_owned"
            ),
        },
        "performance_within_tolerance": performance["within_tolerance"],
    }
    return outcome, sorted(set(failures))


def _comparisons(database: Path) -> dict[str, dict[str, Any]]:
    treatment = compare_treatment_stores(database)
    glucose = compare_glucose_stores(database)
    wearables = compare_wearable_stores(database)
    return {
        "treatments": {"treatments": treatment},
        "glucose": {
            "glucose": glucose["glucose"],
            "fingersticks": glucose["fingersticks"],
        },
        "wearables": wearables["domains"],
    }


def _schema_version(database: Path) -> int:
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()
    return int(row[0])


def build_validation_reports(
    database: Path = DB_PATH,
    *,
    generated_at: str | None = None,
    phase: str = "historical",
    samples: int = 7,
    benchmarker: Callable[[Path], dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build one checksum-protected approval report per typed domain."""
    if phase not in {"historical", "incremental"}:
        raise ValueError("phase must be historical or incremental")
    comparisons = _comparisons(database)
    performance = (
        benchmarker(database)
        if benchmarker
        else benchmark_domains(database, samples=max(1, samples))
    )
    reports = {}
    for domain in REPORT_NAMES:
        outcome, failures = _evaluate(domain, comparisons[domain], performance[domain])
        body = {
            "contract_version": CONTRACT_VERSION,
            "domain": domain,
            "validation_phase": phase,
            "generated_at": generated_at or _utc_now(),
            "database_schema_version": _schema_version(database),
            "authority": {
                "legacy_reads_authoritative": True,
                "typed_reads_authoritative": False,
            },
            "policy": POLICIES[domain],
            "components": {
                name: comparisons[domain][name]
                for name in _DOMAIN_COMPONENTS[domain]
            },
            "performance": performance[domain],
            "outcome": outcome,
            "approval": {
                "decision": "eligible" if not failures else "blocked",
                "failure_codes": failures,
                "requires_detached_signature": True,
            },
        }
        reports[domain] = {**body, "report_checksum": _checksum(body)}
    return reports


def verify_report(report: dict[str, Any]) -> bool:
    supplied = report.get("report_checksum")
    body = {key: value for key, value in report.items() if key != "report_checksum"}
    return isinstance(supplied, str) and supplied == _checksum(body)


def write_validation_reports(
    reports: dict[str, dict[str, Any]],
    output_dir: Path,
) -> list[dict[str, str]]:
    """Create private reports without following links or overwriting evidence."""
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise ValueError("output directory must be a real directory")
    os.chmod(output_dir, 0o700)
    written = []
    for domain, filename in REPORT_NAMES.items():
        report = reports[domain]
        if not verify_report(report):
            raise ValueError(f"{domain} report checksum is invalid")
        path = output_dir / filename
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(report, stream, indent=2, sort_keys=True)
                stream.write("\n")
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        written.append(
            {
                "domain": domain,
                "decision": report["approval"]["decision"],
                "filename": filename,
                "report_checksum": report["report_checksum"],
            }
        )
    return written


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--database", type=Path, default=DB_PATH)
    validate.add_argument("--output-dir", type=Path, required=True)
    validate.add_argument(
        "--phase",
        choices=("historical", "incremental"),
        default="historical",
    )
    validate.add_argument("--samples", type=int, default=7)
    verify = subparsers.add_parser("verify")
    verify.add_argument("reports", nargs="+", type=Path)
    args = parser.parse_args()

    if args.command == "validate":
        reports = build_validation_reports(
            args.database,
            phase=args.phase,
            samples=max(1, args.samples),
        )
        print(json.dumps(write_validation_reports(reports, args.output_dir), sort_keys=True))
        return

    results = []
    exit_code = 0
    for path in args.reports:
        valid = verify_report(json.loads(path.read_text(encoding="utf-8")))
        results.append({"filename": path.name, "checksum_valid": valid})
        exit_code |= not valid
    print(json.dumps(results, sort_keys=True))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    _main()
