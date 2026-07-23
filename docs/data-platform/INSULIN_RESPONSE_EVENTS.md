# Versioned insulin response events

Status: P3, deterministic on-demand derivation, no new storage

Implementation: `server/insulin_response.py`, exposed by `server/insulin.py`

## Contract and semantics

`insulin-response/1.0.0` builds one observational response event for every
non-daily-total insulin treatment with a usable timestamp. An invalid dose,
missing glucose, or sparse response window does not silently disappear: the
event remains present with an explicit hard-exclusion reason.

Each event keeps four meanings separate:

- `observed` contains source snapshots and entity identities for the bolus,
  start/end/nadir glucose, carbohydrates, later boluses, and nearby
  fingerstick context;
- `calculations` contains the declared two-hour response window, CGM coverage,
  start/end/nadir values, time to nadir, glucose changes per unit, and estimated
  IOB;
- `context` contains local time bucket, recorded/imported versus inferred
  cycle provenance, event-time activity/position when actually recorded, and
  separately labeled daily wearable context; and
- `assumptions` and `semantics` state that the window is an association. It
  does not establish bolus causation, insulin resistance, or insulin
  absorption.

The IOB comparison uses linear decay over four hours and prior recorded
boluses. It is not pump-reported IOB and does not model basal insulin, insulin
type, personal action curves, or dose absorption.

## Fixed event rules

The initial algorithm uses:

- a 120-minute response window;
- nearest start/end CGM observations within 10 minutes;
- at least 18 CGM observations in the response window;
- a 0.5 U minimum dose and 100 mg/dL minimum starting glucose for analysis;
- carbohydrates from 20 minutes before through 120 minutes after the bolus;
- prior estimated IOB over four hours and subsequent boluses in the response
  window; and
- optional activity, position, and compression context within 15 minutes.

Hard exclusions are invalid/small doses, missing or low starting glucose,
missing end glucose, and insufficient CGM coverage. Carbohydrates, prior
estimated IOB, later boluses, moderate/vigorous activity, and possible CGM
compression are confounders.

Events are classified as `clean`, `confounded`, or `excluded`. Only `clean`
events have `included_by_default=true` and enter aggregate or stratified
statistics. Confounded and excluded events remain inspectable. Time-of-day,
cycle-phase, activity, and position strata therefore cannot accidentally
reintroduce excluded observations.

## Reproducibility and confidence

Event IDs combine the algorithm version with the canonical bolus identity.
`source_input_hash` identifies each event's contributing observations, and
`input_data_version` hashes canonical order-independent source snapshots plus
the fixed assumptions. Reordering repository results does not change the
result.

The aggregate observed mean uses the shared
`analytics-confidence/1.0.0` sample/effect/interval/missingness/language
envelope. The existing insulin-response quality gate still requires eight
clean, current observations and eligible CGM input before values may enter AI
reasoning. The UI and Visit Report show all event counts and reasons even when
that quality gate blocks an aggregate.

Evidence Bundle 2.2 derives `InsulinResponseEvent` items for bounded insulin
queries. Every item links to its normalized bolus, glucose, carbohydrate,
subsequent-bolus, estimated-IOB-contributor, and contextual-fingerstick
sources. Derived events never replace those source facts.

## Storage, backup, and rollback

P3 adds no table, migration, rollout flag, or second database. Events are
rebuilt on demand from authoritative Treatment, GlucoseReading, PeriodLog,
daily wearable, and FingerstickReading rows. The existing source-data backup
and restore contract is sufficient; there is no new durable count to add to a
backup manifest.

Rollback is an application-image rollback. Reverting the code removes the
derived presentation without transforming or deleting source data.
