# Auditable lab and medical-record extraction

Status: additive compatibility rollout
Migration: 7, `auditable_medical_record_extraction`
Writer switch: `LAB_EXTRACTION_AUDIT_ENABLED` (default `true`)

## Safety contract

An extracted result is a source fact produced by a parser, not a clinical
interpretation. Parser confidence estimates whether the model read the source
as intended; it does not verify the laboratory, reference interval, specimen,
or clinical meaning. Results remain `unverified` until an administrator
compares them with the source and approves or edits them.

The extraction contract preserves both the exact reported representation and a
normalized representation:

- name, value, unit, reference range, flag, specimen, and collection date;
- numeric, qualitative, or titer value kind;
- 1-based source page and visible extraction location;
- parser/schema/input-data versions and parser confidence; and
- validation issues, verification state, correction history, and supersession.

The source document stays under `/data/records`; audit tables store its SHA-256
identity and location metadata, not a second document copy.

## Storage and compatibility

`lab_extraction_runs` records each parser attempt and its exact input hash,
versions, status, page count, batch failures, and timestamps.

`lab_extraction_observations` stores every extracted result version. Original
fields never change when a normalized correction is made. An edit creates a new
observation linked with `supersedes_observation_id`; the old version becomes
`superseded` and points to the new version.

`lab_verification_events` is append-only at the database level. Approval,
editing, and rejection record the actor, reason, before/after normalized
payloads, and timestamp. Update and delete triggers reject history mutation.

`LabResult` remains the generic-API compatibility projection so existing lab
charts and consumers continue to work. Only numeric results project into it.
Qualitative values and titers stay available in source review without being
misrepresented as trendable numbers.

## Deterministic validation

Validation is structural and deliberately not a diagnosis or clinical range
opinion. It detects:

- non-finite numeric values and inverted reference bounds;
- qualitative values and titers that require non-numeric handling;
- repeated extraction at the same source location;
- conflicting units for the same normalized test/date/specimen;
- a specimen field that conflicts with an explicit specimen in the printed
  name; and
- invalid pages, confidence values, and unknown normalized flags.

Invalid reference bounds are preserved as original text but removed from the
numeric compatibility range. Invalid and rejected results are excluded from
Health Overview, Companion, and Visit Report summaries. Warning/unverified
results may remain visible but carry their limitation and verification state.

## Reprocessing behavior

Reprocessing creates a new immutable run. Existing unverified compatibility
rows are refreshed. An approved or edited result is matched by its stable
source key (record, page, location, original name/specimen/date/category) and
retained unchanged; new parser output for that location is stored as
superseded. If a later parser cannot reproduce the same key, the verified result
is still retained rather than silently destroyed.

The audit refresh and compatibility-row mutations share one SQLite transaction.
The LLM call and document rendering occur before that transaction, so a parser
failure leaves the prior current results intact and marks the run failed.

## Review workflow

On Health Records, open Documents and select the review icon. The dialog:

1. previews the original document at the extracted page;
2. switches between exact reported and normalized representations;
3. shows extraction location, parser confidence, validation issues, and state;
4. allows administrators to approve, edit, or reject; and
5. exposes a direct source-document link for independent browser viewing.

Lab trend rows also link to their source page and visibly label unverified or
invalid extraction state. Read-only providers can inspect the evidence but
cannot change verification.

## Rollout and rollback

Migration 7 is additive and does not rewrite existing `LabResult` JSON. Existing
rows therefore appear as unverified legacy results until their document is
reprocessed. Keep `LAB_EXTRACTION_AUDIT_ENABLED=true` for normal operation. A
temporary `false` value restores the legacy replacement writer without dropping
audit tables or history; do not use it while reviewing/reprocessing documents
whose verified corrections must be preserved.

Before deployment, create and independently verify the required off-volume
backup. Backup manifests include extraction-run, observation, verified-version,
and verification-event counts so restoration detects lost audit history.
