# Evidence-linked clinician and specialist briefs

P6 provides deterministic, printable briefs for concise clinician,
endocrinology, gastroenterology, neurology/autonomic, hematology,
gynecology/reproductive, and primary-care review. Briefs do not use an LLM and
do not create clinical conclusions.

## Evidence and minimum-necessary policy

Every mode calls the shared Evidence Bundle service with a bounded date range,
specialty-specific domains, question intent, and item budget. A second
fail-closed entity allowlist removes unrelated items before sectioning.
`InsuranceInfo` is always omitted. The response records its mode, selected
entity types, Evidence Bundle ID/version/data checksum, and
`specialty_minimum_necessary/1.0.0` policy.

The brief contains concerns, objective patterns, glucose/insulin when relevant,
management burden, labs/imaging, reassuring and opposing evidence,
contradictions, limitations, visit questions, and an evidence appendix.
Appendix entries retain the Evidence Bundle source links so authenticated admin
and source-read-only provider sessions can open the underlying evidence.
Providers may append attributable review events without mutating evidence;
owners may accept or dispute them without erasing clinician history.

## Language safety

Each evidence item carries a deterministic strength block. Exploratory,
emerging, reproduced, not-reproduced, invalid, observed, and confirmed states
use distinct lead language. Associations never permit causal language.

The governed hypothesis ledger is filtered using the specialty vocabulary.
Anything short of clinician-confirmed status is labeled:

> Unconfirmed hypothesis — not a diagnosis

Tentative hypotheses never receive definitive language. The brief supports
clinician review and does not recommend treatment.

## API and UI

`POST /api/briefs/clinician` accepts `mode` and a 7–365 day range. It is
authenticated and read-only, including for provider sessions. `/brief`
provides specialty and range controls, printing, sectioned evidence, strength
language, and source drill-down. Meaningful mode/range changes regenerate the
brief; ordinary rerenders do not.
