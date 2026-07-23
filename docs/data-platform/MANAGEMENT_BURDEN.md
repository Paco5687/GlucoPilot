# Auditable diabetes-management burden

P5 keeps glucose outcomes and the effort required to obtain them as separate
dimensions. The implementation describes recorded work; it does not estimate
unobserved lived burden, establish causation, judge treatment choices, or make
clinical recommendations.

## Ledger and precedence

Migration 20 adds strict, owner-scoped `management_burden_events` and
`management_burden_audit` tables. Events are immutable. A correction appends a
new `origin_kind=correction` row referencing the original; the original and
both audit rows remain available. The latest correction controls calculations
and may change category, duration, interactions, or exclude a duplicate.

Origins remain explicit:

- `observed`: direct source events such as boluses, temporary basal actions,
  pump interactions, fingersticks, and recorded device-change language;
- `inferred`: declared rules such as carbohydrates near an observed low,
  overnight management activity, or manual activity explicitly documented as
  glucose management;
- `manual`: owner-entered work when no source event exists; and
- `correction`: an attributable append-only supersession.

Midnight-to-05:00 activity is only an inferred awakening indicator because
sleep itself is not observed. Rescue carbohydrates require a carbohydrate
record within 20 minutes of glucose below 70 mg/dL. These limitations travel
with the event confidence.

## Calculation

`management-burden/1.0.0` publishes every component's event count, recorded
minutes, interaction count, fixed visible weight, and weighted points. The
measured effort index is capped at 100 and is explicitly named *measured*; it
is not a validated clinical scale. Active-management minutes and interactions
per day remain separately visible.

Source coverage is evaluated for pump treatments, fingersticks, ketones,
rescue-carbohydrate context, overnight management, and activity-for-control.
Unavailable families reduce the shared analytics-confidence score and appear
in a missing-source list. They never contribute a silent zero.

Time in, below, and above range are displayed next to effort but are not score
components. When target-range outcomes coexist with high measured effort, the
UI raises a sustainability-review prompt without claiming the effort caused
the outcome or recommending a treatment change.

## Consumers and authorization

- `GET /api/management-burden` is available to authenticated admin and
  read-only provider sessions.
- Manual events and corrections require admin authorization.
- Dashboard and Visit Report show outcomes, effort, component weights,
  confidence, missing sources, and noncausal language.
- Evidence Bundle 2.4 exposes a bounded, source-linked
  `ManagementBurdenSummary` derived metric.
- Verified backup manifests compare total, observed, inferred, manual,
  correction, exclusion, and audit counts after a clean restore.

All derivation is deterministic and bounded to the requested range. Ordinary
React rerenders do not refetch; explicit Refresh, date-range changes, or
successful writes do.
