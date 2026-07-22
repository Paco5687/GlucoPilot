# Authorized relationship query API

Status: additive, read-only, disabled by default

Implementation: `server/relationship_api.py`, `server/relationships.py`

G4 exposes the governed SQLite relationship projection without exposing a
generic graph query language, raw source payloads, or mutation routes. It does
not add a database and does not change which data is authoritative.

## Authorization and rollout gate

Every route requires an authenticated session. Admin and read-only provider
sessions may use GET requests; no POST, PUT, PATCH, or DELETE graph endpoint
exists. Owner scope always comes from the deployment's configured owner
identity. The API does not accept an owner parameter, and absent and
foreign-owner nodes produce the same `404` response.

`RELATIONSHIP_READS_ENABLED=false` returns `503` before graph access. This is
the same explicit cutover gate used by the relationship compatibility reader,
so installing G4 does not expose an empty or unreviewed projection.

## Endpoints

| Method and path | Purpose | Bounds |
|---|---|---|
| `GET /api/relationships/{type}/{id}/neighbors` | Outgoing edges | `limit` 1–250 |
| `GET /api/relationships/{type}/{id}/reverse-neighbors` | Incoming edges | `limit` 1–250 |
| `GET /api/relationships/{type}/{id}/traverse` | Breadth-first outgoing, incoming, or bidirectional traversal | depth 1–4, edges 1–250, expansions ≤1,000 |
| `GET /api/relationships/{type}/{id}/evidence-paths` | Shortest-first paths to a required target | depth 1–4, paths 1–20, expansions ≤1,000 |

Responses include the applied budget, consumed expansions, returned counts,
and a `truncated` flag. Ordering is deterministic and declared in the response:
predicate, source type/ID, target type/ID, and relationship ID, with
breadth-first or shortest-path order where applicable.

## Public edge envelope

Each edge returns:

- typed source, predicate, and target identifiers;
- assertion kind/status and evidence level/count;
- confidence label and score;
- source class;
- governed generator ID/version;
- validity and generation time; and
- opaque SHA-256 references for evidence identifiers, source locators,
  confidence methods/calibrations, and input-data versions.

Raw `source_id`, evidence identifiers, input hashes, projection keys, entity
payloads, settings, credentials, and connector error text are never returned.
Opaque references support equality/correlation without disclosing the original
locator. This is application-layer redaction in addition to owner-scoped SQL.

## Query behavior

Typed reads include only active projector-managed edges plus independently
authored edges. Historical inactive generations remain available for audit and
backup but cannot appear in API traversal. Traversal never expands beyond its
fixed server budget even when the caller requests the largest allowed result.

Provider access remains read-only by construction. Future graph mutations must
use a separate admin-only contract and cannot be added to these routes.
