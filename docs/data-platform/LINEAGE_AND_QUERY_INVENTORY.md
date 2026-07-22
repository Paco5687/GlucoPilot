# Lineage and query inventory

## Source-to-entity lineage

| Source / module | Writes | Identity and time assumptions | Replacement behavior |
|---|---|---|---|
| Dexcom Share | `GlucoseReading` | Upstream ID discarded; datetime to UTC; global ±240-second dedup. | Incremental append; earliest-arriving source wins. |
| Dexcom API v3 | `DexcomConnection`, glucose, treatment | Event `recordId` → `ns_id`; EGV ID discarded; naive time treated UTC. | Incremental overlap; connection patched. |
| Nightscout | `UserSettings`, `NightscoutProfile`, glucose, treatment | Treatment `_id` → `ns_id`; UTC timestamps; profile timezone retained. | Normal append/dedup; backfill deletes all Nightscout glucose/treatment first. |
| Tandem Source | `Treatment` | Pump ID → `tandem-{id}`; naive pump time interpreted in configured timezone then UTC. | Two-hour overlap; processor can update/delete events. |
| Glooko | Treatment and optional glucose | GUID → `glooko-{guid}`; naive time currently treated UTC pending verification. | ID plus ±90-second treatment dedup. |
| Oura | connection, daily, heart rate | Daily provider date; exact UTC HR timestamp. | Daily patch; HR append if timestamp absent. |
| Fitbit Web API | connection and daily | Provider date. | Daily patch. |
| Google Health | connection, Fitbit daily/HR | Provider date; intraday HR to UTC minute. | Daily patch; HR append if minute absent. |
| Medical-record upload | file, `MedicalRecord`, audited extraction run/observations, `LabResult` compatibility projection | File SHA-256 plus parser/schema/input versions; every new result preserves original/normalized fields and a page/location when reported. | Reprocess replaces only unverified projections. Approved/edited corrections remain current and new parser output at that source location is superseded. Delete removes the file, compatibility labs, and cascaded audit data. |
| Lively/phone ingest | `PeriodLog` | Local date key and source. | Merge by date; manual rows respected. |
| CSV/Base44 imports | Glucose, treatment, Oura, period | Timestamp normalization; glucose uses the shared repository ±240-second cross-source rule. | Legacy CSV import replaces owner/source=`csv` glucose/treatment, then reimports only non-overlapping glucose. |
| Manual APIs | Fingersticks, profile/weight, diagnoses, meds, allergies, insurance, symptoms, history | Random IDs and user-supplied dates. | User edit/append/delete per catalog. |
| Rule/LLM jobs | Patterns, insights, summary, Companion | Patterns, insights, and summaries carry versioned quality/input hashes; source-record evidence remains future work. | Deactivate/replace/append varies by output; low-quality pattern/insight windows clear stale conclusions. |
| Contradiction rules | Typed run, contradiction, and immutable event rows | Rules version plus canonical input hash; each fingerprint includes both evidence sides. | Re-evaluation changes detection presence only. Human resolution is attributable and is never silently reset. |

Every source is operationally owned by its module and the deployment owner.
There is no persisted source-record or sync-run model, so partial pages,
coverage windows, failures, upstream revisions, and field-level origin cannot
be reconstructed.

## Derived results and missing-data assumptions

| Result | Inputs | Current incomplete-data behavior |
|---|---|---|
| Dashboard | Glucose, treatment, period, daily wearables/HR, fingersticks | Empty states render; partial windows produce metrics without coverage. |
| Patterns | 14 days glucose/treatment + timezone | Requires eligible CGM coverage/freshness; post-meal conclusions also require eligible nutrition coverage. Outputs carry the applicable quality envelopes. |
| Cross-domain insights | 90-day CGM, daily wearables/HR, cycle, insulin | Each candidate requires eligible quality for its actual domains; low-quality inputs are omitted from LLM conclusions and stale derived rows are removed. |
| Insulin estimate | Profile weight, treatment/TDD, glucose | Uses only pump-reported or complete-delivery calculated TDD, with explicit coverage, freshness, data-through, and limitations. Missing weight suppresses per-kg output. |
| Insulin response | Insulin, nearby glucose, optional carbs | Carries CGM and clean-correction sample envelopes; meal-related/stacked events are excluded and low-quality values are withheld from Companion conclusions. |
| Fingerstick stats | Fingerstick + nearest CGM ±15 minutes | Unpaired readings count but do not enter delta statistics; pairing never refreshes. |
| Cycle inference | Oura temperature + existing period logs | Rebuilds only inferred rows; existing date wins; insufficient input returns none. |
| Health Overview | Glucose, treatments, wearables, labs, profile/clinical lists, cycle, symptoms/history | CGM, wearable, and cycle values enter the LLM only when their envelopes are eligible. Invalid/rejected labs are excluded and unverified labs carry explicit qualification. |
| Companion | Overview context, insulin outputs, records, memory/chat, optional trusted web | CGM, pump-TDD, and insulin-response values are gated by their envelopes. Lab/document context identifies machine-extracted results as unverified unless approved/edited. |
| Visit Report | Windowed glucose/treatment plus profile, clinical lists, labs, symptoms/history, insurance | Domain quality remains visible. Invalid/rejected labs are excluded; included machine-extracted labs and the generated prompt carry verification qualification. |
| Contradiction consumers | Pump TDD, paired CGM/meter, labs, cycle timing, immutable source revisions | Every unresolved item retains and displays both sides. Blocking items suppress definitive derived claims until an attributable resolution. |

## Field-family consumer matrix

| Field family | Direct consumers |
|---|---|
| Glucose `value`, `timestamp`, `trend`, `source` | Dashboard charts/metrics, Explorer, Compare, pattern and insight engines, insulin analysis, Overview, Companion dossier, Visit Report, source cursors/dedup. |
| Treatment time/type/event/amount/basal fields | Dashboard timeline/summary, pattern and insight engines, insulin estimate/response, Overview, Companion, Visit Report, connector dedup. |
| Daily wearable date + sleep/readiness/activity/HRV/SpO2 fields | Dashboard and Wearables views, cycle inference (Oura temperature), insights, Overview, Companion, Visit Report. |
| Intraday HR timestamp/BPM/source | Live dashboard and glucose overlay, Wearables, cross-domain daily-HR aggregation. |
| Period date/phase/source/notes | Dashboard, Period Tracker, insights cycle comparisons, Overview, Companion, Visit Report. |
| Fingerstick value/time/CGM/delta | Dashboard marker/logger, discrepancy statistics, insulin/clinical context. |
| Typed glucose/fingerstick identity, canonical/source time, value, source, trend, fixed pair, fingerprint/version | Shadow parity, future typed reads, contradiction evaluation, verified backup manifests. |
| Medical-record status/date/title/file metadata | Records queue/index, delete/reprocess paths, Companion recent-document context. |
| Lab original/normalized name/value/unit/range/flag/specimen/date, page/location, confidence, validation/verification, category/record ID | Records index/charts/matrix/search and source review, Overview, Companion, Visit Report, record cascade/reprocess. |
| Profile/weight/demographic fields | Settings, BMI/age computation, insulin per-kg estimate, Overview, Companion, Visit Report. |
| Diagnoses, medications, allergies, insurance | Settings/dedicated editors, Overview context, Companion, Visit Report. |
| Symptoms and history fields | Journal/history pages, rollups, Overview, Companion, Visit Report. |
| Pattern/insight titles, descriptions, confidence/evidence/status | Overview/dashboard cards and prior Patterns/Insights compatibility consumers. |
| Summary serialized payload and generated time | Overview and Companion dossier. |
| Companion thread/message/memory fields | Companion thread list/history, memory controls, dossier/prompt construction. |
| Connection tokens/status/expiry/last-sync | Connector modules and Connections UI; settings APIs expose redacted metadata. |
| User settings / `app_settings` configuration | Connector setup, scheduler, auth/LLM/feature configuration, history narrative. |
| Bug-report context/GitHub link | In-app reporter persistence and GitHub issue bridge only. |
| Contradiction rule/domain/severity/explanation, both JSON sides, detection/resolution state, actor/history | Contextual Dashboard/Records/Insulin/Cycle panels, Visit Report, Health Summary, Companion, verified backup manifests. |

## Frontend generic-entity queries

| Consumer | Shape | Notes |
|---|---|---|
| `Dashboard.jsx` | Glucose 26k, treatment 5k, period 500, Oura 90, Fitbit 120; then range-filtered queries. | One initial fetch + intentional 60-second refresh. |
| `Records.jsx` | Medical records 200 by creation; labs 5k by collected date. | Full client-side views. |
| `Explorer.jsx` | Glucose and treatment up to 100k each. | Large browser payload. |
| `Compare.jsx` | Glucose in 5k pages. | No formal coverage response. |
| `PeriodTracker.jsx` | Period 5k and Oura 120; period writes/deletes. | User/inference rows share type. |
| `Overview.jsx` | Latest 50 patterns. | Other context via summary API. |
| Connection UI / Lively import | `UserSettings`; period list + 50-row bulk inserts. | Legacy settings and user import. |

`useViewingData` currently returns deployment-owner data; shared-view identity
flags are placeholders. Generic writes require admin, but generic reads do not
automatically inject `owner_email`.

## Backend query shapes

| Shape | Consumers | Index coverage / risk |
|---|---|---|
| Type + timestamp order/range | Dashboard, dedup, analysis, report, source cursors | Timestamp expression index; owner/source residual. Several dedup paths load the whole type. |
| Type + date order | Wearables and period | Date expression index; owner residual. |
| Type + `collected_date` | Lab UI/report | No matching index; temporary sort. |
| Type + owner + source/upstream ID | Connectors/dedup | No owner/source/`ns_id` index or uniqueness. |
| Type + record ID | Lab reprocess/delete | No `record_id` index or foreign key. |
| Type + thread ID + created date | Companion | No composite/expression index. |
| Arbitrary generic JSON filter/sort | Entity API | Can scan/sort any allowed field; server does not cap limits. |

## Destructive and replacement paths

| Workflow | Scope | Recovery today |
|---|---|---|
| Nightscout backfill | All owner Nightscout glucose/treatment | Whole-volume restore or remote re-sync; delete/fetch/insert is not one transaction. |
| Legacy CSV import | All owner/source=`csv` glucose/treatment | Restore or rerun source import. |
| Record reprocess/delete | Unverified child projections refresh; verified corrections are retained; delete also removes uploaded file and audit rows | Reprocess retained file or restore a verified volume backup. Database changes are transactional per audit/projection refresh; filesystem deletion remains outside SQLite. |
| Insight generation | All owner insights | Regenerate; previous output/input not retained. |
| Health summary | All owner summaries | Regenerate; previous output/input not retained. |
| Cycle inference | All owner Oura-inferred periods | Regenerate from Oura. |
| Demo cleanup | Every known demo type | Catalog drift can leave unknown types. |
| Glucose dedup `--apply` | Selected duplicate IDs | Backup restore only; command defaults to dry run. |

## Foundation risks confirmed by F0

1. Registry drift: 34 code-visible types versus 19 generic-API types.
2. No migration ledger/schema version, validation, uniqueness, or foreign keys.
3. Secrets and clinical records use the same generic JSON mechanism.
4. Provenance usually ends at one mutable `source` string.
5. UTC instants, provider dates, local dates, and naive timestamps differ by
   source.
6. Derived outputs lack algorithm/input versions and missing-data contracts.
7. Whole-type dedup scans and large browser queries will scale poorly.
