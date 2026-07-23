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

## Evidence balance

Every evidence item has exactly one role:

- `supporting` — evidence consistent with the hypothesis;
- `opposing` — evidence consistent with another explanation or against it;
- `missing` — a named gap or verification step that remains unresolved.

`weighted-evidence-v1` recalculates the displayed score for each complete
evidence revision:

```text
supporting weight / (supporting + opposing + missing weight)
```

The score is explicitly an evidence-balance measure, not a diagnostic
probability. Each revision stores a canonical SHA-256 input version. Event
before/after states make score changes attributable to the exact evidence
revision and reason.

## Product surfaces

- Settings shows hypotheses in an amber, “not a diagnosis” ledger separate
  from confirmed conditions.
- Each entry shows supporting, opposing, and missing evidence, origin,
  evidence revision, review time, verification suggestion, and terminal
  reviewer when present.
- The Visit Report prints the same three evidence sides and guardrail in a
  section separate from confirmed diagnoses.
- Legacy `Diagnosis` rows with `status=suspected` are excluded from confirmed
  condition context and Evidence Bundles. They appear as low-confidence legacy
  hypotheses until re-entered in the governed ledger.

## Backup and graph boundaries

Verified backups compare hypothesis, evidence, event, status, and decision
counts after a clean restore. The ledger introduces no second database and no
new graph source of truth. Future graph edges derived from hypotheses remain
rebuildable SQLite projections under the existing relationship controls.
