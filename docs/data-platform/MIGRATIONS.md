# SQLite schema migrations and entity registry

`server/migrations.py` is the only supported path for changing persistent
SQLite schema. Application startup calls it before legacy data compatibility,
the background scheduler, or request serving begins.

## Runtime contract

1. Open the configured database with a 30-second busy timeout.
2. Enable WAL mode with bounded retry for simultaneous clean starts.
3. Acquire a SQLite writer lock with `BEGIN IMMEDIATE`.
4. Create the `schema_migrations` tracking table inside that transaction.
5. validate that applied versions are a contiguous prefix and that names and
   SHA-256 checksums match the application.
6. Apply every pending statement and registry update in version order.
7. Validate the database registry against the code registry.
8. Commit once. Any failure rolls back every pending migration from that
   startup and raises `MigrationError`, preventing application startup.

This makes concurrent startup idempotent: one process applies migrations while
the other waits, rechecks the ledger, and applies nothing.

## Current versions

| Version | Name | Purpose |
|---:|---|---|
| 1 | `legacy_json_store_baseline` | Converges clean and existing installations on `app_settings`, `entities`, and the three legacy indexes without rewriting rows. |
| 2 | `entity_schema_registry` | Persists the 34-type F0 schema catalog while preserving the 19-type generic API allowlist. |
| 3 | `immutable_source_archive` | Adds typed immutable source payload/file metadata and sync-run tables without changing legacy JSON reads or writes. |
| 4 | `connector_provenance_runs` | Adds connector outcome/freshness columns and immutable normalized-to-source links without changing entity JSON or APIs. |
| 5 | `canonical_clinical_time` | Adds a rebuildable time sidecar with canonical timeline indexes, precision, inference, duration, and DST state. |
| 6 | `typed_treatment_domain` | Adds strict, rebuildable treatment, basal-segment, and pump-daily-total projections without changing legacy JSON or generic APIs. |
| 7 | `auditable_medical_record_extraction` | Adds extraction runs, versioned source-located observations, and immutable verification events while retaining `LabResult` compatibility rows. |
| 8 | `clinical_contradiction_ledger` | Adds versioned rule runs, both-sides contradiction records, and immutable attributed resolution events without changing legacy clinical rows. |
| 9 | `typed_glucose_and_fingersticks` | Adds strict, indexed, rebuildable glucose and paired-fingerstick projections without changing legacy JSON authority or generic APIs. |
| 10 | `typed_wearable_storage` | Adds strict, indexed, rebuildable daily wearable and heart-rate sample projections while preserving provider overlap and legacy APIs. |
| 11 | `governed_relationship_storage` | Adds strict owner-scoped edges plus governed predicate, assertion-status, evidence-level, and deterministic-algorithm registries without changing legacy relationship reads. |
| 12 | `evidence_sets_and_observation_windows` | Adds bounded checksum-addressed time-series windows and claim evidence sets without per-sample graph edges or legacy-read changes. |
| 13 | `relationship_projection_runs` | Adds strict graph build runs, historical run-edge membership, active generated edges, checksums, watermarks, and freshness state for atomic full/scoped rebuilds. |
| 14 | `versioned_evidence_backed_claims` | Adds the governed Pattern/Insight algorithm registry, immutable claim-version lineage, EvidenceSet links, evidence roles/rationales, and structured limitations. |
| 15 | `guarded_health_hypothesis_ledger` | Adds hypothesis origin/status, append-only supporting/opposing/missing evidence revisions, attributable confidence and lifecycle events, and clinician-gated terminal decisions. |

## Adding a migration

- Never edit a released `Migration`, its statements, its name, or the immutable
  baseline registry tuple. Applied checksums intentionally reject that drift.
- Append the next contiguous `Migration` to `MIGRATIONS`.
- Use parameterized `Statement` objects for data/registry changes.
- Add new schema metadata to `ENTITY_SCHEMAS` and insert/update it in the new
  migration. Registration must not accidentally grant generic API exposure.
- Add relationship vocabulary only through a new migration and the immutable
  code registry; startup rejects predicate/status/evidence/algorithm drift.
- Add Pattern/Insight algorithms only through a new migration; startup rejects
  claim-algorithm registry drift.
- Keep migrations additive and SQLite-transactional. Network/filesystem work
  does not belong in a schema migration.
- Add clean-install, legacy-upgrade, idempotency, rollback, drift, and
  concurrency coverage appropriate to the change.
- Exercise the migration against an isolated online backup of a
  production-shaped database before release.

## Failure handling

Do not edit `schema_migrations` to bypass a checksum, gap, newer-schema, or
registry-drift error. Keep the application stopped, preserve the database and
WAL files, capture the exact error, and either deploy compatible code or
restore the verified pre-upgrade backup.
