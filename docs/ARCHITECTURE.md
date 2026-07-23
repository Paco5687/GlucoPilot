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
| `relationship_projection.py` | versioned full/scoped graph jobs, atomic generation publication, checksums, watermarks, and freshness |
| `relationship_api.py` | authenticated read-only neighbor, reverse-neighbor, bounded traversal, and redacted evidence-path APIs |
| `evidence_sets.py` | bounded checksum-addressed observation windows, claim evidence sets, drill-down verification, and source-change invalidation |
| `evidence_bundle.py` | authenticated cross-domain evidence queries, deterministic ranking/versioning, protected contradictions, source links, and content-addressed caching |
| `clinical_evidence.py` | shared Overview/Visit Report Evidence Bundle consumer, quality/data-through/source/claim blocks, and validated narrative evidence citations |
| `glucose_reconciliation.py` | pure, versioned CGM/fingerstick pair derivation, contextual strata, directional-bias sample gates, and checked-low semantics |
| `lab_audit.py` | audited medical-record extraction, validation, verification, correction history, and compatibility projection |
| `contradictions.py` | deterministic cross-domain contradiction rules, typed ledger, attributed resolution workflow, and API |
| `auth.py` | first-run setup, login, **admin vs. read-only provider role**, `require_admin` |
| `entities.py` | generic entity REST API (writes gated to admin) |
| `settings_api.py` | in-app settings & secrets (DB-stored, override env) |
| `llm.py` | provider-agnostic `invoke_llm(...)`; Anthropic + local; vision + `tier="quality"` routing |
| `companion_evidence.py` | bounded question-ranked Evidence Bundle retrieval, statement classification, citation validation, and change comparison |
| `hypotheses.py` | guarded patient/algorithm/clinician hypothesis ledger, evidence revisions, attributable confidence, and clinician-gated decisions |
| `episodes.py` | canonical health and medication-exposure intervals, append-only temporal membership revisions, non-causal semantics, and guarded decisions |
| `management_burden.py` | immutable observed/inferred/manual effort ledger, append-only corrections, visible weighted components, source-coverage confidence, and outcome-versus-effort views |
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

Migration 13 adds the G3 build lifecycle. A versioned projector derives the
four governed lab/record and message/thread edges from current authoritative
entity references. Runs, failures, input watermarks, active-edge membership,
and graph checksums stay in SQLite. A complete full or entity-scoped output is
published in one transaction; a failed run leaves the prior active graph and
freshness state untouched. Independently authored assertions are not projector
members and therefore survive rebuilds.

G4 adds no schema. Its authenticated GET-only API reads the active projection
under `RELATIONSHIP_READS_ENABLED`, fixes owner scope server-side, hashes
free-form provenance locators, and enforces fixed depth/item/path/expansion
budgets with deterministic ordering.

G5 also adds no schema. Its authenticated Evidence Bundle API assembles direct
observations, derived metrics, active relationships, documents, contradictions,
source links, missing-data caveats, and confidence under one strict query
budget. The canonical query plus exact public-content hash identifies each
bundle and its bounded process-local cache; blocking contradictions are kept
outside ranking so they cannot be displaced by a small item budget.

G6 adds no schema or database. A versioned analytics-confidence contract gives
Patterns, cross-domain Insights, range-local dashboard correlations, reports,
Health Summary, and Evidence Bundles the same sample/effect/interval,
missingness, temporal-direction, discovery, and replication semantics. A
seven-day result remains exploratory regardless of effect size; only a
same-direction later temporal holdout may use `reproduced`. Cycle analytics
retain explicitly recorded/imported versus algorithm-inferred phase-day counts.

Migration 14 adds the G7 Pattern/Insight claim ledger in the same SQLite
database. New governed generations append instead of deleting history, record
stable semantic keys plus algorithm/input/content versions, link predecessor
and successor claims, and cite role-preserving bounded EvidenceSets over the
exact source entities. Authenticated claim and window endpoints provide paged
source drill-down; JSON entities remain the compatibility surface and all
rollout flags default off.

G8 and G9 add no schema. Overview and Visit Report share one bounded clinical
Evidence Bundle adapter, while Companion uses a smaller question-ranked
portfolio with strict local-model prompt limits. Companion messages persist
typed claim-to-evidence/source links, keep personal inference separate from
general medical references and user memory, and expose owner-checked evidence,
opposition, and content-hash change commands. Invented citations and uncited
personal claims fail closed; unverified machine-extracted labs are qualified
before display or persistence.

Migration 15 adds G10's guarded hypothesis ledger in the same SQLite database.
Patient, algorithm, and clinician proposals remain separate from confirmed
diagnoses; supporting, opposing, and missing evidence is revisioned
append-only; deterministic evidence-balance changes are recorded in immutable
events; and terminal decisions require an attributable clinician review.
Settings and Visit Report render hypotheses with an explicit “not a diagnosis”
guardrail. No graph data becomes authoritative.

Migration 16 adds P1's canonical health episodes and medication exposure
intervals. Date or UTC ranges, manual/rule/model origin, proposed/confirmed/
dismissed status, append-only membership revisions and events, shared
confidence, and owner-validated source references remain in the same database.
Temporal relationships are database-enforced as non-causal. Symptoms, Settings,
Visit Report, and clinical Evidence Bundles expose the ledger without making it
a graph source of truth.

P3 adds no schema. `insulin-response/1.0.0` deterministically derives
observational response windows from canonical treatments and glucose, retaining
invalid, under-covered, and confounded events with explicit reasons. Only clean
events enter time/cycle/activity/position strata. Shared analytics confidence,
the Insulin page, Visit Report, and Evidence Bundle 2.2 preserve the boundary
between source facts, calculations, associations, and resistance/absorption
interpretations. The events are rebuilt on demand and add no backup surface.

P4 adds immutable activity/position intervals and correction events in SQLite.
Manual observations take precedence over overlapping wearable inference while
both remain auditable. Timestamped intervals support glucose, morning,
bolus-response, and fingerstick discrepancy comparisons under the shared
confidence contract. Evidence Bundle 2.3 exposes quantified effects, and
Companion receives only effects that pass the explicit sample/status/confidence
gate.

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
