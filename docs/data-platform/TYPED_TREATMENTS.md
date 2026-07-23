# Typed treatments, basal segments, and pump daily totals

Status: additive dual-write and shadow-read pilot; legacy reads remain default

Implementations: `server/typed_treatments.py`, migration 6 in
`server/migrations.py`, and `tests/test_typed_treatments.py`

## Compatibility boundary

`Treatment` JSON entities remain authoritative in this release. The generic
entity routes still read and mutate `entities` directly, so their request and
response envelopes do not change. `TreatmentCompatibilityRepository` also
keeps every mutation on the legacy repository. It may serve supported domain
queries from the typed projection only when
`TYPED_TREATMENT_READS_ENABLED=true`; the shipped default is `false`.

`TYPED_TREATMENT_WRITES_ENABLED=true` adds a projection write to the same
SQLite transaction as each legacy create or update. Deletes cascade from the
legacy entity. A row that cannot satisfy the strict contract remains available
through the legacy API, while any stale typed projection for it is removed.
This preserves compatibility without inventing a clinical time or value.

`TYPED_TREATMENT_SHADOW_READS_ENABLED=true` runs supported legacy and typed
queries, logs value-free parity and latency, and still returns legacy results.
It is independent from the typed-read flag and defaults to false.

## Tables and ownership

All rows use the single-deployment owner
`urn:glucopilot:owner:self`. Source timestamps are retained for compatibility;
`occurred_at` is the unambiguous canonical UTC instant used for typed indexes.

| Table | Identity and purpose |
|---|---|
| `typed_treatments` | One strict projection per legacy entity ID. Carries canonical identity, source identity, normalized kind, units, UTC time, legacy-compatible fields, mapping version, and a deterministic fingerprint. |
| `basal_segments` | At most one child per typed treatment. Represents temp-basal and suspension start/end, duration in seconds, rate in U/hour, and optional profile percent. |
| `pump_daily_totals` | At most one child per typed treatment. Parses authoritative `Daily Total` notes into total/basal/bolus units and marks partial parses explicitly. |

Primary keys enforce entity-level idempotency. Child primary keys also enforce
one basal or daily-total interpretation per source treatment. Canonical IDs are
unique. Provider IDs are deliberately indexed, not unique: the legacy generic
API historically permits duplicate `ns_id` values, and enabling a sidecar must
not make an otherwise accepted legacy request fail. Existing connectors dedup
provider IDs before writes; cross-entity duplicates are exposed by comparison
rather than silently deleted.

Indexes cover owner/time, owner/source/time, provider ID, treatment kind,
basal start, and pump-total local date. Raw legacy values are not logged by the
backfill or comparison commands.

## Mapping rules

| Legacy `type` | Typed kind / child | Unit behavior |
|---|---|---|
| `insulin` | `insulin`; optional pump-total child for `Daily Total` | `amount` is U; parsed totals are U |
| `carb` | `carbohydrate` | `amount` is g and required |
| `tempbasal` | `basal` plus temp-basal segment | `absolute` is U/hour; `duration` minutes becomes seconds |
| `suspension` | `suspension` plus suspension segment | absent rate becomes 0 U/hour; duration is explicit when known |
| `bg` | `blood_glucose` | `glucose` is mg/dL and required |
| `note` / unknown | `note` / `other` | narrative fields remain optional |

The original legacy type, event type, timestamp text, notes, reason, provider
ID, and envelope times remain available to the compatibility projection.
Combined Nightscout meal-bolus notes remain notes; the mapper does not invent a
separate carbohydrate event.

## Backfill and shadow comparison

After migration 6 and a verified backup, enable writes only for a reviewed
rehearsal and run:

```bash
TYPED_TREATMENT_WRITES_ENABLED=true \
  python -m server.typed_treatments backfill --database /data/app.sqlite3
python -m server.typed_treatments compare --database /data/app.sqlite3
```

Backfill is bounded by `--batch-size`, ordered by legacy row ID, committed per
batch, and safe to repeat. The comparison recomputes every expected projection
and reports only counts for matched, missing, mismatched, extra, categorized
unmappable rows, child-table totals, provider-identity duplicates, and query
checksum/order/aggregate parity. A read rollout is not permitted while any
unexpected mismatch remains.

H2's private per-domain approval and signing procedure is defined in
[Production dual-write validation](DUAL_WRITE_VALIDATION.md). Rollback for this
release is switching shadow reads and writes off while leaving typed reads off.
Legacy data continues to serve all existing paths. Restoring a pre-migration
verified backup remains the database rollback procedure; the migration does not
remove or rewrite JSON.
