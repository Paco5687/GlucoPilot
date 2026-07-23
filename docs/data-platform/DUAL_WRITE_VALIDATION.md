# Production dual-write validation

Status: H2 validation gate; legacy reads remain authoritative

Implementation: `server/dual_write_validation.py` and
`tests/test_dual_write_validation.py`

This gate validates the treatment, glucose/fingerstick, and wearable typed
projections before any read cutover. It does not enable typed reads. Reports
contain aggregate counts, reason codes, checksums, booleans, mapping/schema
versions, and query latency only. They contain no entity IDs, owner email,
timestamps, clinical values, notes, or source payloads.

## Approval contract

Each domain receives an independent checksum-protected report and detached
operator signature. A report is eligible only when:

- every mappable legacy row has one matching typed projection;
- missing, mismatched, fingerprint-drift, and extra counts are zero;
- compatibility query count, normalized checksum, ordering, and aggregate
  checks match (absent and explicit unknown/null fingerstick capture defaults
  are semantically equivalent);
- treatment provider identities contain no cross-entity duplicates;
- unmappable rows have only explicitly permitted static reason codes and stay
  within the domain ratio; and
- representative typed-query p95 is at most the larger of 50 ms or legacy p95
  times two plus 5 ms.

The domain tolerances are deliberately narrow:

| Domain | Permitted unmappable reasons | Maximum ratio |
|---|---|---:|
| Treatments | none | 0 |
| Glucose/fingersticks | legacy glucose value outside the strict 20–600 mg/dL contract | 1% |
| Wearables | none | 0 |

An allowed unmappable row is not discarded or hidden. Legacy JSON remains its
authoritative representation. A new reason code or excess ratio blocks the
domain and requires a synthetic regression plus reviewed policy change; free
text exception matching is not used.

The synthetic fixtures already retain both compatibility differences found
during rehearsal: an out-of-contract glucose value and a suspension without an
explicit rate. The latter must round-trip without adding an `absolute` field.

## Staged production procedure

1. Create and independently restore-verify an off-volume backup.
2. Enable the three write flags while all typed-read flags remain false.
3. Run each bounded, restartable backfill.
4. Enable the three shadow flags. Shadow queries return legacy rows and log
   value-free parity and latency.
5. Generate private reports in a newly named backup directory:

```bash
python -m server.dual_write_validation validate \
  --database /data/app.sqlite3 \
  --phase historical \
  --output-dir /private-backup-path/h2-cutover-UTC
```

The directory is forced to mode `0700`; report files are created once with mode
`0600`. Existing files, symlink output directories, and invalid report
checksums are rejected. Standard output contains only domain, decision,
filename, and report checksum.

6. Verify the internal checksums:

```bash
python -m server.dual_write_validation verify \
  /private-backup-path/h2-cutover-UTC/*-cutover-report.json
```

7. After reviewing each private report, the authorized operator creates and
   verifies a detached armored signature per domain:

```bash
for report in /private-backup-path/h2-cutover-UTC/*-cutover-report.json; do
  gpg --armor --detach-sign "$report"
  gpg --verify "$report.asc" "$report"
done
```

Production-derived reports and signatures stay in private backup storage. Only
their decision, checksum, signer fingerprint, and verification result may be
recorded in the issue; never attach the reports to a public issue or commit
them.

## Observation and rollback

Run the validator once with `--phase historical` after backfill and again with
`--phase incremental` after at least one connector/write cycle, using a new
output directory each time. A domain is approved only when both reports are
eligible and signed. Keep:

```dotenv
TYPED_TREATMENT_READS_ENABLED=false
TYPED_GLUCOSE_READS_ENABLED=false
TYPED_WEARABLE_READS_ENABLED=false
```

throughout H2. To stop the pilot, set shadow flags false first and then write
flags false. This immediately stops comparison/projection work while legacy
reads and data remain unchanged. A database restore is reserved for database
integrity failure, not ordinary flag rollback. H3 owns any selective typed-read
cutover.
