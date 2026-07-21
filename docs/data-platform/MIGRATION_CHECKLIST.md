# Data-platform migration checklist

Use this for every schema, repository, dual-write, shadow-read, or cutover
change in the Epic. Each checkbox is a release gate.

## Design

- [ ] Name the owning repository and code owner.
- [ ] Link every current reader, writer, replacement, and deletion path.
- [ ] Define owner/subject authorization scope.
- [ ] Define upstream source, immutable source ID, sync run, and source-record
      version semantics.
- [ ] Define observed UTC instant, source timezone/offset, local date, and
      ingested/recorded timestamps independently.
- [ ] Define fields, units, enums, null semantics, partial-data behavior, and
      contradiction behavior.
- [ ] Define database-level idempotency and dedup keys.
- [ ] Classify source, canonical, profile, assertion, relationship, and derived
      records explicitly.

## Migration implementation

- [ ] Capture a value-free production baseline with `server.data_audit`.
- [ ] Take a consistent SQLite + uploaded-files backup and restore-test it.
- [ ] Add an ordered, idempotent migration with an explicit schema version.
- [ ] Keep the generic entity API compatible until all consumers migrate.
- [ ] Add repository/unit-of-work APIs; do not add raw SQL to feature modules.
- [ ] Add constraints/indexes from measured identity and query requirements.
- [ ] Scrub credentials and tokens before archiving source payloads.

## Backfill and dual write

- [ ] Backfill in bounded, restartable batches with checkpoints.
- [ ] Record source ID, target ID, migration version, and outcome without values
      in logs.
- [ ] Make interrupted and repeated batches idempotent.
- [ ] Reconcile cross-store partial failure explicitly.
- [ ] Compare legacy and typed counts by owner/source/time window.
- [ ] Shadow-compare nulls, units, ordering, pagination, and timezone edges.
- [ ] Quarantine mismatches instead of silently choosing one value.
- [ ] Benchmark API and browser behavior against the F0 baseline.

## Cutover and retirement

- [ ] Gate typed reads with a reversible switch.
- [ ] Prove backup, upgrade, restore, read rollback, and export in CI using a
      production-shaped synthetic fixture.
- [ ] Preserve or narrow provider/read-only authorization.
- [ ] Expose completeness, freshness, reliability, and contradictions before
      missing data can influence derived output.
- [ ] Record algorithm and input-data versions for every derived result.
- [ ] Keep legacy JSON for at least two verified release cycles.
- [ ] Obtain explicit approval before deleting or archiving legacy rows.
- [ ] Produce final parity counts and an unresolved-mismatch list.
- [ ] Prove historical backups remain upgradeable.
- [ ] Update this audit in the same change that retires a legacy path.
