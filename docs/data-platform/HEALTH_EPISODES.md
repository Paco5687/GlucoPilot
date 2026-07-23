# Canonical health episodes and medication exposures

Implementation: `server/episodes.py`; migration 16 in
`server/migrations.py`; UI in Symptoms, Settings, and Visit Report.

This ledger represents bounded health episodes and medication-use intervals
without turning temporal proximity into a causal claim. It uses the deployment
owner sentinel and the same SQLite database as all other clinical data.

## Storage contract

| Table | Contract |
|---|---|
| `health_episodes` | Current owner-scoped type, title, origin, proposed/confirmed/dismissed status, exact date or UTC interval, shared confidence envelope, non-causal guard, membership revision, and input hash. |
| `episode_members` | Append-only membership revisions linking symptoms, glucose events, cycle days, treatments, history events, and medication exposures. Every row has observed bounds, source version, temporal relationship, and database-enforced `causation_asserted=0`. |
| `episode_events` | Append-only creation, correction, membership-revision, confirmation, and dismissal history with actor, reason, and before/after state. |
| `medication_exposures` | Medication, dose, formulation, frequency, origin, proposed/confirmed/dismissed status, and bounded or open-ended effective interval. |
| `medication_exposure_events` | Append-only correction and decision history for exposure intervals. |

Episode endpoints use one precision at both boundaries: `YYYY-MM-DD`, a UTC
minute, or a UTC second. Medication exposures may have a null end, meaning
ongoing. A null end is not an inferred stop date.

## Origins and lifecycle

`manual`, `rule`, and `model` origins all enter as `proposed`. The API never
accepts an initial terminal status. An admin may correct a proposed record,
confirm it, or dismiss it with an attributable reason. Terminal records cannot
be silently rewritten. Provider sessions can read but cannot mutate either
ledger.

Rules and models may propose an interval and confidence, but their shared
confidence envelope always disables definitive and causal language. Manual
entries default to `not_assessed` confidence.

## Temporal membership

Supported member sources are:

- `SymptomLog`
- `GlucoseReading`
- `PeriodLog`
- `Treatment`
- `HistoryEntry`
- `MedicationExposure`

The API verifies that every selected source exists for this deployment owner.
Relationships are limited to `within_episode`, `temporal_overlap`, and
`near_episode`. These describe time only. They must not be read as a trigger,
effect, treatment response, diagnosis, or causal edge.

When membership changes, the ledger inserts a complete next revision and
preserves the prior rows. The current episode stores only the active revision
number and an input hash over its interval and canonical member list.

## API and consumers

| Endpoint | Access | Purpose |
|---|---|---|
| `GET /api/episodes` and `GET /api/episodes/{id}` | admin/provider | List or inspect episodes; detail includes immutable events. |
| `GET /api/episodes/candidates` | admin/provider | Return a bounded owner-scoped set of records within a requested interval. |
| `POST /api/episodes` | admin | Create a proposed episode. |
| `PUT /api/episodes/{id}` | admin | Correct a proposed episode or append a membership revision. |
| `POST /api/episodes/{id}/decision` | admin | Confirm or dismiss with a reason. |
| `GET /api/medication-exposures` and `GET /api/medication-exposures/{id}` | admin/provider | List or inspect effective intervals. |
| `POST`, `PUT`, and `/decision` exposure endpoints | admin | Create, correct, confirm, or dismiss exposure intervals. |

Symptoms provides manual date-range creation and explicit temporal-source
selection. Settings provides bounded or ongoing medication exposure intervals.
Visit Report includes both with the non-causal warning. Clinical Evidence
Bundles include overlapping, non-dismissed canonical rows as source-linked
items and preserve their native confidence; rule/model episodes remain derived
items.

## Recovery and regression gates

Verified backup manifests compare all five table counts, episode statuses, and
open-ended exposure counts on a clean restore. Golden tests cover interval
precision, multi-source membership, source ownership, the database causal
constraint, append-only revisions/events, correction and terminal decisions,
provider read-only access, Evidence Bundle inclusion, UI guardrails, and backup
parity.

Rollback to an image that predates migration 16 requires restoring the verified
pre-migration backup. Never edit or remove the migration ledger by hand.
