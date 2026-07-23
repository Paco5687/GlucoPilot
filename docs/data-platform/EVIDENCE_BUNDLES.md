# Deterministic Evidence Bundle API

Status: v2.2, additive, authenticated, read-only

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
  "item_budget": 50,
  "normalized_entity_types": ["DailySummary", "WeeklySummary", "Pattern"]
}
```

Supported domains are `glucose`, `insulin`, `wearables`, `cycle`, `labs`,
`records`, `clinical`, and `analytics`. Instants are normalized to UTC, domains
and the optional normalized-entity-type allowlist are deduplicated and sorted,
intent whitespace is canonicalized, and the item budget is 1–250. Allowlisted
types must belong to the selected domains. A query matching more than 100,000
source rows is rejected with `413`; callers must narrow the range, domains, or
normalized entity types rather than receive a silent sample.

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

G7 adds authenticated claim-specific paths alongside bundle queries. Pattern
cards call `GET /api/evidence/claims/{type}/{id}` to load the linked EvidenceSet,
role-grouped support/limitations, and supersession history only on demand.
Window links page through exact checksum-validated observations at
`GET /api/evidence/windows/{window_id}`; each observation links back to the
redacted normalized-source endpoint above.

Evidence Bundle 2.0 adds two safety semantics used by G8 consumers:

- a machine parser score can no longer label an unverified `LabResult` as
  clinically high-confidence; lab confidence includes verification status,
  validation status, an explicit `clinically_verified` boolean, and the
  unverified limitation; and
- selected governed Pattern/Insight items carry a claim-version block plus an
  authenticated claim-evidence link. Legacy rows remain visible as derived
  metrics but are not presented as governed evidence-backed claims.

Evidence Bundle 2.2 adds on-demand `InsulinResponseEvent` derived metrics to a
bounded `insulin` query. Each event carries
`insulin-response/1.0.0`, its input hash, clean/confounded/excluded state,
explicit reasons, and exact normalized source links. These items are ranked
within the existing shared budget, do not persist a second copy of source
facts, and do not infer causation, resistance, or absorption.

Evidence Bundle 2.3 adds source-linked `ActivityPositionEffect` items for
bounded wearable/analytics queries. Effects retain sample size, measured and
missing intervals, the shared confidence envelope, and explicit replication
status. Daily wearable totals never become event-time position evidence.
Companion filters this type to qualifying effects only; exploratory and invalid
effects remain visible to direct bundle, Wearables, and Visit Report consumers.

Evidence Bundle 2.4 adds one bounded `ManagementBurdenSummary` derived metric
for analytics queries. It preserves visible component weights, source coverage,
the shared confidence envelope, outcomes as a separate dimension, noncausal
language, and links back to the normalized source events used in the measured
effort calculation. Missing source families remain explicit limitations.

P6 specialist briefs are deterministic Evidence Bundle consumers. Each mode
declares domains, intent, budget, and a fail-closed entity allowlist. Briefs
retain source links and contradiction/limitation blocks while always omitting
insurance and unrelated specialty PHI. They do not add a new bundle version or
permit a generated narrative to become evidence.

## Shared Overview and Visit Report consumer

`server/clinical_evidence.py` is the common G8 adapter over this API. Overview
generation and the Visit Report use the same calendar-bounded, content-addressed
portfolio of three scoped bundles: governed claims, clinical context, and
labs/records. Their declared budgets total 250, preventing a large lab dossier
from crowding clinical history or claims out of the shared context. The adapter
returns explicit data-quality, data-through, contradiction, source,
missing-data, and governed-claim blocks. The bounded sanitized item list is the
only non-metric dossier supplied to the LLM. Prior generated summaries are
excluded from that list so a narrative cannot become evidence for its own
replacement.

Generated narratives may cite only item IDs present in that bundle. Unknown or
invented IDs are removed server-side before persistence or response, and valid
IDs resolve only to links already attached to the selected evidence item.
Deterministic glucose, insulin, cycle, wearable, and lab-count metrics remain
separate domain calculations; the LLM neither computes nor overwrites them.
See [Shared clinical evidence](SHARED_CLINICAL_EVIDENCE.md).

## Companion consumer

G9 replaces the Companion's broad dossier-only grounding with a
question-ranked portfolio of five bounded Evidence Bundle scopes. Personal
statements may cite only the selected `E#` aliases; invented aliases are
removed, uncited personal claims fail closed, and unverified lab citations are
qualified before display or persistence. The assistant message stores exact
item/source links and exposes evidence, opposing-evidence, and content-hash
change commands. General web references and user memories remain separately
typed. See [Evidence-grounded Companion](COMPANION_EVIDENCE.md).

## Rollout behavior

G5 adds no migration and enables no write path. Relationship items follow the
existing `RELATIONSHIP_READS_ENABLED` gate; when disabled, the bundle remains
usable and carries an explicit missing-data caveat. Evidence-set claim reads
continue to follow `EVIDENCE_SET_READS_ENABLED`, preserving the established
compatibility rollback path.
