# CGM and Fingerstick Reconciliation

P2 adds deterministic, non-causal comparisons between a manually recorded
fingerstick and the nearest CGM observation within 15 minutes. The two
observations remain separate. Logging or deleting a fingerstick never changes a
CGM entity, and later CGM imports do not rewrite the fixed pair snapshot.

## Capture contract

`POST /api/functions/fingerstick` with `action: add` stores:

- both values, timestamps, source, CGM entity identity, CGM trend, and signed
  pair offset;
- signed CGM-minus-meter delta, absolute difference, relative difference,
  deterministic 15 mg/dL-or-15% comparison band, and algorithm version;
- separate `<70` and `<54` classifications: confirmed by both observations,
  CGM-only, meter-only, or neither;
- optional timing context, sensor day/site, activity, position, hydration,
  possible compression, and a bounded context note.

Context is optional and collapsed in the dashboard so the ordinary path remains
value, time, and Log. Enums and bounds are enforced server-side.

## Aggregate semantics

`action: stats` returns the same versioned reconciliation definitions used by
the Visit Report:

- mean signed and absolute differences;
- persistent directional bias only after at least five pairs and only when the
  95% interval for the mean signed difference excludes zero;
- sample count, interval, discovery status, and shared analytics-confidence
  version;
- strata by CGM trend, sensor-day band, sensor site, position, and activity;
- checked-low counts separated into confirmed, CGM-only, and meter-only.

Strata with small samples are descriptive only. No comparison is presented as
causal or as proof that one measurement source is clinical truth.

## Time-below-range uncertainty

The Visit Report continues to calculate raw TBR from CGM samples. Meter-checked
low counts are displayed alongside it, never used to correct or reweight it.
This is intentionally not a “confirmed TBR”: sporadic meter checks cannot
estimate how much of the full CGM trace was or was not a true low.

Evidence Bundles retain the direct fingerstick observation, the fixed CGM
snapshot, reconciliation version, low semantic class, and explicit limitations.
They do not assign an overall clinical-confidence score to a pair.

## Storage and rollback

Legacy JSON remains authoritative. Migration 18 extends the rebuildable strict
`fingerstick_readings` projection with reconciliation and context columns.
Typed glucose feature flags retain their existing rollback behavior; this work
does not enable any rollout flag. Verified backups already include both the
authoritative entities and typed table count, so clean-target restore parity is
unchanged.
