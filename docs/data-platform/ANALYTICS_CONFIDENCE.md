# Analytics confidence and replication

Status: active shared contract

Implementations: `server/analytics_confidence.py`, `server/insights.py`,
`server/patterns.py`, `server/report.py`, `server/health_summary.py`,
`server/evidence_bundle.py`, and `frontend/src/lib/analyticsConfidence.js`

Contract version: `analytics-confidence/1.0.0`

G6 gives observational analytics one deterministic vocabulary. It describes
the support available for a calculation; it does not represent clinical
verification, diagnosis, treatment advice, or causality. Pattern and Insight
entities remain rebuildable JSON rows in the existing SQLite entity store. No
second database or authoritative derived-data store is introduced.

## Envelope

Every governed result carries:

- sample count and distinct valid days;
- an effect-size metric, value, magnitude, and direction;
- a 95% confidence interval and named method when calculable;
- expected, valid, and missing days plus missingness rate;
- the temporal direction tested;
- discovery and replication status;
- a bounded numerical confidence score and label; and
- language controls that always prohibit definitive and causal claims.

Correlations use Pearson `r` with a Fisher-z 95% interval. Two-group results
use a standardized mean difference (Cohen's d) plus a normal Welch interval
for the raw mean difference. Repeated binary observations use an observed rate
with a Wilson-score interval. These are descriptive observational statistics,
not clinical significance tests.

## Discovery and replication statuses

| Status | Meaning |
|---|---|
| `exploratory` | A valid initial estimate has at most seven valid days or fewer than 14 samples. A perfect seven-day correlation is still exploratory. |
| `emerging` | An initial estimate has at least 14 samples but no eligible temporal holdout. |
| `reproduced` | At least 28 dated correlation pairs permit a first-half/later-half holdout, and both halves have an effect of at least `|r|=0.30` in the same direction. |
| `not-reproduced` | The eligible temporal holdout reverses direction, falls below the effect threshold, or cannot reproduce the discovery half. |
| `invalid` | The statistic cannot be calculated, including a constant correlation axis or too few numeric values. |

`reproduced` means internal temporal reproduction only. The envelope names the
holdout kind and both sample/effect values so no consumer can mistake it for
external, prospective, or clinical replication. Group comparisons and pattern
rates do not claim replication without a separately defined holdout.

## Cycle phase provenance

Cycle summaries and cycle-based Insight confidence distinguish:

- `confirmed_days`: explicitly recorded or imported phase days; and
- `inferred_days`: phase days whose source is algorithmic inference.

Here, “confirmed” means confirmed as an explicit record/import, not
clinician-confirmed physiology. Mixed phase groups retain per-phase and total
counts. An inferred day is never silently promoted to confirmed.

## Language and AI consumers

Each status selects a deterministic lead such as “An exploratory signal was
observed in this limited sample.” Small-sample and invalid results always set
`definitive_allowed=false` and `causal_allowed=false`. Model text containing
prohibited claims such as “proves,” “causes,” or “clinically confirmed” is
discarded in favor of deterministic fallback language.

The full numerical envelope is supplied to Pattern and Insight enrichment.
Stored Insight envelopes and statistics flow into Health Summary synthesis;
cycle provenance flows into Health Summary and Visit Report synthesis; and
Evidence Bundles expose the analytics score, method version, and discovery
status as source confidence. Prompts explicitly require status-scaled language
and recorded/inferred phase distinctions.

The dashboard's range-local Glucose × Oura cards mirror the same version,
Pearson/Fisher calculation, seven-day label, and temporal-holdout rule. They
display discovery status separately from effect magnitude: a large effect in a
small sample is still exploratory.

## Golden datasets and change control

`tests/fixtures/golden/analytics_confidence.json` contains synthetic/public-safe
cases for seven-day exploration, temporal reproduction, failed reproduction,
an invalid constant axis, small group/rate samples, and mixed cycle provenance.
Backend and browser tests pin effect sizes, intervals, status, missingness,
replication metadata, and language restrictions.

Changing a formula, threshold, or status definition requires:

1. a new contract version;
2. an explicit golden-fixture update;
3. backend/browser parity tests; and
4. review of every AI prompt and displayed confidence label.

Rollback is code-only: deploy the previous image. Source records, graph tables,
and evidence tables are untouched, and derived Pattern/Insight rows can be
regenerated from authoritative observations.
