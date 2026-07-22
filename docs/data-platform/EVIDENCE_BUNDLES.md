# Deterministic Evidence Bundle API

Status: additive, authenticated, read-only

Implementation: `server/evidence_bundle.py`

G5 provides one owner-scoped query contract for downstream analytics,
reporting, and reasoning features. It assembles bounded evidence from the
existing JSON entities, immutable source-link metadata, governed relationship
projection, claim evidence sets, and contradiction ledger. SQLite remains the
only operational database, and source entities remain authoritative.

## Query contract

`POST /api/evidence/bundles/query` accepts:

```json
{
  "start": "2026-05-01T00:00:00Z",
  "end": "2026-05-31T23:59:59Z",
  "domains": ["glucose", "labs", "analytics"],
  "question_intent": "glucose and laboratory trends",
  "item_budget": 50
}
```

Supported domains are `glucose`, `insulin`, `wearables`, `cycle`, `labs`,
`records`, `clinical`, and `analytics`. Instants are normalized to UTC, domains
are deduplicated and sorted, intent whitespace is canonicalized, and the item
budget is 1–250. A query matching more than 100,000 source rows is rejected
with `413`; callers must narrow the range or domains rather than receive a
silent sample.

The deployment owner is fixed on the server. No owner field is accepted.
Admin and read-only provider sessions may query bundles; the API exposes no
write, resolution, graph-mutation, or source-mutation operation.

## Response sections and safety budget

Every successful response contains:

- direct observations;
- existing derived metrics and their governed/legacy claim evidence;
- active governed relationships when `RELATIONSHIP_READS_ENABLED=true`;
- document records and openable document links;
- explicit reassuring/opposing classifications only when a source supplied a
  normal/abnormal flag;
- unresolved contradictions with both sides preserved;
- missing-data caveats for every requested domain;
- authenticated source links and immutable provenance references; and
- source-level confidence (including versioned analytics confidence and
  discovery status for Pattern/Insight sources) plus an explicitly unassessed
  bundle-level confidence envelope.

The requested item budget is shared by ranked observations, derived metrics,
documents, and relationships. Unresolved blocking contradictions are a
separate protected safety section: they never compete in ranking and therefore
cannot be omitted by a small item budget. The contradiction section has a
fixed 1,000-item server bound; a query that would exceed it is rejected rather
than silently dropping safety data.

Ranking is deterministic: question-intent token relevance, category priority,
observed time descending (undated items last), entity type, then entity ID.
The response declares this ordering and reports the requested/returned budget,
available source count, protected blocking count, and truncation state.

## Identity, caching, and invalidation

The service hashes the complete public snapshot that can affect the response:
selected source content, counts, relationship metadata, claim evidence,
source-link metadata, contradictions, caveats, schema version, and generator
version. That SHA-256 value is the `data_version.input_hash`.

The bundle ID is a SHA-256 identity over the canonical query checksum and data
version. Repeating the same query against the same public snapshot returns an
identical response. A bounded, process-local LRU uses the canonical query,
SQLite's connection-observed commit revision, and the evidence/relationship
read gates as its lookup guard, then returns deep copies of the
content-addressed response. Cache hits add no timestamps or hit markers that
would change semantics. Any committed source, evidence, relationship,
provenance, contradiction, or schema change advances the observed revision and
cannot hit a stale cache entry; the exact public-content hash remains the
portable response data version.

## Source access and redaction

Each normalized entity item links to
`GET /api/evidence/sources/{entity_type}/{entity_id}`. The endpoint repeats the
same fixed owner check and entity allowlist, returning bounded normalized data,
confidence, immutable source-reference hashes, and an original document link
when applicable. Medical-record and lab items link to
`GET /api/records/file/{record_id}?inline=true`; lab links retain the reported
source page.

Credential-like fields, owner email, and internal filesystem locators are
removed recursively. Free-form source/evidence/version locators from graph and
archive records are returned only as opaque SHA-256 references. Foreign-owner
and absent source IDs intentionally produce the same `404` response.

## Rollout behavior

G5 adds no migration and enables no write path. Relationship items follow the
existing `RELATIONSHIP_READS_ENABLED` gate; when disabled, the bundle remains
usable and carries an explicit missing-data caveat. Evidence-set claim reads
continue to follow `EVIDENCE_SET_READS_ENABLED`, preserving the established
compatibility rollback path.
