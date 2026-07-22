# Canonical clinical time

I3 adds a rebuildable `canonical_times` sidecar beneath the legacy JSON entity
store. It separates the time of a clinical event from the time an assertion was
recorded and the time GlucoPilot received it. Legacy fields and API responses
remain intact.

## Roles and storage

Each entity has at most one compact sidecar row. Indexed columns preserve its
primary clinical timeline value plus the exact envelope times:

| Role | Meaning |
|---|---|
| `observed` | Biological measurement, specimen collection, or observation time |
| `effective_start` / `effective_end` | Point or interval when a treatment or status applies |
| `recorded` | Time the assertion was authored or last updated |
| `received` | Exact UTC ingestion time in GlucoPilot |

`source_text` preserves the primary legacy value exactly; recorded and received
source text have separate columns. `timeline_at` is populated only when the
primary clinical value resolves to a real UTC instant. Partial calendar values use
`normalized_value`, precision, local date, timezone, and basis without a
synthetic midnight. Duration-bearing treatments derive an explicit effective
end and retain the duration plus `canonical-time/1.0.0` normalizer version.

## DST and uncertainty

- An unambiguous local datetime resolves through its IANA timezone.
- A fall-back fold requires a selected offset or resolution. Without one it is
  stored as `ambiguous` and has no `canonical_at`.
- A spring-forward gap is stored as `nonexistent` and has no `canonical_at`.
- Day-, month-, and year-only values are `partial`, never exact instants.
- Algorithm-estimated dates retain `basis=inferred`.
- Invalid source text remains visible as `invalid`; it is not silently replaced
  by ingestion time.

Additional non-primary source fields are retained in compact JSON on the same
row rather than multiplying high-volume observation rows. Only resolved
clinical roles participate in instant timeline queries. Partial
and unresolved records remain queryable per entity and local date.

## Compatibility and rollout

`CANONICAL_TIME_ENABLED=false` is the default. With the switch off, all legacy
writes and reads behave as before, while reports can derive additive time
metadata in memory. With it on, entity creates and updates atomically replace
their sidecar rows in the same SQLite transaction. Entity deletion cascades to
the derived rows.

Before enabling future typed reads, enable the write switch and backfill:

```bash
CANONICAL_TIME_ENABLED=true python -m server.canonical_time backfill
```

Backfill is idempotent and batches commits. Compare sidecar counts and timeline
fixtures before enabling any consumer. Disabling the switch stops dual writes;
legacy JSON remains the rollback read path. The sidecar may be rebuilt and is
included in verified backup metadata.

## Report behavior

Visit Report lab entries expose `event_time` separately from `ingestion_time`.
The rendered report labels the date as “Collected” and the exact receipt date as
“Imported”. A date-only collection value is displayed as a date, never through
JavaScript timestamp parsing.
