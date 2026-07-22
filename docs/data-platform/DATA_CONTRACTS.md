# Clinical data contracts v1.0.0

Status: **additive contract, pending review through F3**  
Python reference: `server/data_contracts.py`  
Contract name: `glucopilot-clinical-data-contracts`  
Contract version: `1.0.0`

These contracts define the meaning required by future typed tables. They do
not validate, rewrite, dual-write, or change reads from the current JSON entity
store. A typed schema must not be added until its mapping below is reviewed in
the F3 pull request and the later schema migration has its own review.

## Non-negotiable invariants

1. GlucoPilot remains a single-owner deployment. The canonical owner is
   `urn:glucopilot:owner:self`; it is not an email address or a tenant key.
2. An internal canonical ID, an upstream source-record ID, and a legacy row ID
   are different identifiers. One must never silently replace another.
3. Observed, recorded, received, and effective time are different facts. A
   missing value in one role cannot be filled from another without marking the
   result inferred.
4. Date-only, month-only, year-only, unknown, inferred, ambiguous-DST, and
   nonexistent-local-time values remain explicit. Date-only data is never
   converted to synthetic midnight.
5. Source fact, patient report, derived statistic, hypothesis, and clinician
   confirmation are distinct assertion kinds. Confidence and evidence do not
   change an assertion kind or status.
6. Every derived statistic, and every algorithm-created hypothesis, names the
   algorithm version and the version or immutable snapshot of its inputs.
7. Existing legacy data remains readable. Contract adoption is additive and
   feature-gated until a separately reviewed cutover.

## Canonical identity

### Owner

`urn:glucopilot:owner:self` is a stable sentinel for the one deployment owner.
`APP_OWNER_EMAIL` remains a legacy compatibility value and display/login
identity; it is not a canonical clinical-data identity. This contract does not
introduce organizations, tenants, owner tables, cross-owner queries, or shared
records.

### Entity IDs

`canonical_entity_id(entity_type, stable_local_id)` derives a UUIDv5 in a
fixed GlucoPilot namespace and emits:

```text
urn:glucopilot:<entity-type>:<uuid>
```

The entity type is part of the derivation, so identical local IDs in different
tables cannot collide. Existing legacy entity IDs are valid stable inputs. New
application-created records generate a local UUID once and retain the derived
canonical ID for their lifetime. The fixed UUID namespace
`6988cb42-b1a7-5bd1-b91f-fcf5a2bd8da6` must never change.

Provider IDs use `canonical_source_record_id(source_system, source_record_id)`
and are stored as source-record identities, not as the clinical entity primary
key. This permits one clinical event to retain multiple source assertions and
prevents a provider rename or merge from changing the entity ID. Callers use a
stable source-system registry key rather than a display name; changing that key
changes the derived source-record ID.

Identity policies used by the entity map are:

| Policy | Meaning |
|---|---|
| `application_record` | A user/system-created record receives one immutable local ID. |
| `source_or_legacy_record` | Preserve a legacy mapping; future source rows also retain upstream identity separately. |
| `owner_singleton` | One logical record for `owner:self`, with an immutable canonical ID. |
| `owner_natural_key` | A stable owner-scoped key such as provider day, profile name, or content hash. |
| `parent_scoped` | Identity includes an immutable parent relationship plus a stable child key. |
| `derived_output` | Identity covers an algorithm run/output version, not merely its display title. |
| `legacy_undefined` | No new writes until the legacy type is specified or retired. |

## Time semantics

| Role | Definition | Examples |
|---|---|---|
| `observed` | When the biological measurement, symptom, specimen collection, or source observation occurred. | CGM sample, heart rate, lab collection, symptom day. |
| `recorded` | When a person, device, clinician, or algorithm authored or amended the assertion. | Note entry, algorithm generation, source update. |
| `received` | When GlucoPilot first ingested the representation. This is an exact UTC system instant. | JSON envelope `created_date`, upload receipt. |
| `effective` | The point or interval during which an event, status, profile, medication, or assertion applies. | Bolus time, insurance start, diagnosis date, medication exposure interval. |

The current `created_date` and `updated_date` envelope maps to received and
recorded metadata. It must not stand in for a missing observed or effective
time.

### Precision and partial time

`PartialTime` preserves one of `second`, `minute`, `hour`, `day`, `month`,
`year`, or `unknown`, plus an explicit basis: `exact`, `patient_reported`,
`source_reported`, `inferred`, or `unknown`.

- Exact instants are ISO 8601 and normalized to UTC (`Z`/`+00:00`).
- Provider or patient calendar dates stay `YYYY-MM-DD` with day precision.
- A known month or year stays partial; it is not expanded into an arbitrary
  first day.
- Unknown time is `{value: null, precision: unknown, basis: unknown}`. Absence
  is not silently interpreted as unknown.
- An estimated or computed date uses `basis: inferred` even if its string is
  day-precise.

### Time zones and DST

Local timestamps retain an IANA time zone and explicit resolution state:

- `unambiguous` with the selected UTC offset;
- `ambiguous_earlier_offset` or `ambiguous_later_offset` with the chosen offset
  for a fall-back fold;
- `unresolved` when the fold cannot be selected;
- `nonexistent_local_time` for a spring-forward gap.

An unresolved or nonexistent local time cannot claim a UTC offset or canonical
instant. It remains queryable as a source assertion but is excluded from
instant ordering until corrected. A date-only value may carry a zone for
calendar interpretation but never requires DST resolution.

Examples:

```text
2026-07-21T17:05:00Z                 exact UTC minute
2026-07-21                            source-reported local day
2026-07                               patient-reported month
2026-11-01T01:30 America/New_York     ambiguous; earlier/later/unresolved required
2026-03-08T02:30 America/New_York     nonexistent local time; no instant invented
null                                  explicit unknown only with unknown precision/basis
```

`EffectiveTime` distinguishes a point, closed interval, open-ended interval,
and unknown interval. Medication exposure and other longitudinal facts must
adopt an interval instead of overloading a mutable active/stopped flag.

## Provenance and assertion semantics

### Source classes

`device_or_provider`, `clinical_document`, `clinician`, `patient`, `import`,
`system`, `algorithm`, and `external_knowledge` describe who or what made the
assertion. A display string such as `source=manual` is not enough to establish
this class or identify the source record.

### Assertion kinds

| Kind | Meaning | Must not be presented as |
|---|---|---|
| `source_fact` | Faithful representation of a device, provider, document, or import field. | Clinician-confirmed truth merely because it came from a chart. |
| `patient_report` | What the deployment owner reported, logged, remembered, or selected. | Device measurement or clinician confirmation. |
| `derived_statistic` | Reproducible computation over versioned inputs. | Source measurement or medical conclusion. |
| `hypothesis` | Tentative interpretation proposed by a rule, model, person, or clinician. | Diagnosis, confirmed causal claim, or derived statistic. |
| `clinician_confirmation` | A clinician explicitly confirmed the assertion. | Generic medical-document extraction or patient report. |

Assertion status is orthogonal: `unverified`, `provisional`, `confirmed`,
`disputed`, `refuted`, `superseded`, or `entered_in_error`. For example, a
patient report can remain `confirmed` as an accurate report of what the person
said without becoming a clinician confirmation.

### Evidence and confidence

Evidence levels are `none`, `assertion_only`, `source_record`, `corroborated`,
and `clinician_reviewed`. Levels that claim evidence require immutable evidence
IDs. Clinician confirmation requires clinician source class and
clinician-reviewed evidence. A document-extracted lab is a source fact with a
document/page evidence link; it is not clinician-confirmed merely because the
document is clinical.

Confidence is separate metadata: `not_assessed`, `low`, `medium`, or `high`,
with an optional score from 0 to 1. Any assessed confidence names its method
and, when applicable, calibration version. Confidence never upgrades evidence,
assertion kind, or assertion status.

## Algorithm and data versions

Every typed record carries:

- contract name and semantic version;
- typed schema version;
- source revision when the provider exposes one;
- immutable input snapshot ID and/or SHA-256 of canonical input data;
- for algorithm outputs: algorithm ID, semantic version, implementation commit,
  parameter hash, and model ID/version when a model is used.

Changing computation logic, prompts, calibration, material parameters, or an
LLM model requires a new algorithm version. Re-running the same algorithm over
new inputs requires a new input snapshot/version, not necessarily a new
algorithm version. Derived outputs may be superseded but are not mutated in a
way that erases their original algorithm and input version.

## Relationship projection contract

Projected relationships reuse these primitives rather than treating an edge as
an anonymous subject/predicate/object tuple. Every edge records the deployment
owner, registered node types, governed predicate, assertion kind/status,
evidence level, source class/identifier, deterministic generator ID/version,
immutable input-data version/hash, validity, confidence, and generation time.

Predicate registration fixes its allowed subject/object types and inverse.
Assertion-status, evidence-level, predicate, and relationship-algorithm
registries are migration-owned and validated at startup. A new vocabulary term
or algorithm version therefore requires an additive migration, not an ad hoc
runtime insert.

Projection identity includes generator and input-data version. The same input
is idempotent; new input produces a new row, preserving the prior generation
for later supersession or complete rebuild. See
[Governed relationship projection](RELATIONSHIP_GRAPH.md).

Bounded Evidence Sets apply the same rule to time series: query definition,
ordered membership, canonical observation checksum, summary, generator, and
input-data version are recorded once per window. Sample observations remain
source entities and are opened on demand. A checksum mismatch invalidates the
window rather than silently changing the evidence behind a claim. See
[Evidence sets and observation windows](EVIDENCE_SETS.md).

## Entity-level destination map

All 34 registered entity types map to the v1 primitives. Envelope
`created_date` and `updated_date` always map to received and recorded time and
are omitted from the time column below for brevity.

| Domain / entity | Identity | Domain time | Allowed assertion/source |
|---|---|---|---|
| Glucose / `GlucoseReading` | source or legacy | `timestamp` observed UTC | source fact; device/provider or import |
| Insulin / `Treatment` | source or legacy | `timestamp` effective UTC | source fact or patient report; device/provider, import, or patient |
| Legacy / `DailySummary` | undefined | none defined | versioned derived statistic only; algorithm |
| Legacy / `WeeklySummary` | undefined | none defined | versioned derived statistic only; algorithm |
| Analytics / `Pattern` | derived output | first/last detected times recorded UTC | derived statistic or hypothesis; algorithm |
| Analytics / `Insight` | derived output | `date_generated` recorded UTC | derived statistic or hypothesis; algorithm |
| Companion / `AIConversation` | undefined | envelope only | patient report or hypothesis; patient, system, or algorithm legacy source |
| Cycle / `PeriodLog` | owner + local date | `date` observed partial date | source fact, patient report, or derived statistic; device/import, patient, or algorithm |
| Insulin / `NightscoutProfile` | owner + profile name | envelope only until an effective interval exists | source fact; device/provider or import |
| Connections / `OuraConnection` | owner singleton | expiry effective; last sync recorded | source fact; device/provider or import |
| Wearables / `OuraDaily` | owner + provider date | `date` observed day | source fact; device/provider or import |
| Wearables / `OuraHeartRate` | source or legacy | `timestamp` observed UTC | source fact; device/provider or import |
| Settings / `UserSettings` | owner singleton | envelope only | source fact or patient report; patient or system |
| Connections / `DexcomConnection` | owner singleton | expiry effective; last sync recorded | source fact; device/provider or import |
| Records / `MedicalRecord` | owner + content hash | record date effective partial; upload received UTC | source fact; clinical document |
| Records / `LabResult` | record + stable source key + extraction version | original and normalized collection dates remain distinct partial observations | source fact from a clinical document; `unverified` until approved/edited against source, never clinician-confirmed from parser confidence alone |
| Connections / `FitbitConnection` | owner singleton | expiry effective; last sync recorded | source fact; device/provider or import |
| Wearables / `FitbitDaily` | owner + provider date | `date` observed day | source fact; device/provider or import |
| Wearables / `FitbitHeartRate` | source or legacy | `timestamp` observed UTC | source fact; device/provider or import |
| Typed wearable daily projection | canonical legacy entity + explicit provider | provider-local observed date | source fact; device/provider or import; mapping version and legacy fingerprint required |
| Typed wearable sample projection | canonical legacy entity + explicit provider | canonical observed UTC plus original source timestamp/local date | source fact; device/provider or import; strict BPM unit/value |
| Glucose / `FingerstickReading` | application record | `timestamp` observed UTC | patient report; patient |
| Typed glucose projection | canonical legacy entity/source identity | canonical observed UTC plus original source timestamp and local date | source fact; device/provider or import; mapping version and legacy fingerprint required |
| Typed fingerstick projection | canonical application identity | canonical observed UTC plus fixed paired-CGM time when present | patient report; patient; paired CGM remains a separate source fact |
| Connections / `GoogleHealthConnection` | owner singleton | expiry effective; last sync recorded | source fact; device/provider or import |
| Profile / `HealthProfile` | owner singleton | date of birth observed partial | patient report; patient |
| Profile / `WeightLog` | application record | `date` observed day | patient report; patient |
| Clinical / `Diagnosis` | application record | diagnosed date effective partial | patient report or clinician confirmation; matching patient/clinician source |
| Clinical / `Medication` | application record | envelope only until exposure interval is added | patient report or clinician confirmation; matching patient/clinician source |
| Clinical / `Allergy` | application record | envelope only | patient report or clinician confirmation; matching patient/clinician source |
| Clinical / `InsuranceInfo` | owner singleton | effective date partial | source fact or patient report; document, clinician, or patient |
| Clinical / `SymptomLog` | application record | entry date observed partial | patient report; patient |
| Clinical / `HistoryEntry` | application record | entry date effective partial | patient report; patient |
| Analytics / `HealthSummary` | derived output | generated time recorded UTC | versioned derived statistic; algorithm |
| Companion / `HealthMemory` | application record | envelope only | patient report or hypothesis; patient or algorithm |
| Companion / `CompanionThread` | application record | envelope only | system source fact or patient report; system or patient |
| Companion / `ChatMessage` | parent-scoped | envelope only | patient report or algorithm hypothesis; patient or algorithm |
| Operations / `BugReport` | application record | envelope received/recorded | patient report; patient |
| Reliability / contradiction ledger | detection fingerprint | first/last detected recorded UTC | versioned rule detection retaining both source assertions; resolution is a separate attributed user action |
| Relationship projection | owner + generator/version + input-data version + projection key | point, interval, open-ended, or unknown validity | governed assertion/evidence/source metadata; deterministic rebuildable algorithm required |

Mixed-source legacy types such as `PeriodLog`, `Treatment`, `Diagnosis`, and
`HealthMemory` choose assertion/source metadata per row; the allowed set is not
a default inference from entity type.

## Review gate for typed schemas

Before a later typed-table issue can use this contract, its pull request must
show:

- canonical-ID derivation and legacy-ID mapping;
- all four time roles, partial precision, basis, timezone, and DST behavior;
- row-level source class, assertion kind/status, evidence, and confidence;
- source-record linkage and immutable evidence IDs;
- schema/data/algorithm versions for derived outputs;
- behavior for missing, disputed, superseded, and entered-in-error data;
- a shadow-read comparison and rollback path under the migration checklist.

Clinical review should focus on assertion labels and display behavior. A source
fact may be accurately transcribed yet clinically wrong; a patient report may
be faithfully represented without clinician verification; and a high-confidence
hypothesis must remain visibly tentative.
