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
| `GlucoseRepository` | `GlucoseReading` JSON entities |
| `TreatmentRepository` | `Treatment` JSON entities |
| `LabRepository` | `LabResult` JSON entities |
| `WearableRepository` | Oura/Fitbit daily and heart-rate JSON entities |
| `RelationshipRepository` | Read-only projection of lab→record and message→thread references |
| `EvidenceRepository` | Read-only projection of Pattern/Insight inline support and ChatMessage sources |
| `EntityRepository` | Compatibility adapter used for remaining registered domains |

Every entity adapter preserves the generic filter, sort, limit, skip, envelope,
and mutation behavior used by the existing API. The generic entity routes still
call `server.db` directly, so F4 does not expand or restrict API exposure.

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

1. F4 adds no migration or typed table.
2. Default reads and writes remain legacy JSON-backed.
3. Repository overrides are context-local and tests must restore them.
4. Typed implementations must pass the same repository compatibility suite.
5. Read cutover remains feature-flagged and belongs to H3.
6. New relationship/evidence writes wait for G1/G2 schemas and migrations.
7. Any operation spanning multiple repositories must use a unit of work or
   document why partial persistence is intentional.
