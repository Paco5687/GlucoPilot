# Architecture

## Overview

```
Browser ──► FastAPI (server/) ──► SQLite (one JSON entity store)
                │                        ▲
                ├── serves built React SPA (frontend/dist)
                ├── session auth (admin + read-only provider role)
                ├── per-source sync modules ──► external APIs
                ├── background scheduler (periodic syncs)
                └── LLM layer ──► Anthropic API  OR  local OpenAI-compatible server
```

Single container, single Docker volume (`/data`) holding the SQLite DB,
uploaded records, and config. One owner per deployment.

## Backend (`server/`)

| Module | Responsibility |
|---|---|
| `main.py` | app assembly, SPA serving, router registration, lifespan |
| `db.py` | SQLite JSON entity store (`query/create/update/delete`, Mongo-style filter ops); config store |
| `migrations.py` | ordered, checksummed, transactional SQLite schema migrations |
| `schema_registry.py` | canonical entity metadata and generic-API exposure registry |
| `backup.py` | WAL-consistent backups, checksummed manifests, and clean-target restore verification |
| `data_contracts.py` | additive v1 identity, time, provenance, assertion, and data-version contracts |
| `repositories.py` | swappable domain interfaces and legacy JSON-backed adapters |
| `unit_of_work.py` | atomic transaction boundary shared by repository implementations |
| `source_archive.py` | scrubbed, compressed immutable source payloads, file references, sync runs, retention, and size metrics |
| `connector_provenance.py` | feature-flagged connector/upload lifecycle, fetch outcomes, freshness, and normalized evidence links |
| `canonical_time.py` | canonical event/effective/recorded/received time normalization, DST handling, and sidecar repository |
| `typed_treatments.py` | feature-gated strict treatment, basal-segment, and pump-total projections, backfill, and parity checks |
| `typed_glucose.py` | strict glucose/fingerstick projections, repository dedup, backfill, shadow parity, and read rollback |
| `typed_wearables.py` | strict wearable daily/sample projections, provider overlap, bounded backfill, shadow parity, and rollback |
| `relationship_registry.py` | migration-governed predicates, assertion statuses, evidence levels, and relationship algorithms |
| `relationships.py` | strict owner-scoped temporal relationship projection, immutable identity, indexed queries, and compatibility cutover |
| `lab_audit.py` | audited medical-record extraction, validation, verification, correction history, and compatibility projection |
| `contradictions.py` | deterministic cross-domain contradiction rules, typed ledger, attributed resolution workflow, and API |
| `auth.py` | first-run setup, login, **admin vs. read-only provider role**, `require_admin` |
| `entities.py` | generic entity REST API (writes gated to admin) |
| `settings_api.py` | in-app settings & secrets (DB-stored, override env) |
| `llm.py` | provider-agnostic `invoke_llm(...)`; Anthropic + local; vision + `tier="quality"` routing |
| `scheduler.py` | background sync loop, per-source intervals |
| `readings.py` | shared cross-source glucose dedup |
| **Sources** | `dexcom.py`, `dexcom_share.py`, `nightscout.py`, `tandem.py`, `glooko.py`, `oura.py`, `fitbit.py` |
| **Analysis** | `patterns.py`, `insights.py`, `cycle_inference.py`, `report.py` |
| **Records** | `records.py` (upload → vision extraction → audited observations → `LabResult` compatibility projection) |
| **Ingest** | `ingest.py` (token-auth push endpoint for phone automations) |
| **Imports** | `import_legacy.py`, `import_base44_export.py`, `dedup_readings.py`, `reset_password.py` |

### Function dispatch

The frontend calls backend "functions" via `POST /api/functions/{name}` (a
compatibility shape). Names map to source/analysis handlers in `functions.py`.

### Entity model

All records live in one `entities` table as JSON documents keyed by `type`.
Notable types: `GlucoseReading`, `Treatment`, `Pattern`, `Insight`,
`AIConversation`, `PeriodLog`, `OuraDaily`, `OuraHeartRate`, `FitbitDaily`,
`MedicalRecord`, `LabResult`, plus per-source connection records. This mirrors a
flexible document API, which keeps sync/analysis code simple.

Migration 8 adds a typed contradiction sidecar. It never rewrites either
clinical source: stable rule detections and append-only resolution events are
surfaced in contextual panels, reports, summaries, and Companion prompts.

Migration 11 adds the first relationship-graph projection in the same SQLite
database. Edges remain rebuildable and subordinate to source entities; governed
registries, owner/type validation, attribution, input/algorithm versions, and
temporal/confidence indexes prevent anonymous or untyped graph data.

## Frontend (`frontend/`)

React + Vite + Tailwind + shadcn/ui, charts via Recharts, canvas Explorer chart.
`src/api/base44Client.js` is a thin adapter exposing `auth`/`entities`/`functions`/
`integrations` over the backend's REST API. Role (`isAdmin`/`isProvider`) drives
nav and control visibility. Built to `frontend/dist` and served by the backend.

## LLM layer

`invoke_llm(prompt, response_json_schema=?, images=?, tier=?)`:

- **provider** = `anthropic` | `local` (Settings page).
- **images** → routes to a vision-capable model (lab extraction).
- **tier="quality"** + text-only + a configured report model → routes to a larger
  local model (the Visit Report narrative); everything else uses the fast default.

See [LOCAL_MODELS.md](LOCAL_MODELS.md).

## Sync & dedup

The scheduler polls each connected source on its own interval. Glucose inserts
from every source pass through a global ±4-minute dedup (`readings.py`) so
overlapping feeds (Share + official API + Nightscout) never double-store a
reading. Treatments dedup by source id and per-type time windows.

See the [data platform audit](data-platform/README.md) for the complete entity
catalog, source lineage, query inventory, production baseline, and migration
gates.
