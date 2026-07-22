# Data platform audit

This directory is the implementation baseline for the integrity, evidence, and
relationship-layer Epic. It describes the system before schema migrations or
typed repositories are introduced.

- [Entity catalog](ENTITY_CATALOG.md) — ownership, fields, identity, dedup,
  time semantics, mutability, and migration priority for every entity type.
- [Lineage and query inventory](LINEAGE_AND_QUERY_INVENTORY.md) — source and
  consumer paths, derived-data assumptions, destructive workflows, and query
  risks.
- [Production baseline](PRODUCTION_BASELINE.md) — privacy-safe storage, count,
  index, and latency measurements.
- [Migration checklist](MIGRATION_CHECKLIST.md) — release gates for later
  typed-table migrations.
- [Migration runner](MIGRATIONS.md) — ordered migration and schema-registry
  runtime contract.
- [Backup and rollback runbook](BACKUP_AND_ROLLBACK.md) — verified backup,
  upgrade, failure, restore, and recovery operations.
- [Clinical data contracts](DATA_CONTRACTS.md) — versioned canonical identity,
  time, provenance, assertion, evidence, confidence, and data-version semantics.
- [Domain repositories and unit of work](REPOSITORIES.md) — swappable legacy
  adapters, relationship/evidence projections, and atomic transaction rules.
- [Synthetic golden datasets](GOLDEN_DATASETS.md) — public-safe clinical edge
  cases, prior-release schema snapshots, expected outcomes, and CI gates.
- [Immutable source archive](SOURCE_ARCHIVE.md) — scrubbed/compressed payloads,
  file references, sync-run lineage, deduplication, retention, and rollout.
- [Connector provenance](CONNECTOR_PROVENANCE.md) — connector/upload coverage,
  complete outcomes, normalized evidence links, freshness, and rollout gates.
- [Canonical clinical time](CANONICAL_TIME.md) — event/recorded/ingestion roles,
  partial dates, duration intervals, DST handling, and rollout/backfill.
- [Typed treatments](TYPED_TREATMENTS.md) — strict treatment, basal-segment,
  and pump-total projections, compatibility mapping, parity, and rollout gates.
- [Auditable lab extraction](AUDITABLE_LAB_EXTRACTION.md) — source locations,
  validation, verification, correction/supersession history, and safe reprocessing.
- [Clinical contradiction ledger](CONTRADICTIONS.md) — deterministic conflict
  rules, both-sides display, attributed resolution history, and safe rollback.
- [Typed glucose and fingersticks](TYPED_GLUCOSE.md) — strict projections,
  repository-owned dedup, restartable backfill, shadow parity, and read rollback.
- [Typed wearables](TYPED_WEARABLES.md) — strict daily/sample projections,
  explicit provider overlap, bounded backfill, indexed reads, and rollback.
- [Governed relationship projection](RELATIONSHIP_GRAPH.md) — typed predicates,
  assertion/evidence/algorithm registries, owner validation, temporal/confidence
  indexes, deterministic full/scoped rebuild jobs, atomic publication,
  freshness, and compatibility rollback.
- [Authorized relationship API](RELATIONSHIP_API.md) — owner-scoped neighbors,
  reverse neighbors, bounded traversal, redacted evidence paths, query budgets,
  deterministic ordering, and provider-safe GET access.
- [Evidence sets and observation windows](EVIDENCE_SETS.md) — bounded time-series
  membership, deterministic checksums, exact drill-down, invalidation, and
  feature-gated Pattern citations.
- [Deterministic Evidence Bundle API](EVIDENCE_BUNDLES.md) — owner-scoped
  cross-domain queries, strict budgets, protected blocking contradictions,
  source links, content-addressed caching, and semantic invalidation.
- [Analytics confidence and replication](ANALYTICS_CONFIDENCE.md) — shared
  samples/effect/interval/missingness metadata, discovery/replication statuses,
  cycle-phase provenance, strength-scaled language, and AI handoff rules.

## Reproduce the structural audit

Run this inside an installed container when `/data` is not host-accessible:

```bash
python -m server.data_audit /data/app.sqlite3 > /tmp/glucopilot-audit.json
```

The command opens SQLite read-only and emits no entity values, identities,
timestamps, source identifiers, or settings. Its output is limited to counts,
field names/types, schema/index definitions, query plans, and timings. Review
the output before sharing it even though it is deliberately value-free.

These documents record current behavior; they do not normalize or change it.
