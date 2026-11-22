# Guarded health-hypothesis ledger

Implementation: `server/hypotheses.py`; migration 15 in
`server/migrations.py`.

G10 keeps tentative health ideas structurally separate from confirmed
`Diagnosis` entities. A hypothesis may originate with the patient, an
algorithm, or a clinician, but every new entry starts as `proposed`. Companion
or analytics code may call the internal algorithm entrypoint; that entrypoint
cannot select a diagnosis or terminal status.

## Storage and lifecycle

The same SQLite database contains three strict tables:

| Table | Purpose |
| --- | --- |
| `health_hypotheses` | Current owner-scoped title, origin, status, evidence-balance score, evidence version, review time, verification suggestion, and attributable terminal decision. |
| `hypothesis_evidence` | Append-only evidence revisions with supporting, opposing, or missing roles; source identity/version, bounded summary, weight, and approved source link. |
| `hypothesis_events` | Append-only creation, evidence-revision, review, status, and archive history with actor, reason, before/after state, and evidence input version. |

Evidence rows and events cannot be updated or deleted. Replacing evidence
creates a complete new revision; older revisions remain replayable. Hypothesis
rows cannot be deleted and can be hidden only through an attributable archive
transition.

Allowed transitions are:

```text
proposed -> under_review -> confirmed
                         -> ruled_against
proposed/under_review -> archived
```

Terminal rows cannot be reopened or silently revised. `confirmed` and
`ruled_against` require an authenticated admin action that explicitly records
`decision_authority=clinician`, a reviewer identity, and a rationale. Read-only
provider sessions can inspect the ledger but cannot mutate it. P7 provider
confirm/reject actions live in the separate clinical-review audit and never
rewrite this guarded hypothesis ledger.

The dedicated API surface is:

| Route | Authorization | Behavior |
| --- | --- | --- |
| `GET /api/hypotheses` | admin or provider | List current entries with complete current evidence roles. |
| `GET /api/hypotheses/{id}` | admin or provider | Return one entry plus its immutable event history. |
| `POST /api/hypotheses` | admin | Create a tentative proposal. The request cannot choose a later status. |
| `PUT /api/hypotheses/{id}/evidence` | admin | Append a complete evidence revision and confidence event. |
| `POST /api/hypotheses/{id}/transition` | admin | Apply a guarded lifecycle transition; terminal decisions require clinician attribution. |
| `POST /api/hypotheses/{id}/lab-suggestions` | admin | Suggest lab evidence (read-only; see below). |
| `GET /api/hypothesis-labs` | admin | Stored lab lookups for open hypotheses: per hypothesis, labs on file and tests not on file that are not yet in its evidence; which are still waiting; whether the queue is running. |
| `POST /api/hypothesis-labs/queue` | admin | Start the background lookup queue (no-op when it is running or nothing is waiting); returns the same status. |

## Evidence state and audit score

Every evidence item has exactly one role:

- `supporting` — evidence consistent with the hypothesis;
- `opposing` — evidence consistent with another explanation or against it;
- `missing` — a named gap or verification step that remains unresolved.

`weighted-evidence-v1` recalculates an audit-compatible score for each complete
evidence revision:

```text
supporting weight / (supporting + opposing + missing weight)
```

The score is explicitly not a diagnostic probability and is retained for
revision replay and audit compatibility. Product surfaces do not present it as
a percentage. Instead, they show an evidence state:

- `not_evaluated` when no supporting or opposing evidence is recorded;
- `preliminary` when only one supporting or opposing item is recorded;
- `leans_supportive`, `mixed`, or `leans_against` when at least two supporting
  or opposing items are available;
- the terminal clinician states `confirmed` and `ruled_against`.

Missing evidence names a gap. It does not count as opposing evidence when the
product determines the evidence state. Each revision stores a canonical
SHA-256 input version. Event before/after states make score changes
attributable to the exact evidence revision and reason.

## Lab suggestions

Implementation: `server/hypothesis_labs.py` (`hypothesis-labs/1.3.0`).

The endpoint writes nothing to the ledger. It asks the local model two questions at
temperature 0, with the hypothesis, its notes, and the active confirmed
conditions and medications: whether the hypothesis is usually checked with
lab tests (and how it is usually diagnosed), and which lab tests, which way
each usually reads if the hypothesis is true, and any listed condition or
medication that moves the test that way by itself. The second question is
asked either way; for a hypothesis usually diagnosed without lab tests the
response sets `lab_tests_used` to false and the panel says so above the list.
The questions stay separate because a single prompt with a "none" option made
the model list nothing. The call site
`hypothesis_labs` is not on the Breeze allowlist, so it always stays local.

The model never sees lab values. Named tests are matched to LabResult rows by
`analyte_key` (exact, then ignoring spaces, then allowing assay-variant words
such as "hs"); rows are filtered and de-duplicated as the Visit Report does.
Each match becomes a suggested `entity` evidence item for its latest draw,
linked to the source document, with a summary built from the stored value,
unit, date, flag and reference range. The suggested role follows the expected
direction and the result's flag; it is left undecided when the flag is not a
standard one, the direction is "any", or a listed condition or medication
could produce the result. Unmatched named tests (imaging and panels excluded)
become suggested `missing` items. The owner ticks items and picks a role for
each lab; the client saves them with the existing evidence-revision route,
built on the stored evidence it reads just before saving.

### Stored lookups and the queue

The model's answers (the triage answer and the test list) are
stored per hypothesis in `app_settings` under `hypothesis_labs:<id>`, with a
fingerprint of the method, the model and the prompt. A later request with the
same fingerprint reuses them and makes no model call; values, dates, flags and
"already in evidence" are rebuilt from the current lab rows and evidence each
time. A new title or description, a change to the active conditions or
medications, or another model gives a new fingerprint and a new lookup. One
lookup runs per hypothesis at a time; a panel request and the queue wait for
each other and share the stored answer.

The queue looks up every proposed or under-review hypothesis with no current
stored answer, one at a time, as its own task. The scheduler starts it every 5
minutes (only while background sync is enabled), the Settings page starts it
after adding a hypothesis, and a dashboard addition opens its panel, which
looks it up directly. A failed lookup waits an hour before the queue tries it
again. Each pass deletes stored answers for hypotheses that are no longer
open. The stored answers are derived data: deleting them only means the next
lookup asks the model again.

## Product surfaces

- The dashboard's Hypotheses card lists open hypotheses and adds new ones; a
  new entry opens its lab suggestions, and each open entry has "Find labs".
- Settings shows hypotheses in a “not a diagnosis” ledger separate from
  confirmed conditions, with a qualitative evidence state and all three
  evidence roles.
- Each entry shows supporting, opposing, and missing evidence, origin,
  evidence revision, review time, verification suggestion, and terminal
  reviewer when present.
- The Visit Report keeps evidence-bearing questions separate from confirmed
  diagnoses. Supporting, opposing, and missing details are expandable.
- Legacy `Diagnosis` rows with `status=suspected` are excluded from confirmed
  condition context and Evidence Bundles. With no governed supporting or
  opposing evidence, they appear collapsed as “previously recorded” and are
  excluded from print by default until re-entered in the governed ledger.

## Backup and graph boundaries

Verified backups compare hypothesis, evidence, event, status, and decision
counts after a clean restore. The ledger introduces no second database and no
new graph source of truth. Future graph edges derived from hypotheses remain
rebuildable SQLite projections under the existing relationship controls.
