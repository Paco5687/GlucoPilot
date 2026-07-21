# Synthetic golden datasets

Status: additive regression layer

Implementations: `tests/fixtures/`, `tests/test_golden_data.py`,
`tests/test_migration_fixtures.py`

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

`clinical_edge_cases.json` stores both the synthetic inputs and their expected
outcomes. Tests must compare production parser, deduplication, analytics,
report, and Companion evidence functions with those outcomes. Incorrect health
interpretation fixes must add or update a synthetic scenario before changing
the expected result.

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
