# Typed wearable daily and sample storage

Status: additive dual-write and shadow-read pilot; legacy reads remain default

Implementations: `server/typed_wearables.py`, migration 10 in
`server/migrations.py`, and the Oura, Fitbit, and Google Health connectors

I10 adds strict, rebuildable projections for daily wearable observations and
intraday heart-rate samples while retaining legacy JSON as the authority. Oura,
Fitbit, Google Health, supported imports, demo seed, cycle inference, Insights,
and other backend consumers share the wearable repository boundary. Generic
entity API behavior remains unchanged.

## Typed schema

`wearable_daily` stores one row per legacy `OuraDaily` or `FitbitDaily` entity:
canonical entity/owner identity, explicit provider, optional provider record
identity, provider-local observed date, typed sleep/readiness/activity/vital
metrics, assertion/source class, mapping version, fingerprint, and received /
recorded envelopes. Scores, efficiencies, SpO2, durations, counts, heart rate,
temperature deviation, respiratory rate, and HRV have finite domain checks.

`wearable_samples` stores one row per `OuraHeartRate` or `FitbitHeartRate`
entity: provider identity, canonical and original observed time, local date,
strict positive BPM value/unit, assertion/source class, version, fingerprint,
and envelopes.

Compatibility-only unknown fields are retained in a JSON extension so
typed reads do not silently drop an imported field. A known daily field's
presence is recorded separately, preserving the difference between an absent
metric and an explicitly reported null. Invalid legacy rows remain untouched
and appear as unmappable in parity reports.

## Provider overlap and indexes

Provider overlap is not deduplicated. Fitbit and Google Health observations for
the same date remain separate `FitbitDaily` rows with `provider=fitbit` and
`provider=google_health`; Oura remains independently identified. The same rule
applies to contemporaneous Oura and Google/Fitbit heart-rate samples.

Every typed query is deployment-owner and entity-type scoped. Daily tables have
owner/type/date and owner/provider/date indexes. Samples have owner/type/time
and owner/provider/time indexes. Both tables have partial provider-record
identity indexes. Thus the established latest/range queries can avoid JSON
extraction and temporary scans after an approved read cutover.

## Dual writes, bounded backfill, and parity

`TYPED_WEARABLE_WRITES_ENABLED=true` activates the transaction-local hook for
all four wearable entity types. A typed persistence failure rolls back the
legacy mutation. Mapping failures remove only the rebuildable projection and
leave the legacy row visible. Legacy deletion cascades to typed storage.

Bulk projections use bounded `executemany` upserts. The explicit backfill scans
legacy row IDs in configurable batches and commits each completed batch:

```bash
TYPED_WEARABLE_WRITES_ENABLED=true \
  python -m server.typed_wearables backfill --database /data/app.sqlite3 --batch-size 1000
python -m server.typed_wearables compare --database /data/app.sqlite3
```

Re-running any completed or interrupted batch is safe. Comparison output is
value-free: per-domain counts, missing/drift/extra counts, SHA-256 checksums,
ordering and aggregate booleans, plus the mapping version. It never emits a
metric, date/time, identity, provider record ID, or compatibility payload.

## Feature flags and rollback

| Flag | Behavior |
|---|---|
| `TYPED_WEARABLE_WRITES_ENABLED` | Maintain typed projections and permit explicit backfill. |
| `TYPED_WEARABLE_SHADOW_READS_ENABLED` | Run supported typed and legacy queries, log value-free parity/latency, and return legacy rows. |
| `TYPED_WEARABLE_READS_ENABLED` | Return typed rows for supported query shapes; unsupported JSON fields/operators fall back to legacy. |

All switches default to false and are independent. With typed reads enabled and
shadowing disabled, supported queries do not touch the JSON store. Rollback
sets the read flag false; no database restore is required. Disabling writes
stops projection maintenance but deletes neither typed nor legacy rows.

Before production backfill or shadowing, create and verify an off-volume
backup, enable writes first, run backfill and comparison, inspect every
mismatch, and retain legacy rows. H2's private per-domain approval, latency,
and signing gates are defined in
[Production dual-write validation](DUAL_WRITE_VALIDATION.md). H3 owns selective
typed-read cutover; I10 enables neither during deployment.
