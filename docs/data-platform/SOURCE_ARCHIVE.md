# Immutable source archive

Status: additive typed foundation, integration writes disabled by default

Implementations: `server/source_archive.py`, migration 3 in
`server/migrations.py`

I1 introduced three typed tables without changing legacy JSON reads or API
schemas. I2 adds immutable normalized evidence links and feature-flagged source
instrumentation:

| Table | Responsibility | Mutability |
|---|---|---|
| `source_records` | Scrubbed canonical JSON payload, source/external identity, observed/received times, parser version, hash, compression sizes, and originating sync run | Updates rejected by a database trigger; retention may delete complete rows |
| `source_files` | Relative reference and SHA-256 for an existing document, with source/time/parser/run metadata; never stores document bytes | Updates rejected by a database trigger; retention deletes only the reference |
| `sync_runs` | Source/parser lifecycle and archived/deduplicated record/file counters | Moves once from `running` to `succeeded`, `partial`, or `failed` |
| `normalized_source_links` | Owner-scoped link from an existing generic entity to one immutable source manifest or file reference | Updates rejected; source retention or entity deletion cascades the derived link |

Foreign keys restrict deletion of a sync run while an archived record or file
still refers to it. Archive writes can share the existing SQLite unit of work
with legacy/canonical writes.

Every row carries the F3 canonical deployment owner ID. Repository queries and
retention operations scope themselves to that owner even though the current
deployment model is single-user.

## Privacy boundary

Payload processing is deliberately ordered:

1. Recursively redact credential-bearing keys such as access/refresh tokens,
   passwords, client secrets, API keys, authorization headers, and cookies.
2. Redact bearer/basic credentials and secret query or assignment values found
   inside strings.
3. Serialize the scrubbed value as canonical sorted JSON.
4. Enforce the uncompressed size limit.
5. SHA-256 hash the scrubbed canonical bytes.
6. Compress them with deterministic gzip (`mtime=0`).

The archive never hashes or compresses an unsanitized payload. This both keeps
credentials out of the raw archive and lets otherwise-identical payloads with
rotated OAuth tokens deduplicate to one row. The original source integration
remains responsible for keeping credentials in the existing secret store.

Unknown Python objects, non-finite JSON numbers, malformed/naive timestamps,
unsafe file paths, invalid hashes, oversized payloads, and writes linked to a
missing or completed sync run are rejected before persistence.

## Deduplication and identity

`source_records` is content-addressed by source type plus the SHA-256 of the
scrubbed canonical payload. A repeated payload returns the existing immutable
row and increments the sync run's deduplication counter. `source_files` uses
source type plus file SHA-256, so a repeated upload points to the first safe
relative file reference and never copies bytes.

Observed time may be absent when a source did not provide it. Received time is
always present and normalized to UTC. Parser versions are mandatory so future
reprocessing can identify the interpretation that produced canonical data.

## Limits, retention, and observability

Defaults are conservative and configurable:

```dotenv
SOURCE_ARCHIVE_ENABLED=false
SOURCE_ARCHIVE_RETENTION_DAYS=90
SOURCE_ARCHIVE_MAX_PAYLOAD_BYTES=2097152
CONNECTOR_PROVENANCE_ENABLED=false
```

`stats()` reports policy values, record count, uncompressed/stored byte totals,
oldest/newest receive times, referenced file bytes, sync-run count, and
deduplication totals. `prune_before(cutoff)` deletes archive payload rows and
file-reference rows older than a reviewed cutoff. It never deletes a document
from the records directory.

## Rollout

Both flags remain false by default. I2 instruments supported sources only when
`SOURCE_ARCHIVE_ENABLED=true` and `CONNECTOR_PROVENANCE_ENABLED=true`. Each
instrumented source:

- starts and finishes a sync run;
- archives provider responses before persistence while keeping secrets outside
  the payload;
- writes raw and legacy/canonical results in one unit of work where practical;
- records a stable parser version; and
- records fetched/created/updated/skipped/failed/stale outcomes and freshness;
- links new normalized rows to an immutable source manifest or file reference;
- prevents partial/failed sources from recording successful freshness; and
- passes the synthetic secret, deduplication, failure, and retention fixtures.

Legacy JSON remains authoritative until later feature-flagged read cutovers.
