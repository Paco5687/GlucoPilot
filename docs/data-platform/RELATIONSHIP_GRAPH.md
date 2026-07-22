# Governed relationship projection

Status: additive storage and deterministic build lifecycle; projection reads disabled by default
Implementation: `server/relationship_registry.py`, `server/relationships.py`,
`server/relationship_projection.py`, migrations 11 and 13

G1 adds a rebuildable relationship projection inside the existing SQLite
database. It does not add a graph database, replace source entities, expose a
new API, or make projected edges authoritative.

G3 adds versioned build jobs for that projection. The initial
`legacy-reference-projection/1.0.0` reads current authoritative `LabResult`
`record_id` and `ChatMessage` `thread_id` references, including reviewed edits
already applied to those entities, and emits both governed directions. It does
not infer new clinical claims from unverified text.

## Governed registries

Four migration-owned registries reject vocabulary drift:

| Registry | Initial governed values |
|---|---|
| Assertion status | `unverified`, `provisional`, `confirmed`, `disputed`, `refuted`, `superseded`, `entered_in_error` |
| Evidence level | `none`, `assertion_only`, `source_record`, `corroborated`, `clinician_reviewed` |
| Predicate | `extracted_from`, `has_lab_result`, `member_of_thread`, `has_message`, each with an exact subject/object pair and inverse |
| Algorithm | `legacy-reference-projection/1.0.0`, deterministic and rebuildable |

Registry changes require a new ordered migration. Startup compares every
persisted row with the immutable code registry and fails with
`MigrationError` on additions, edits, or deletions. Foreign keys reject unknown
values and predicate/type mismatches at write time.

## Edge contract

`entity_relationships` stores directed, owner-scoped projections with:

- registered subject type/ID, predicate, and registered object type/ID;
- assertion kind/status and evidence level;
- source class and non-empty source identifier;
- generator ID/version, immutable input-data version/hash, and stable
  projection key;
- point, closed-interval, open-ended, or unknown validity;
- confidence label, optional finite score, method, and calibration version;
- canonical generation and creation timestamps.

The repository verifies both nodes exist with the declared registered types
and that both carry the requested `owner_email`. The database additionally
fixes `owner_id` to `urn:glucopilot:owner:self`.

## Deterministic rebuild and immutability

Relationship identity is deterministic over owner, generator ID/version,
input-data version, and projection key. Repeating the same projection is
idempotent. Reusing that identity with different content fails rather than
silently rewriting provenance. A new input-data version produces a new row, so
older generations remain available for later supersession/rebuild logic.

Only algorithms registered as both deterministic and rebuildable may write
projection rows. Each G3 plan canonically sorts its relationship-bearing input,
computes an input checksum and version, and caps output at 200,000 edges. A
repeat build over unchanged input therefore produces the same edge identities,
output checksum, and complete-graph checksum.

`relationship_projection_runs` records running, succeeded, and failed jobs,
their full or entity scope, input version/hash, source watermark, output count,
checksum, timestamps, and bounded error metadata. Historical run-edge
membership is retained. Active-edge membership and
`relationship_projection_state` identify the one complete published view.

Publication uses one `BEGIN IMMEDIATE` transaction:

1. Validate and insert every immutable edge and historical membership.
2. Replace all active memberships for a full build, or only the exact
   `LabResult:<id>` / `ChatMessage:<id>` anchor for a scoped build.
3. Recompute the checksum and watermark over all active edges.
4. Mark the run successful and advance freshness.

Any exception rolls back all four steps. The separately durable run is marked
failed, while the prior active graph, checksum, and freshness remain unchanged.
Scoped builds cannot emit an edge outside their requested anchor. This prevents
partial or mixed publication without requiring a second database.

Only relationships claimed through run-edge membership are replaceable by the
projector. Direct patient reports, clinician confirmations, and any other
independently authored assertions have no such membership and survive full and
scoped derived rebuilds.

## Query indexes

Indexes cover:

- owner + subject + temporal validity;
- owner + object + temporal validity;
- owner + predicate + assertion status + confidence; and
- owner + generator/version + input-data version + projection key.

Repository queries may filter outgoing edges by predicate, an instant within
validity, and minimum confidence. Unknown validity is excluded from a
point-in-time query rather than guessed.

Managed historical edges remain immutable for audit, but typed queries expose
only active managed edges plus independently authored edges. Freshness reports
the current graph checksum/count, source watermark, publication age, last
successful run, and latest run status, so a failed rebuild is observable
without being mistaken for fresh data.

## Compatibility and rollback

`RepositoryCatalog.relationships` remains a compatibility adapter. With the
default `RELATIONSHIP_READS_ENABLED=false`, existing lab→record and
message→thread reads are projected directly from legacy fields exactly as
before. The strict repository is available as `typed_relationships` for the G3
projector and tests. Enabling typed reads is an explicit later cutover gate.
The operational rebuild entry point is independently gated by
`RELATIONSHIP_PROJECTION_WRITES_ENABLED=false` and supports a full build or one
bounded `LabResult`/`ChatMessage` anchor.

After a verified backup, an operator may temporarily enable the write gate and
run a full build:

```bash
python -m server.relationship_projection --owner-email owner@glucopilot.local
```

Add `--entity-type LabResult --entity-id <id>` (or `ChatMessage`) for a scoped
repair. Disable the write gate again after inspecting the recorded run,
checksum, count, watermark, and backup verification. Never enable typed reads
merely because a build completed; read cutover has its own parity gate.

Rollback is therefore a configuration change: keep both
`RELATIONSHIP_PROJECTION_WRITES_ENABLED=false` and
`RELATIONSHIP_READS_ENABLED=false`. Migrations 11 and 13 are additive and do
not affect legacy references. If an older image must be restored, use a
verified pre-migration backup rather than editing the migration ledger.

## Backup and test coverage

Verified backup manifests include edge and all four registry counts plus run,
historical membership, active membership, and freshness-state counts. The
synthetic golden fixture covers exact types, temporal intervals/points,
confidence, algorithm/input versions, build scopes, and public-safety checks.
Risk-critical tests also cover registry drift, unknown values, owner mismatch,
missing nodes, invalid temporal/confidence metadata, deterministic checksums,
failure rollback, scoped replacement, authored assertion preservation,
freshness, index selection, compatibility reads, and clean restore parity.
