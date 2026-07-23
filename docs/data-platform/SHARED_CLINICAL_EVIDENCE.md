# Shared clinical evidence for Overview and Visit Report

Status: additive G8 consumer contract

Implementation: `server/clinical_evidence.py`

## Boundary

Overview and Visit Report no longer assemble separate lab, record, analytics,
and contradiction dossiers for generated narrative. Both call one adapter over
Evidence Bundle 2.0 with the same calendar-bounded, content-addressed bundle
portfolio:

- governed analytics claims, budget 50;
- clinical history, budget 50; and
- labs plus medical records, budget 150.

The fixed total budget is 250. Separate scopes prevent a large lab import from
crowding every active governed claim or the available clinical history out of
the dossier. Duplicate contradiction and source references are collapsed by
stable ID/link while the aggregate context identity hashes all three bundle
identities and input hashes.

SQLite remains the only store. The adapter is read-oriented and adds no schema,
database, backfill, or rollout flag. Contradiction rules are refreshed through
the existing governed ledger before the bundle snapshot is read.

High-volume glucose and wearable samples are deliberately not copied into this
clinical dossier. Their report values continue to come from the existing
deterministic domain calculators, with their data-quality envelopes passed as a
separate block. This avoids both an unbounded LLM prompt and any possibility
that generated prose replaces deterministic TIR, GMI, insulin, cycle, sleep, or
activity calculations.

## Public context

`clinical-evidence-context/1.0.0` exposes:

- the content-addressed bundle ID, query, algorithm version, and data version;
- data-quality envelopes supplied by deterministic domain calculators;
- per-domain data-through dates, including an explicit null when no dated
  evidence is available;
- every unresolved in-scope contradiction with both sides intact;
- authenticated normalized-source, original-document, and claim-evidence
  links selected by the bundle;
- active governed Pattern/Insight claim references; and
- missing-data caveats and budget truncation state.

The same frontend component renders this contract on Health Overview and Visit
Report. Existing stored HealthSummary rows do not fabricate this metadata; the
block appears after the next normal or manual regeneration.

## Generated narrative

The reasoning payload contains only an allowlisted clinical subset of each
bundle item's already sanitized data plus deterministic metric blocks.
`InsuranceInfo` is excluded because administrative identifiers cannot support
the generated clinical narrative. `DailySummary`, `WeeklySummary`, and prior
`HealthSummary` items are also excluded to prevent circular narrative evidence.

The model must copy the IDs of supporting items into `evidence_item_ids`.
Server-side validation removes any ID that was not in the selected bundle and
derives openable links only from the retained items. This makes a missing
citation visible and prevents an invented locator from becoming a source link.

Lab safety is inherited from Evidence Bundle 2.0: parser confidence remains
available as extraction metadata, but an unapproved/unedited result is labeled
`unverified`, has `clinically_verified=false`, and carries an explicit
limitation. Reports continue to show both sides of unresolved contradictions
and never select a conflicting value silently.

## Compatibility and rollback

The change is additive to API responses and stored HealthSummary JSON. Older
frontends ignore the new block, and the new frontend hides it for an older
summary that lacks the contract. Rolling back the image restores the prior
consumer behavior without a database restore because G8 adds no migration or
new authoritative data.
