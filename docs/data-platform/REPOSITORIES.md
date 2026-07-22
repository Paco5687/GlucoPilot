# Domain repositories and unit of work

Status: additive legacy adapter layer
Implementations: `server/repositories.py`, `server/unit_of_work.py`

F4 separates clinical consumers from the physical JSON entity table without
changing stored data, APIs, or query results. The first implementation delegates
to the legacy entity functions; later typed-table implementations can replace
individual repositories behind reviewed feature flags.

## Boundaries

Core analytics depend on `RepositoryCatalog`, not `db.query_entities`:

| Interface | Current implementation |
|---|---|
| `GlucoseRepository` | `GlucoseReading` compatibility repository plus feature-gated strict typed projection and repository-owned ±240-second dedup |
| `FingerstickRepository` | `FingerstickReading` compatibility repository plus strict paired-reading projection |
| `TreatmentRepository` | `Treatment` JSON entities |
| `BasalSegmentRepository` | Strict feature-gated `basal_segments` sidecar |
| `PumpDailyTotalRepository` | Strict feature-gated `pump_daily_totals` sidecar |
| `LabRepository` | `LabResult` JSON entities |
| `LabAuditRepository` | Migration-7 extraction runs/observations/events plus the `LabResult` compatibility projection |
| `ContradictionRepository` | Migration-8 deterministic detections, both evidence sides, detection state, resolution state, and immutable history |
| `WearableRepository` | Oura/Fitbit/Google Health compatibility repositories plus feature-gated strict daily/sample projections |
| `RelationshipRepository` | Read-only projection of lab→record and message→thread references |
| `EvidenceRepository` | Read-only projection of Pattern/Insight inline support and ChatMessage sources |
| `SourceArchiveRepository` | Typed immutable source payload/file metadata, sync runs, outcome counters, freshness, and normalized links |
| `ClinicalTimeRepository` | Atomic sidecar synchronization, per-entity time metadata, and cross-source canonical timeline queries |
| `EntityRepository` | Compatibility adapter used for remaining registered domains |

Every entity adapter preserves the generic filter, sort, limit, skip, envelope,
and mutation behavior used by the existing API. The generic entity routes still
call `server.db` directly, so F4 does not expand or restrict API exposure.

I4 wraps the legacy Treatment adapter with `TreatmentCompatibilityRepository`.
Mutations always retain the legacy behavior. Supported domain reads may use the
strict typed projection only under `TYPED_TREATMENT_READS_ENABLED`; unsupported
legacy JSON filters fall back to the compatibility store. See
[Typed treatments](TYPED_TREATMENTS.md).

I7 keeps existing consumers on `LabRepository` while `records.py` writes and
reviews through `SqliteLabAuditRepository`. The repository refreshes unverified
JSON projections transactionally, preserves approved/edited projections, and
retains all versioned extraction/history rows. This isolates the additive
sidecar without expanding the generic entity API.

I8 adds `SqliteContradictionRepository` to the catalog. Rule reconciliation may
mark a detection active or not current, but it cannot resolve it. Admin
resolution and reopening are explicit, attributable operations; provider access
is read-only. The typed sidecar does not expand the generic entity API.

I9 wraps the glucose and fingerstick adapters with compatibility repositories.
All accepted legacy mutations can project into the typed tables in the same
transaction. Connector/import dedup is a repository operation rather than a
second storage path. Supported queries can shadow both stores or return typed
results under independent flags; unsupported JSON fields always fall back.

I10 wraps all four wearable adapters with compatibility repositories. Oura,
Fitbit, Google Health, supported imports, demo seed, cycle inference, and
analytics use those boundaries. Typed daily/sample projections preserve
provider overlap and compatibility extensions. Supported date/time/source and
metric queries may shadow or cut over independently; unsupported fields and
source operators fall back to legacy JSON.

The relationship and evidence repositories deliberately do not create a hidden
schema. They project current fields only. G1 and G2 will add reviewed registries
and evidence storage; those implementations can replace the projections without
changing consumers.

## Swapping implementations

`get_repositories()` returns the production legacy catalog. Tests and isolated
operations use `use_repositories(catalog)` to install a context-local catalog:

```python
with use_repositories(in_memory_catalog):
    result = insulin._daily_tdd()
```

The override uses `ContextVar`, so concurrent async request contexts do not
share a mutable global override. A future feature-flag factory may choose a
legacy or typed implementation per repository while retaining the catalog
contract.

## Unit-of-work semantics

`SqliteUnitOfWork` opens one SQLite connection, starts `BEGIN IMMEDIATE`, and
binds every repository in its catalog to that transaction:

```python
with unit_of_work() as work:
    work.repositories.entity("HealthSummary").create(summary)
    set_config_value("health_summary_last_run", now, connection=work.connection)
    work.commit()
```

`commit()` is a commit request, not an immediate partial commit. The actual
commit occurs only on clean context exit. The entire transaction rolls back
when:

- `commit()` was not requested;
- `rollback()` was requested;
- any exception occurs, including after `commit()` was requested; or
- repository construction fails.

The transaction connection is public only for repository implementors and
small compatibility adapters such as the settings cursor above. Core analytics
must use repositories. This keeps future raw, canonical, relationship, and
evidence tables in one logical transaction without leaking SQL into consumers.

## Migrated core paths

F4 moves direct clinical entity reads out of:

- insulin resistance and response analysis;
- cross-domain Insights;
- Health Summary generation and retrieval;
- Visit Report computation; and
- Companion dossier, memory, thread, and message paths.

Existing multi-record mutation paths now use a unit of work:

- Insight set replacement;
- Health Summary replacement plus its `app_settings` scheduler cursor (a real
  two-table transaction);
- legacy Companion thread migration;
- Companion thread/message cascade deletion; and
- assistant-message creation plus thread timestamp update.

LLM calls and external requests remain outside database transactions. A user
message is intentionally persisted before an LLM call so a failed response does
not erase what the user submitted.

## Compatibility and rollout rules

1. F4 added no migration or typed table; I1/I2 add isolated provenance
   storage, and I3 adds only a rebuildable time sidecar.
2. Default reads and writes remain legacy JSON-backed.
3. Repository overrides are context-local and tests must restore them.
4. Typed implementations must pass the same repository compatibility suite.
5. Read cutover remains feature-flagged and belongs to H3.
6. New relationship/evidence writes wait for G1/G2 schemas and migrations.
7. Any operation spanning multiple repositories must use a unit of work or
   document why partial persistence is intentional.
