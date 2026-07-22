# Clinical contradiction ledger

Status: additive typed sidecar, enabled by default

Implementations: `server/contradictions.py`, migration 8 in
`server/migrations.py`, and `frontend/src/components/ContradictionPanel.jsx`

The contradiction engine makes conflicting clinical facts visible without
choosing a winner or rewriting either source. It evaluates a bounded 120-day
snapshot, creates stable detection fingerprints, and stores resolution actions
in an append-only audit history. Glucose, pump, lab, cycle, source-revision,
report, Health Summary, and Companion consumers all receive the same unresolved
view.

## Storage and lifecycle

Migration 8 adds three owner-scoped typed tables:

| Table | Purpose |
|---|---|
| `contradiction_runs` | Rules version, canonical input-data hash, run status, counts, and timestamps. |
| `contradictions` | Stable detection key, rule/domain/subject, severity, explanation, both evidence sides, detection state, and attributed resolution state. |
| `contradiction_events` | Append-only detected, not-current, resolved, and reopened state transitions with actor, reason, and before/after state. |

The ledger is additive and does not delete or mutate legacy clinical records.
Re-running an unchanged snapshot is idempotent. If evidence changes, its
fingerprint changes and a new unresolved row is created; an earlier resolution
is never transferred to the new evidence. A rule may change `detection_state`
between `active` and `not_current`, but cannot change `resolution_state`.
Not-current unresolved rows remain visible until a user resolves them.

Contradiction rows cannot be deleted, and contradiction events cannot be
updated or deleted. The immutable source external ID participates only in a
one-way subject hash and is never returned by the contradiction API.

## Version 1 rules

| Rule | Severity and trigger | Scope |
|---|---|---|
| Pump-reported source conflict | Blocking when two complete pump sources report different totals for the same day. | Retains both reported totals; never silently selects one. |
| Reported versus calculated TDD | Warning for a non-rounding mismatch; blocking at at least 5 units or 20% of reported TDD. | Calculated TDD is used only when delivered basal and bolus coverage is complete. |
| CGM versus fingerstick | Review begins at the greater of 20 mg/dL or 20% of meter value; blocking at the greater of 40 mg/dL or 30%. | A visibility signal, not a device-accuracy or treatment claim. The broader reconciliation workflow remains tracked separately. |
| Conflicting lab units | Blocking when the same normalized test, collection date, and specimen have distinct units. | Rejected and superseded observations are excluded; both source observations remain visible. |
| Conflicting lab ranges | Warning when that same identity has distinct ranges within the same unit. | A unit disagreement does not also manufacture a range disagreement. |
| Hormone timing | Warning only when a lab explicitly declares an expected cycle phase and the recorded phase for that date differs. | The engine never invents a clinically preferred phase. |
| Revised source record | Warning when one source type and provider external identity has multiple immutable payload hashes. | Retains both source versions and redacts external identity from output. |

Rules are deterministic and carry `clinical-contradictions/1.0.0`. Synthetic
golden fixtures pin rule output, ordering, fingerprints, and privacy behavior.

## Read, resolution, and AI behavior

`GET /api/contradictions` refreshes the read-through snapshot no more than once
per process per 60 seconds and supports domain filtering. The process lock
prevents overlapping refreshes. Panels are contextual: Dashboard shows glucose
and pump TDD, Records shows lab/timing/source revisions, Insulin shows pump TDD,
and Cycle shows hormone timing. The Visit Report prints the explanation and
both evidence sides.

Admins may resolve with `accepted_left`, `accepted_right`, `both_valid`,
`data_corrected`, or `not_applicable`. Blocking items require a written note.
Every resolution stores the authenticated actor and event history. Reopening
requires a reason. Provider sessions can read the ledger and history but cannot
mutate it.

Visit Report, Health Summary, and Companion prompts receive both sides of every
unresolved item. A blocking contradiction forbids a definitive derived claim;
the model must describe the disagreement and may suggest questions for review.
Event history is omitted from model context to bound prompt size.

## Rollout and rollback

`CONTRADICTION_ENGINE_ENABLED=true` is the default. Set it to `false` and
restart to stop evaluation and hide contradiction API/context output without
removing the migration or any audit rows. Before deploying migration 8, create
and verify an off-volume backup. Restore that backup only for a schema-level
rollback; ordinary feature rollback is the flag plus the prior application
image. Backup manifests and verification include run, contradiction,
unresolved, unresolved-blocking, and event counts.
