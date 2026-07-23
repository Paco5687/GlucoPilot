# Provider annotation and owner review audit

Migration 21 adds a dedicated clinical-review layer. Providers remain unable
to mutate source entities, canonical projections, settings, hypotheses, or
corrections. Their permitted write surface is limited to attributable review
events: annotation, mark reviewed, hypothesis confirmation or rejection,
correction confirmation, and a question.

`clinical_review_threads` stores current owner-scoped state for one target.
`clinical_review_events` stores every provider and owner action with actor
identity and role, UTC time, target, reason, Evidence Bundle identity when
supplied, and complete prior/new state. Database triggers reject event updates
and deletes.

Provider actions reset owner state to `pending`. The owner may append an
acceptance or dispute. A later decision changes current state but does not
remove the clinician action or any earlier dispute.

## API and authorization

| Route | Authorization | Behavior |
|---|---|---|
| `GET /api/clinical-reviews` | owner/provider | Bounded current threads with immutable event history. |
| `GET /api/clinical-reviews/{id}` | owner/provider | One owner-scoped thread and history. |
| `POST /api/clinical-reviews/actions` | provider only | Append a permitted review action; never update the target. |
| `POST /api/clinical-reviews/{id}/owner-decision` | owner only | Append accept/dispute and retain provider history. |

The Clinician Brief exposes provider controls alongside evidence and
hypotheses. Owner controls accept or dispute a review with a required reason.
The previous frontend no-op audit helper now calls these server endpoints.

## Companion semantics

Review context is content-addressed with the Companion Evidence Bundle
portfolio. Only a non-disputed `hypothesis_confirm` provider action appears in
`clinician_confirmed_facts`. Other reviews are explicitly
`provider_annotation`, and disputed reviews remain in a separate protected
list. The system prompt forbids promoting annotations or disputed entries to
clinician-confirmed fact.

## Rollback

Before migration 21, create and independently restore-verify an off-volume
backup. Code rollback is compatible because old code ignores these dedicated
tables. Do not delete tables or audit events during code rollback. Restore the
verified pre-migration backup only if a full data rollback is explicitly
required.
