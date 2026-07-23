# Platform self-diagnostics

H1 adds one privacy-safe operational view across source synchronization, data
quality, derived projections, storage, and visible backups. The authenticated
`GET /api/diagnostics` endpoint and the Diagnostics page use
`platform-diagnostics/1.0.0`.

Diagnostics describe whether GlucoPilot has current, usable inputs. They are
not health findings, do not assess the owner, and must not be interpreted as a
clinical status. The boundary is part of the machine-readable contract:

```json
{
  "semantics": {
    "category": "operational_diagnostics",
    "not_health_findings": true
  }
}
```

## Source status

The source list covers Dexcom, Dexcom Share, Nightscout, Tandem Source, Glooko,
Oura, Fitbit, Google Health, medical-record uploads, and cycle records. Each
entry reports only operational metadata:

- configured and governed-versus-legacy tracking state;
- latest successful sync and source data-through times;
- source-specific freshness age and limit;
- import lag when both times exist;
- latest governed run status and aggregate counters; and
- stable issue codes with non-sensitive explanations.

`inactive` means no connection, governed run, or dated source data is known.
`current` means dated data fall within the source's limit. `stale` means dated
data exist but exceed that limit. `warning` means the latest outcome was partial
or freshness is uncertain. `error` means the latest governed run failed or a
configured source has no dated data.

Ordinary records such as uploads can become current without being configured.
Source failures remain explicitly categorized as `source_health`; they are
never presented as changes in the user's health.

## Platform status

The remaining blocks expose bounded aggregates:

- data quality: failed/partial runs, failed or skipped/deduplicated items,
  parser failures, unverified/invalid records, and unresolved canonical times;
- graph: feature state, latest published projection, and build freshness;
- analytics: latest Pattern/Insight generation and shared-confidence gaps;
- storage: database and WAL byte sizes; and
- backup: age of the newest checksummed manifest visible to the application and
  the number of unreadable manifests.

Backup status proves only that a manifest's own checksum is valid and readable
at diagnostic time. It does not verify the adjacent database/record payloads
and does not replace an off-volume backup, clean-target restore verification,
or the operator runbook in
[Backup and rollback](BACKUP_AND_ROLLBACK.md).

Overall status is `critical` when any operational caveat has critical severity,
`warning` when only non-critical caveats exist, and `healthy` otherwise.

## Companion and report use

The shared clinical evidence adapter and question-ranked Companion adapter
receive a reduced diagnostics context. It contains source label/status,
last-success/data-through time, freshness, safe issue text, and the explicit
non-medical semantics. Visit Report renders the same per-source data-through
block.

Backup caveats, storage sizes, run counters, paths, and manifest details are not
sent to model reasoning. Source-staleness and ingestion caveats are included so
the Companion and generated report cannot silently reason as though missing or
old source data were current.

## Authorization and privacy

The endpoint requires a logged-in owner or read-only provider session. It
returns no entity or run IDs, owner identity, health values, filenames, backup
paths, checksums, credentials, token fields, source URLs, or raw parser/sync
errors. The frontend performs one initial fetch and refreshes only when the user
presses Refresh; it does not poll. Navigating away aborts any in-flight request.

Regression fixtures are fully synthetic and verify aggregation, source
staleness propagation, owner/provider authorization, privacy exclusions,
rerender stability, explicit refresh, and unmount cancellation.
