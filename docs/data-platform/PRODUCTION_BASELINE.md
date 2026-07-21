# Production baseline

Measured read-only on 2026-07-21 against the running SQLite volume. The audit
captured counts, field names/types, indexes, plans, and timings only. No entity
values, identities, source identifiers, timestamps, credentials, or settings
were captured.

## Storage

| Item | Measurement |
|---|---:|
| SQLite main file | 82,411,520 bytes |
| SQLite WAL at measurement | 206,032 bytes |
| SQLite shared-memory file | 32,768 bytes |
| Logical SQLite size (`page_count × page_size`) | 82,411,520 bytes |
| Uploaded record files | 359 files / 104,226,669 bytes |
| Total entity rows | 212,977 |

WAL size is transient. A valid backup must capture SQLite consistently and
include uploaded records; copying only the main database file is insufficient.

## Entity counts

| Entity | Rows | Entity | Rows |
|---|---:|---|---:|
| `OuraHeartRate` | 149,058 | `GlucoseReading` | 41,439 |
| `FitbitHeartRate` | 15,478 | `Treatment` | 5,327 |
| `LabResult` | 836 | `FitbitDaily` | 366 |
| `OuraDaily` | 181 | `PeriodLog` | 172 |
| `MedicalRecord` | 63 | `Pattern` | 15 |
| `ChatMessage` | 8 | `HealthMemory` | 6 |
| `Diagnosis` | 4 | `Medication` | 4 |
| `Allergy` | 3 | `Insight` | 3 |
| `AIConversation` | 2 | `CompanionThread` | 2 |
| `WeightLog` | 2 | `DexcomConnection` | 1 |
| `GoogleHealthConnection` | 1 | `HealthProfile` | 1 |
| `HealthSummary` | 1 | `InsuranceInfo` | 1 |
| `NightscoutProfile` | 1 | `OuraConnection` | 1 |
| `UserSettings` | 1 | | |

Seven code-visible types had no production rows: `BugReport`, `DailySummary`,
`FingerstickReading`, `FitbitConnection`, `HistoryEntry`, `SymptomLog`, and
`WeeklySummary`.

## Indexes

```sql
CREATE INDEX idx_entities_type
    ON entities(type);
CREATE INDEX idx_entities_type_ts
    ON entities(type, json_extract(data, '$.timestamp'));
CREATE INDEX idx_entities_type_date
    ON entities(type, json_extract(data, '$.date'));
```

The primary-key auto-index on `entities.id` is also present. There are no
indexes for owner, source, upstream ID, record ID, thread ID, collected date,
or other JSON fields.

## Representative warm-query latency

Five in-process samples were taken in the application container. Timings
exclude JSON decoding in `db.py`, API serialization, network transfer, browser
parsing, and rendering.

| Query | Rows | Median | Maximum | Plan |
|---|---:|---:|---:|---|
| Glucose, latest 5,000 by timestamp | 5,000 | 2.725 ms | 3.274 ms | timestamp expression index |
| Treatment, latest 2,000 by timestamp | 2,000 | 1.066 ms | 1.246 ms | timestamp expression index |
| Fitbit HR, latest 400 by timestamp | 400 | 0.198 ms | 0.277 ms | timestamp expression index |
| Oura HR, latest 5,000 by timestamp | 5,000 | 2.701 ms | 5.171 ms | timestamp expression index |
| Labs, latest 5,000 by collected date | 836 | 1.238 ms | 1.575 ms | type index + temporary sort |
| Oura daily, latest 90 by date | 90 | 0.052 ms | 0.129 ms | date expression index |
| Fitbit daily, latest 120 by date | 120 | 0.056 ms | 0.066 ms | date expression index |

The complete structural field inventory took about 600 ms over 212,977 rows.
That is an audit operation, not a request-path benchmark.

## Reproduction rules

- Run `python -m server.data_audit /data/app.sqlite3` on the same host/container
  class when comparing releases.
- Use the default five repetitions and distinguish cold from warm runs.
- Never commit values, timestamps, identities, source identifiers, or settings.
- Measure API/browser payload latency separately from SQLite latency.
- Re-run before every typed-read cutover and attach deltas to its issue or PR.

## Repository verification baseline

At this audit commit:

- Backend Ruff passes.
- Backend pytest passes (10 tests).
- Frontend Vitest passes (4 files / 7 tests).
- Frontend ESLint passes.
- Frontend production build passes, with the existing large-chunk warning.
- Frontend TypeScript checking is not a green baseline. `npm run typecheck`
  reports pre-existing JavaScript inference/prop errors across the API shim,
  shared UI components, connection pages, Settings, Report, and other pages.
  F0 does not suppress or bulk-fix those errors; F5 must establish a clean,
  enforced typecheck baseline before migration code relies on it as a gate.
