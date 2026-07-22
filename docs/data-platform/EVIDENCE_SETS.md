# Evidence sets and observation windows

Status: additive, feature-gated projection
Implementation: `server/evidence_sets.py`, migration 12

G2 lets a derived claim cite a bounded time-series window without creating a
graph edge for every glucose or heart-rate sample. Source entities remain the
authority; the SQLite evidence projection is deterministic and rebuildable.

## Observation windows

An `observation_windows` row contains the canonical query definition, inclusive
UTC range, entity type, ordered member IDs, count, summary, query checksum,
observation checksum, generator version, and validity state. Membership is a
single bounded JSON index (maximum 100,000 observations), not graph edges.

The ID is deterministic over owner, query checksum, and canonical observation
checksum. Reversing input order produces the same window because observations
are ordered by canonical time and ID before hashing.

## Drill-down and invalidation

Drill-down loads the recorded IDs from the authoritative entity repository in
bounded batches, restores the original order, and recomputes the canonical
checksum. Missing, edited, or owner-mismatched observations invalidate the
window with reason `source_data_changed` and permanently invalidates every set
that cites it; stale drill-down then fails instead of presenting changed
evidence as the original support. Reverting the source does not revive an old
generation—a fresh checksum-addressed window must be built.

## Evidence sets

`evidence_sets` binds one claim to 1–16 observation windows through
`evidence_set_windows`. Its checksum includes claim identity, ordered window
IDs/checksums, summary, generator version, and input-data version. The claim
must exist and share the deployment owner. Pattern analysis can create one CGM
window and cite it from every generated Pattern, avoiding sample-edge explosion.

## Rollout and rollback

- `EVIDENCE_SET_WRITES_ENABLED=false` disables Pattern projection writes.
- `EVIDENCE_SET_READS_ENABLED=false` keeps existing inline Pattern/Insight and
  ChatMessage evidence reads unchanged.
- No production backfill is performed in G2.
- Disable both flags to roll back behavior while retaining the additive tables.
  Restore a verified pre-migration backup before using an older image.

Verified backups compare observation-window, evidence-set, and join counts.
Synthetic risk-critical tests cover determinism, bounds, exact drill-down,
mutation invalidation, owner/claim validation, Pattern citation cardinality,
indexes, and restore parity.
