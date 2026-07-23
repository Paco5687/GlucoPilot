# Versioned evidence-backed Pattern and Insight claims

Status: additive, dual-write gated

Implementation: `server/claims.py`, `server/patterns.py`, `server/insights.py`,
`server/evidence_bundle.py`, migration 14

G7 makes each newly governed Pattern and Insight an auditable claim generation.
The JSON entity remains the compatibility record and SQLite remains the only
database. The strict `claim_versions` sidecar records the stable semantic claim
key, monotonically increasing version, content checksum, assertion kind/status,
algorithm and input-data versions, G6 confidence envelope, EvidenceSet link,
and both directions of supersession lineage.

## Publication and lineage

Pattern and Insight publication occurs in one unit of work with their evidence:

1. retain previous JSON entities and mark their active claims superseded;
2. append the new compatibility entity;
3. create its provisional `derived_statistic` claim version;
4. capture checksum-addressed source windows from the authoritative entities;
5. bind those windows to one EvidenceSet and attach it to the claim; and
6. commit the entity, claim, windows, set, and lineage together.

The same semantic key advances its version number and links the predecessor.
Claims that disappear from a later complete generation are superseded without
inventing a successor. Low-quality or empty analysis retires current claims
instead of deleting history. Insight refresh no longer uses replace-all delete.

LLM text is enrichment only. The claim remains a derived statistic governed by
the deterministic algorithm, input checksum, and G6 confidence metadata.

## Exact evidence and roles

Evidence windows capture the stored source rows, not normalized in-memory
working values. Pattern claims cite the 14-day CGM source window and treatment
context when present. Insight claims cite only the domains used by that
candidate: CGM plus wearable daily/heart-rate, cycle, or treatment observations.
High-volume minute heart rate is split into bounded 30-day windows.

`evidence_set_windows` records `supporting`, `opposing`, or `limiting` roles and
an optional rationale. Current statistical rules normally produce supporting
source windows; they do not fabricate opposing evidence. Analytics uncertainty
and domain-quality caveats are retained as structured limiting evidence.
EvidenceSets therefore expose all three sections even when one is empty.

## Authenticated drill-down

- `GET /api/evidence/claims/{Pattern|Insight}/{entity_id}` returns the claim,
  EvidenceSet, role-grouped windows, structured limitations, five source-link
  previews per window, and the complete version lineage.
- `GET /api/evidence/windows/{window_id}?offset=0&limit=50` validates the source
  checksum and returns up to 100 exact observations with authenticated source
  links. Changed source data returns `409` and permanently invalidates the stale
  window/set instead of silently showing different support.
- Existing `GET /api/evidence/sources/{type}/{id}` opens a redacted observation.

Owner identity is fixed server-side. Anonymous access is rejected, foreign
owner IDs are not enumerable, credentials and filesystem paths stay redacted,
and the window API enforces paging bounds. Pattern cards show the evidence
dialog only when an EvidenceSet link exists.

## Rollout and rollback

`EVIDENCE_SET_WRITES_ENABLED=false` retains legacy inline Pattern/Insight
evidence and disables new claim/evidence projection writes. Enabling it creates
both the compatibility entity and versioned evidence-backed claim atomically.
`EVIDENCE_SET_READS_ENABLED` still controls compatibility repository cutover;
the explicit authenticated claim/window APIs read only links already published
by the write path.

Migration 14 is additive. Rollback disables the write/read flags and returns to
legacy inline reads while retaining the ledger. Verified backups include claim
algorithm and claim-version counts. No production backfill or flag change is
performed by G7.
