# Governed relationship projection

Status: additive storage and repository contract; projection reads disabled by default
Implementation: `server/relationship_registry.py`, `server/relationships.py`, migration 11

G1 adds a rebuildable relationship projection inside the existing SQLite
database. It does not add a graph database, replace source entities, expose a
new API, or make projected edges authoritative.

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

## Rebuild and immutability

Relationship identity is deterministic over owner, generator ID/version,
input-data version, and projection key. Repeating the same projection is
idempotent. Reusing that identity with different content fails rather than
silently rewriting provenance. A new input-data version produces a new row, so
older generations remain available for later supersession/rebuild logic.

Only algorithms registered as both deterministic and rebuildable may write
projection rows. G3 will add the bounded projector and generation lifecycle;
G1 deliberately does not backfill production relationships.

## Query indexes

Indexes cover:

- owner + subject + temporal validity;
- owner + object + temporal validity;
- owner + predicate + assertion status + confidence; and
- owner + generator/version + input-data version + projection key.

Repository queries may filter outgoing edges by predicate, an instant within
validity, and minimum confidence. Unknown validity is excluded from a
point-in-time query rather than guessed.

## Compatibility and rollback

`RepositoryCatalog.relationships` remains a compatibility adapter. With the
default `RELATIONSHIP_READS_ENABLED=false`, existing lab→record and
message→thread reads are projected directly from legacy fields exactly as
before. The strict repository is available as `typed_relationships` for the G3
projector and tests. Enabling typed reads is an explicit later cutover gate.

Rollback is therefore a configuration change: keep
`RELATIONSHIP_READS_ENABLED=false`. Migration 11 is additive and the empty
relationship table does not affect legacy reads or writes. If an older image
must be restored, use a verified pre-migration backup rather than editing the
migration ledger.

## Backup and test coverage

Verified backup manifests include edge and all four registry counts. The
synthetic golden fixture covers exact types, temporal intervals/points,
confidence, algorithm/input versions, and public-safety checks. Risk-critical
tests also cover registry drift, unknown values, owner mismatch, missing nodes,
invalid temporal/confidence metadata, deterministic idempotency, immutable
conflicts, index selection, compatibility reads, and clean restore parity.
