# Synthetic golden datasets

Status: additive regression layer

Implementations: `tests/fixtures/`, `tests/test_golden_data.py`,
`tests/test_migration_fixtures.py`, `tests/test_contradictions.py`,
`tests/test_typed_glucose.py`, `tests/test_typed_wearables.py`,
`tests/test_relationship_projection.py`, `tests/test_relationship_api.py`,
`tests/test_evidence_backed_claims.py`, and `tests/test_episodes.py`

F5 establishes deterministic, public-safe fixtures for clinical interpretation
and storage risks. The fixtures contain invented values, synthetic identifiers,
the reserved `.invalid` domain, and the application default
`owner@glucopilot.local`. They contain no exported records, production database
rows, credentials, or copied health information.

## Golden scenarios

| Scenario | Expected outcome protected by CI |
|---|---|
| Missing pump data | Bolus and carbohydrate summaries remain available, but basal availability stays false and basal totals stay zero. |
| Compression low | One isolated short CGM artifact does not become a recurring-low pattern. The paired fingerstick remains a distinct measurement. |
| Fingerstick disagreement | The nearest CGM sample is retained alongside the meter value, with the signed CGM-minus-meter delta and bias classification. |
| Cycle effects | Phase-day counts, per-phase glucose summaries, inferred-source labeling, and a 28-day cycle interval remain deterministic. |
| Duplicate labs | Repeated extraction of the same test/date/value is stored once per document; a genuinely different value remains distinct. |
| Conflicting ranges | A report uses the reference range and flag attached to the latest observation instead of reinterpreting the value with an older range. |
| Source overlap | Readings within the global four-minute window are skipped across sources; the next physiological sample is retained. |
| DST fall-back | Both UTC observations in the repeated local 1 a.m. hour remain present, while ambiguous local timestamps require an explicit offset. |
| Clinical contradictions | Seven synthetic conflicts deterministically preserve both sides across glucose, pump TDD, lab unit/range, hormone timing, and revised-source rules. |
| Typed glucose/fingersticks | Exact ±240-second dedup boundary, strict mapping, fixed pairing, restartable backfill, shadow parity, rollback, and backup counts remain deterministic. |
| Typed wearables | Oura/Fitbit/Google provider overlap, explicit null/extension preservation, strict metric/time mapping, bounded backfill, indexed high-volume reads, parity, rollback, and backup counts remain deterministic. |
| Relationship projection jobs | Repeated full builds have stable checksums; failed and scoped builds cannot publish mixed generations; authored assertions survive; freshness and backup counts remain observable. |
| Relationship query API | Anonymous/disabled access, admin/provider GET behavior, owner isolation, deterministic bounds, reverse traversal, evidence paths, and raw-locator redaction remain enforced. |
| Analytics confidence | Exact effects/intervals/missingness, seven-day exploratory status, reproduced and not-reproduced temporal holdouts, invalid inputs, small-sample language, and confirmed/inferred cycle provenance remain deterministic. |
| Evidence-backed claims | Pattern/Insight generations preserve predecessor/successor lineage, exact source-window replay, evidence roles/limitations, authenticated source links, and non-destructive refresh. |
| Canonical health episodes | Date/UTC ranges, multi-source temporal membership, non-causal database constraints, append-only corrections, open medication exposures, decisions, provider access, Evidence Bundles, and restore counts remain deterministic. |
| CGM/fingerstick reconciliation | Fixed nearest-CGM snapshots preserve both values; absolute/relative differences, pair timing, context strata, persistent-bias sample gates, and confirmed/CGM-only/meter-only low classifications remain deterministic. |
| Activity/position analysis | Manual-first interval resolution retains inferred rows; glucose/morning slopes, clean bolus response, and fingerstick discrepancy effects preserve samples, interval missingness, confidence/replication status, noncausal language, Companion gates, and backup counts. |
| Management burden | Visible weighted components, missing-source confidence penalties, rescue/device/overnight inference, append-only exclusions, outcome-versus-effort language, provider authorization, Evidence Bundle links, and backup counts remain deterministic. |
| Specialist briefs | Every mode uses a bounded Evidence Bundle query; irrelevant/insurance PHI is omitted, source links remain openable, exploratory language stays qualified, hypotheses remain tentative, and provider source access remains read-only. |
| Provider reviews | Provider actions preserve actor/target/time/prior/new state, source mutation stays forbidden, owner disputes retain clinician history, backups retain the audit ledger, and Companion promotes only non-disputed clinician confirmations. |
| Share-safe exports | All five role policies use explicit field allowlists; synthetic email/token/URL/insurance/RX/employer/internal-ID leaks stay absent; research remains Evidence Bundle-bounded; changed snapshots cannot download under an earlier preview checksum. |

`clinical_edge_cases.json` stores both the synthetic inputs and their expected
outcomes. Tests must compare production parser, deduplication, analytics,
report, and Companion evidence functions with those outcomes. Incorrect health
interpretation fixes must add or update a synthetic scenario before changing
the expected result.

`contradictions.json` separately pins contradiction rule output, order, stable
fingerprints, changed-evidence behavior, explicit hormone timing declarations,
blocking resolution requirements, actor history, and source-identity redaction.

`typed_glucose.json` uses only invented readings and provider identifiers. It
pins canonical/source time preservation, strict value/delta constraints,
cross-source overlap, the inclusive tolerance boundary, invalid timestamp
handling, idempotent backfill, query checksum/order/aggregate parity, and atomic
rollback.

`typed_wearables.json` uses invented daily measures, heart-rate samples, and
provider identifiers. It pins same-date provider overlap, explicit-null and
compatibility-extension preservation, invalid metric handling, source/canonical
time identity, and value-free parity.

`analytics_confidence.json` uses invented linear series, group values, event
counts, and phase rows. It pins the versioned statistical contract and contains
no production-derived measurements or dates.

`evidence_backed_claims.json` pins the public claim contract, algorithm
versions, allowed evidence roles, and two-generation supersession outcome.

## Migration snapshots

The SQL fixtures represent two supported prior states:

1. `pre_registry_v0.sql` is the untracked legacy JSON schema.
2. `tracked_baseline_v1.sql` has migration 1 recorded but predates the entity
   schema registry.

Both snapshots carry one synthetic sentinel row. Tests migrate each snapshot to
the current schema, verify the row survives, run SQLite integrity checks, and
compare the resulting table/index shape with a clean current database. The v1
checksum token is filled from the immutable migration definition during the
test; fixture SQL never contains a credential.

When a new migration ships, add the immediately preceding release state before
changing the runner. Never regenerate an old snapshot from a production
database.

## Regression layers

- Parser: Glooko unit/scaling and treatment mapping plus Nightscout combined
  treatment mapping.
- Deduplication: cross-source glucose tolerance and repeated lab extraction.
- Analytics: isolated compression-low behavior and explicit fingerstick
  disagreement.
- Report snapshot: missing basal data, cycle effects, date-specific lab ranges,
  and repeated DST-hour aggregation.
- Companion evidence: numbered source blocks survive prompt construction and
  repository projection.
- Rollback: an error after entity and settings writes leaves neither table
  partially updated.
- Migration: every maintained prior-release snapshot converges without data
  loss.
- Relationship graph: governed registry drift/unknowns, subject/object and
  owner validation, temporal/confidence filters, deterministic immutable
  generations, atomic full/scoped replacement, failure rollback, authored
  assertion preservation, freshness, compatibility reads, indexes, and backup
  counts.
- Evidence windows: checksum determinism, bounded membership, exact drill-down,
  source-change invalidation, Pattern citation cardinality, and restore counts.
- Evidence-backed claims: Pattern/Insight non-destructive publication, version
  lineage, governed confidence, role-preserving EvidenceSets, authenticated
  paged source access, and backup counts.
- Health hypotheses: origin/status guards, immutable evidence revisions and
  events, supporting/opposing/missing evidence balance, provider read-only
  access, clinician-gated terminal decisions, and backup parity.
- Health episodes: interval precision, owner-validated multi-source membership,
  immutable revisions/events, database-enforced non-causal semantics,
  open-ended medication exposures, admin/provider access, confirmation/
  correction, clinical Evidence Bundle inclusion, and backup parity.
- Relationship API: authentication/read gating, provider-safe GET access,
  owner non-enumeration, deterministic budgets/order, and secret-locator
  redaction.

## CI gate

Tests carrying the `risk_critical` marker run in the dedicated
`Risk-critical data integrity` CI job:

```bash
pytest -q -m risk_critical
```

The ordinary backend job still runs the complete suite. The dedicated job makes
data-integrity failures visible as a separate required check and includes the
golden fixtures plus migration, backup, contract, audit, repository, and
rollback coverage.

## Fixture safety rules

1. Every clinical fixture declares `"synthetic": true` or includes the SQL
   header `SYNTHETIC FIXTURE ONLY`.
2. Use only invented identifiers, `owner@glucopilot.local`, and `.invalid`
   URLs.
3. Do not copy timestamps, prose, filenames, measurements, or record sequences
   from production data.
4. Do not add PDFs, images, CSV exports, SQLite files, tokens, or settings
   dumps.
5. The public-safety assertions must pass before any fixture is committed.
