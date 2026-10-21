# Evidence-linked clinician and specialist briefs

P6 provides deterministic, printable briefs for concise clinician,
endocrinology, gastroenterology, neurology/autonomic, hematology,
gynecology/reproductive, and primary-care review. Briefs do not use an LLM and
do not create clinical conclusions.

## Evidence scopes

A brief is assembled from several bounded Evidence Bundle queries ("scopes"),
so the standing record never competes with high-volume rows for one item
budget:

| Scope | Types | Lookback |
| --- | --- | --- |
| standing | Diagnosis, Medication, Allergy, HealthProfile | full history |
| history | HistoryEntry, SymptomLog, health episodes, medication exposures | brief window |
| weight | WeightLog | 365 days |
| labs_records | LabResult, MedicalRecord | 730 days |
| analytics | Pattern, Insight, summaries, activity/position effects, management burden | brief window, 150 items |
| signals | glucose/insulin contradictions only | brief window |

Diagnoses, medications, allergies and weight appear in every mode. Labs,
documents and history items still pass the specialty keyword filter.

High-volume sources are summarized instead of listed:

- **CGM summary**: time in range, below 70 and below 54 mg/dL against the
  international consensus targets, mean, GMI, CV, low episodes (≥15 minutes
  below 70), overnight lows, coverage and data-through date.
- **Insulin & carbohydrate summary**: reconciled TDD, bolus units and count per
  day, carbohydrate per day, manual temp basals, suspensions, automated-mode
  share, and the median glucose drop per unit over unconfounded bolus windows.
- **Wearable summary** and **cycle summary** for modes that use them. SpO2
  day values outside 70–100% are excluded as device errors.
- **Weight trend**: latest weight, change from the 12-month high, and change
  over the brief window, with links to the endpoint WeightLog rows.

Labs list the most recent eligible value of each test (rejected or invalid
extractions are excluded); machine extractions that were never approved are
labelled "Unverified extraction".

## Concerns

Concerns come from explicit rules, each with a stated reason:

- abnormal lab: the latest value of a test is flagged high/low/abnormal/critical,
  or an earlier value in the window was;
- weight loss: ≥5% below the 12-month high;
- hypoglycemia: time below 70 mg/dL above 4% or below 54 mg/dL above 1%;
- reported symptoms: HistoryEntry titled "Reported symptoms: …", or a
  SymptomLog of moderate or severe severity.

## Minimum-necessary policy

Policy `specialty_minimum_necessary/1.1.0`. `InsuranceInfo` is always omitted.
MedicalRecords are included only for clinical document types (`lab_report`,
`imaging_report`, `visit_summary`, and similar). Insurance cards and documents
whose text carries insurance identifiers are excluded, and so are any labs
extracted from them. Other uploads (`other`, unprocessed, self-authored
supplement protocols) are excluded as non-clinical. The response reports the
omitted counts in `privacy.omitted_documents`.

## Patterns and insights

Superseded generations (`is_active: false`) and dismissed patterns are dropped,
and repeats of the same finding are merged into the newest one. Titles drop the
patient-facing possessive and body text moves to the third person ("Your sleep
lines up with your time in range" → "Sleep lines up with time in range").
SpO2-based items are withheld when the window's SpO2 source holds implausible
values.

## Display contract

Every item carries a `display` block: `label`, `value`, `unit`, `date`,
`reference`, `flag`, `status`, `detail`. The page and the clinician share export
render from it; raw entity type names are not shown as titles.

## Language safety

Each evidence item carries a deterministic strength block. Exploratory,
emerging, reproduced, not-reproduced, invalid, observed, confirmed, and
calculated states use distinct lead language. Associations never permit causal
language.

The governed hypothesis ledger is filtered using the specialty vocabulary.
Diagnosis rows worded as tentative ("suspected", "possible", "rule out") are
moved to the hypotheses section. Anything short of clinician-confirmed status
is labelled:

> Unconfirmed — not a diagnosis

Primary-care mode leaves unconfirmed hypotheses out and notes how many were
omitted. Tentative hypotheses never receive definitive language. The brief
supports clinician review and does not recommend treatment.

## API and UI

`POST /api/briefs/clinician` accepts `mode` and a 7–365 day range. It is
authenticated and read-only, including for provider sessions. `/brief`
provides specialty and range controls, printing, value-bearing cards, a lab
table, source drill-down (hidden in print), and an appendix table. Meaningful
mode/range changes regenerate the brief; ordinary rerenders do not.
