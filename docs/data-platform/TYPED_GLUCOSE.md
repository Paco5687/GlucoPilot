# Typed glucose and fingerstick storage

Status: additive dual-write and shadow-read pilot; legacy reads remain default

Implementations: `server/typed_glucose.py`, migration 9 in
`server/migrations.py`, `server/readings.py`, and `server/fingerstick.py`

I9 adds strict, rebuildable glucose and fingerstick projections while keeping
the JSON entity store authoritative. Connector, supported import, and demo CGM
writes share one repository-owned cross-source dedup operation. Generic entity
CRUD retains its existing exact compatibility semantics; when dual writes are
enabled, its accepted writes still project transactionally into the typed
tables.

## Typed schema

`glucose_readings` stores the legacy entity/canonical identity, deployment
owner, source and optional canonical source-record identity, canonical observed
instant, original timestamp, local date, mg/dL value, trend, assertion/source
class, mapping version, deterministic legacy fingerprint, and received/recorded
times. Values must be finite and between 20 and 600 mg/dL.

`fingerstick_readings` stores the patient-reported meter observation plus the
fixed CGM pairing captured at entry time: paired entity identity, value,
original/canonical timestamp, source, and CGM-minus-meter delta. Meter and
paired values must be finite and between 10 and 800 mg/dL. A stored delta must
match the two retained values within the one-decimal rounding tolerance.

Both tables reference the compatibility entity with `ON DELETE CASCADE` and
have owner/time indexes. Glucose additionally indexes owner/source/time and
non-null provider identity; fingersticks index their paired glucose identity.
Typed repository queries always apply the stable deployment-owner identity so
range/order reads use those indexes while retaining the legacy owner-email
filter semantics.
Invalid legacy rows remain untouched and are reported as unmappable rather than
being coerced into the typed schema.

## Cross-source dedup contract

`GlucoseCompatibilityRepository.create_deduplicated` owns the shared rule:

- a candidate is skipped when any retained owner reading is within **±240
  seconds**, regardless of source or value;
- the boundary is inclusive: exactly 240 seconds is a duplicate, 241 seconds
  is retained;
- invalid or timezone-ambiguous timestamps are skipped;
- candidates are ordered by canonical instant with stable input ordering;
- an accepted earlier source remains the compatibility fact; later providers
  do not overwrite it; and
- the lookup and batch insert run under one SQLite writer transaction, so
  overlapping requests cannot both pass the check in one process/database.

Dexcom, Dexcom Share, Nightscout, Glooko, Base44-export import, legacy import,
and demo seed now use this repository path. This preserves the measured legacy
tolerance while removing the Base44 import's second implementation.

## Dual writes, backfill, and parity

`TYPED_GLUCOSE_WRITES_ENABLED=true` activates a transaction-local hook for
GlucoseReading and FingerstickReading creates/updates. A typed constraint or
unexpected persistence failure rolls back the legacy mutation too. A legacy
row that no longer maps removes only its rebuildable typed projection. Legacy
deletion cascades to typed storage.

The explicit restartable backfill scans legacy row IDs in bounded batches and
upserts by entity ID:

```bash
TYPED_GLUCOSE_WRITES_ENABLED=true \
  python -m server.typed_glucose backfill --database /data/app.sqlite3
python -m server.typed_glucose compare --database /data/app.sqlite3
```

Re-running after any completed batch is safe. The comparison emits only
value-free counts, SHA-256 checksums, match/missing/drift/extra counts, ordering
and aggregate booleans, and the mapping version. It emits no row values,
timestamps, identities, or source payloads.

## Shadow reads and rollback

The independent switches are intentionally false by default:

| Flag | Behavior |
|---|---|
| `TYPED_GLUCOSE_WRITES_ENABLED` | Maintain typed projections and permit explicit backfill. |
| `TYPED_GLUCOSE_SHADOW_READS_ENABLED` | Run supported typed and legacy queries, log value-free parity/latency, return legacy results. |
| `TYPED_GLUCOSE_READS_ENABLED` | Return typed results for supported query shapes; unsupported JSON fields fall back to legacy. |

When both read and shadow flags are enabled, both stores are compared and typed
results are returned. With only typed reads enabled, the compatibility
repository avoids the legacy query. Rollback sets the read flag false; no
database restore is required. Disabling writes stops projection maintenance but
does not delete typed or legacy rows.

Before a production backfill or read cutover, create and verify an off-volume
backup, enable writes first, run backfill and comparison, review every mismatch,
and retain legacy rows. H2 owns production parity approval and H3 owns default
read cutover; I9 does not enable either in production.
