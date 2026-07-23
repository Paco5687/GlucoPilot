# Entity catalog

## Shared storage contract

All entities currently share one table:

```text
entities(id TEXT PRIMARY KEY, type TEXT, data TEXT, created_date TEXT, updated_date TEXT)
```

`id` is a random application UUID. UTC `created_date`/`updated_date` envelope
timestamps come from `server/db.py`; same-named JSON keys supplied by features
are discarded. All other data is unvalidated JSON. The deployment is
single-owner and code normally adds `owner_email`, but SQLite does not enforce
ownership, schema, source identity, uniqueness, or references.

The generic entity API allows the 19 names in `db.ENTITY_TYPES`, while backend
modules write 15 more through dedicated APIs. The registry therefore exposes
19 of 34 code-visible types and cannot be treated as the full schema catalog.

A `?` below marks an optional field. Schemas combine current code with the
value-free production field/type inventory.

## Measurements and source records

| Entity | Schema / owner | Identity, time, and mutation | Missing-data behavior |
|---|---|---|---|
| `GlucoseReading` | Owner; `value`, `timestamp`, `trend`, `source`. | Global ±240-second timestamp dedup across Dexcom/Share/Nightscout/Glooko/imports; no upstream ID or DB constraint. Intended UTC. Append except source backfills/dedup maintenance. | Invalid times are skipped; consumers omit missing values with no coverage record. |
| `Treatment` | Owner; `timestamp`, `source`, `type`, `event_type`, `amount?`, `duration?`, `absolute?`, `insulin_type?`, `glucose?`, `glucose_type?`, `notes?`, `ns_id?`. | Upstream `ns_id` where available; Glooko/imports also use same-type ±90 seconds. Intended UTC. Append/update/delete; Nightscout backfill replaces its source rows. | Missing amount/type is skipped. Missing basal/daily totals cannot be distinguished from zero delivery. |
| `FingerstickReading` | Owner; `timestamp`, `value`, `cgm_value?`, `delta?`, `note`, `source=manual`. | Random ID; repeats retained. Nearest CGM within ±15 minutes is fixed at creation. User-deletable. | No nearby CGM produces a valid unpaired row excluded from delta statistics. |
| `PeriodLog` | Owner; `date`, `phase`, `source`, `notes`. | Intended owner + local date; imports merge by date and manual rows beat inference. Oura inference replaces only its own rows. | Missing days/phases are absence, not explicit unknown intervals. |
| `OuraDaily` | Owner; `date` plus optional sleep, readiness, activity, temperature, SpO2, and HR metrics. | Application upsert by date; provider-local calendar date. Existing day is patched as endpoints return fields. | Each metric is optional and joins silently skip unavailable fields. |
| `OuraHeartRate` | Owner; `timestamp`, `bpm`, `source=oura`. | Application exact-timestamp dedup; UTC; normally append-only. | Invalid timestamps skipped; sparse coverage is not recorded. |
| `FitbitDaily` | Owner; `date`, `source`, optional steps, active minutes, resting/non-REM HR, HRV, breathing, sleep, and SpO2 metrics. | Application upsert by date; Fitbit and Google Health patch the same type. | Optional metrics are skipped; row-level `source` cannot express field-level provenance. |
| `FitbitHeartRate` | Owner; `timestamp`, `bpm`, `source`. | Google Health uses exact UTC minute; no DB constraint; normally append-only. | Invalid timestamps skipped; consumers show empty state without coverage metadata. |
| `MedicalRecord` | Owner; `filename`, `stored_as`, `content_hash`, status/upload fields, extracted title/type/date/summary/page/lab/extraction counts, partial/error and extraction versions. | SHA-256 rejects byte-identical upload. Random stored filename. Deletion removes the file, compatibility labs, and cascaded audit rows. | Partial batch failures remain explicit; field-level state lives in the audit tables. |
| `LabResult` | Compatibility projection with normalized numeric value/unit/range/flag/date/category, record ID, specimen, source page/location, parser confidence, validation and verification state, original printed fields, and audit observation ID. | Current UI/API compatibility row. Unverified rows refresh on reprocess; approved/edited rows survive and supersede subsequent parser output for the same stable source location. | Qualitative/titer results remain in audited observations rather than being misrepresented as ordinary numeric trends. Invalid results are visibly qualified and excluded from summaries. |
| `SymptomLog` | Owner; title/description/severity/duration/time-of-day/entry-date. | Random ID; user append/delete; local user date. | Optional text becomes empty string; no not-asked state. |
| `WeightLog` | Owner; `weight_kg`, `date`. | Appends on profile weight change; no date uniqueness; UTC calendar date. | No interpolation or coverage metadata. |

Migration 7 also adds typed `lab_extraction_runs`, versioned
`lab_extraction_observations`, and append-only `lab_verification_events`.
These tables are the audit record; `LabResult` remains the compatibility view.

Migration 8 adds typed `contradiction_runs`, `contradictions`, and append-only
`contradiction_events`. These rows reference both sides of a disagreement but
do not replace, delete, or silently choose between the source entities. They are
not exposed through the generic entity API.

Migration 9 adds strict `glucose_readings` and `fingerstick_readings`
projections. The latter retains the paired CGM entity, value, timestamp, source,
and signed delta captured at entry. Invalid compatibility rows remain visible
only in legacy storage and appear as unmappable in parity reports.

Migration 10 adds strict `wearable_daily` and `wearable_samples` projections.
Fitbit and Google Health rows remain separate even on the same day; provider
overlap is explicit rather than silently merged. Known metric presence and
unknown compatibility fields round-trip, while invalid rows remain in legacy
storage and are reported as unmappable.

## Clinical profile and narrative records

| Entity | Schema | Identity / mutation | Missing-data behavior |
|---|---|---|---|
| `HealthProfile` | Owner singleton; height, weight, date of birth, sex, units. | First owner row patched. | BMI/age null when inputs are absent/invalid. |
| `Diagnosis` | Owner; name, diagnosed date, active/resolved status, notes. | Random ID; append/delete. | Confirmed conditions only. Legacy `suspected` rows remain readable but are excluded from diagnosis evidence and surfaced through the hypothesis ledger. |
| `Medication` | Owner; name, medication/supplement kind, dose, frequency, active/stopped status, notes. | Random ID; append/delete. | Legacy catalog row has no interval; migration 16's medication-exposure ledger records actual effective ranges separately. |
| `Allergy` | Owner; allergen, reaction, severity, notes. | Random ID; append/delete. | Empty optional values retained. |
| `InsuranceInfo` | Owner singleton; carrier/plan/member/Rx/contact/effective-date fields and notes. | First owner row patched; card extraction is preview-only until save. | Unread fields empty; confidence/source location not stored. |
| `HistoryEntry` | Owner; title, kind, entry date, details. Standing narrative is in `app_settings`. | Random ID; append/delete. | Undated entries retained and sorted last. |
| `NightscoutProfile` | Owner + profile name; timezone, units, DIA, carb absorption and serialized basal/ISF/carb-ratio/target schedules. | Application upsert by profile name. | Missing profile means unavailable; schedule provenance/version absent. |

## Connections and credentials

| Entity | Schema | Identity / mutation |
|---|---|---|
| `DexcomConnection` | Owner, access/refresh tokens, type, expiry, connected, last sync?. | Singleton application upsert/token refresh. |
| `OuraConnection` | Owner, access/refresh tokens, expiry, connected. | Singleton application upsert/token refresh. |
| `FitbitConnection` | Owner, access/refresh tokens, expiry, connected. | Singleton; declared but absent in measured production after Google Health migration. |
| `GoogleHealthConnection` | Owner, access/refresh tokens, expiry, connected. | Singleton; active but absent from generic API allowlist. |

Passwords, API keys, cursors, feature configuration, and the standing health
narrative can also live in `app_settings` under `cfg_*`. It is a separate
configuration/secret store requiring backup and redaction controls.

## Derived and conversational records

| Entity | Schema / producer | Identity and incomplete-data behavior |
|---|---|---|
| `Pattern` | Rule engine + optional LLM; compatibility fields plus governed confidence, claim contract/version/key/status, algorithm/input versions, EvidenceSet ID, and supersession IDs. | New generations append. Migration 14's strict ledger retains predecessor/successor lineage; exact CGM and optional treatment windows are openable. Requires eligible 14-day CGM quality; small samples remain exploratory. |
| `Insight` | Cross-domain engine + optional LLM; compatibility fields plus governed confidence, claim contract/version/key/status, algorithm/input versions, EvidenceSet ID, and supersession IDs. | New generations append instead of replace-all delete. Exact CGM plus candidate-specific wearable/cycle/treatment windows are linked; a metric with seven paired days remains exploratory and eligible 28-pair correlations report temporal replication. |
| `HealthSummary` | Overview synthesis; generated time, serialized data, owner. | Singleton-by-replacement. Empty context sections omitted; no immutable input snapshot/version. |
| `HealthMemory` | Companion memory; content, category, source, owner. | Append/delete free text without evidence or verification state. |
| `CompanionThread` | Title, owner, envelope dates. | Random ID; application-only message cascade on delete. |
| `ChatMessage` | Thread ID, role, content, optional web sources, owner. | Append until thread delete; sources are not internal evidence links. |
| `AIConversation` | Legacy title, serialized messages, context summary?, archived flag. | Current Companion does not use it; production legacy rows lack owner. |
| `DailySummary` | Declared legacy type; no active writer or production row. | Undefined schema; establish or retire before migration. |
| `WeeklySummary` | Declared legacy type; no active writer or production row. | Undefined schema; establish or retire before migration. |

Migration 14 also adds strict `claim_algorithm_registry` and `claim_versions`
tables plus evidence roles/rationales. They are not generic API entities; JSON
Pattern/Insight rows remain the compatibility surface and source observations
remain authoritative.

Migration 15 adds strict `health_hypotheses`, append-only
`hypothesis_evidence`, and append-only `hypothesis_events`. These rows are
owner-scoped and use a dedicated guarded API rather than generic entity CRUD.
They keep patient/algorithm/clinician proposals distinct from `Diagnosis`,
retain supporting/opposing/missing evidence revisions, and require an
attributable clinician decision for confirmed or ruled-against status.

Migration 16 adds strict `health_episodes`, `episode_members`,
`episode_events`, `medication_exposures`, and `medication_exposure_events`.
These dedicated, owner-scoped API records preserve date/instant ranges,
manual/rule/model origins, proposed/confirmed/dismissed decisions, append-only
membership and correction history, and database-enforced temporal-only
semantics. They do not use generic entity CRUD and do not replace the source
symptom, glucose, cycle, treatment, history, or medication rows they reference.

Migration 19 adds strict, append-only `activity_position_intervals` and
`activity_position_events`. Dedicated owner-scoped APIs record manual
activity/position ranges and corrections; timestamped wearable step intervals
may add low-confidence walking inference. Manual rows take query-time
precedence without updating or deleting inferred rows. Daily wearable totals
remain separate context rather than event-time position evidence.

## Operational records

| Entity | Schema | Identity / notes |
|---|---|---|
| `UserSettings` | Owner plus legacy Nightscout URL/secret/connected/last-sync fields. | First row patched. Overlaps `app_settings`; precedence is feature-specific. |
| `BugReport` | Owner; title, description, page, role, status, GitHub URL/number, created time. | Append-only fallback/audit record; automatic context excludes health data. |

## Migration order

1. Separate secrets/config from clinical entities.
2. Register schemas and ownership for all 34 types.
3. Normalize source identity, UTC instant, local date/timezone, and ingestion
   metadata before typed clinical tables.
4. Migrate high-volume observations/treatments first with dual writes and
   shadow reads.
5. Add document/version evidence before labs and derived outputs.
6. Replace application-only dedup with explicit idempotency constraints.
