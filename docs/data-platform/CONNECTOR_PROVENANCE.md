# Connector and upload provenance

Status: additive implementation, disabled by default

Implementations: `server/connector_provenance.py`, migration 4 in
`server/migrations.py`

I2 gives every supported connector and upload one common lifecycle without
changing existing entity JSON, existing API response fields, scheduling
intervals, or source-specific persistence behavior. Enabled responses add only
the provenance run ID and outcome.

## Covered executions

| Source | Scheduled | Manual/backfill | Raw boundary | Normalized domains |
|---|---:|---:|---|---|
| Dexcom API | yes | yes | EGV and event responses | glucose, treatments |
| Dexcom Share | yes | yes | follower-feed readings | glucose |
| Nightscout | yes | yes | entries, treatments, meter entries, profiles | glucose, treatments, profile |
| Tandem Source | yes | yes | decoded pump event submitted by the connector library | treatments |
| Glooko | yes | yes | each paginated clinical response | glucose, treatments |
| Fitbit Web API | yes | yes | each clinical endpoint response | wearable daily |
| Google Health | yes | yes | each data-type page | wearable daily and heart-rate samples |
| Oura | yes | yes | each paginated endpoint response | wearable daily and heart-rate samples |
| Medical records | n/a | upload/reprocess | stored file hash/reference | record and extracted labs |
| Cycle ingest | n/a | authenticated ingest | parsed JSON/CSV source rows | period log |

OAuth exchanges, refresh responses, connection rows, and credentials are not
clinical source evidence and are deliberately excluded. Insurance-card image
extraction currently returns an unsaved review draft and creates no normalized
record; it remains outside this link layer until a reviewed save operation
provides a durable target.

## Complete outcomes and freshness

Migration 4 extends `sync_runs` with run/trigger/connector versions; fetched,
created, updated, skipped, failed, and stale counts; and
`last_successful_data_at`. Every enabled execution finishes as:

- `succeeded`: no provider fetch failed; the latest observed source time may
  advance freshness;
- `partial`: at least one provider response/file was captured or normalized
  write completed, and at least one fetch/processing step failed; freshness
  remains unset for that run; or
- `failed`: no provider response/file was captured and no normalized write
  completed before the source returned or raised a failure; freshness remains
  unset.

Provider helpers that historically treated optional endpoint failures as
best-effort now report them to the shared run while preserving their existing
user-facing behavior. Legacy `last_sync` settings advance only when the active
provenance run has no source failure.

## Evidence links

The active context observes creates and updates only for the source's clinical
entity allowlist. Provider responses are secret-scrubbed and archived before
the associated persistence phase. Large result sets are split into bounded
250-record chunks so backfills remain within the archive size policy. At
completion, one immutable manifest lists the raw source-record IDs for the
run; each written entity receives a `normalized_source_links` row to that
manifest. File-backed uploads link to the immutable `source_files` reference
instead.

This keeps compatibility JSON untouched and bounds link growth: a normalized
row has one run manifest link rather than one link per paginated response. Raw
payloads remain independently deduplicated and openable through the manifest.

Links carry owner, entity type/ID, source evidence, sync run, parser version,
and link time. Database triggers reject updates. Links are derived metadata, so
deleting a retained source record/file or deleting its normalized entity
cascades the corresponding link; raw source rows themselves remain immutable
until reviewed retention deletes the complete row.

## Rollout

```dotenv
SOURCE_ARCHIVE_ENABLED=false
CONNECTOR_PROVENANCE_ENABLED=false
```

Connector provenance activates only when both flags are true. With either flag
off, no context or sync run is created and every existing connector follows its
pre-I2 code path. Rollout should enable a source in a restore-rehearsed instance,
inspect complete/partial outcomes and archive growth, and retain legacy JSON as
the authoritative read path.
