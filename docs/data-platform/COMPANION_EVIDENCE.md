# Evidence-grounded Companion

Status: G9, additive, admin-only

Implementation: `server/companion_evidence.py`, `server/companion.py`,
`frontend/src/components/evidence/CompanionEvidence.jsx`

The Companion no longer receives a broad, hand-built clinical dossier as its
personal-data authority. Each turn builds a deterministic Evidence Bundle 2.0
portfolio ranked by the question. SQLite entities remain authoritative; the
persisted Companion claim links are historical references, not a second
clinical store or a graph source of truth.

## Bounded retrieval

Five independent scopes prevent high-volume samples from crowding out clinical
evidence:

| Scope | Window | Maximum selected items |
| --- | ---: | ---: |
| glucose and insulin | 30 days | 16 |
| wearables and cycle | 14 days | 12 |
| governed analytics | 365 days | 12 |
| clinical history | 10 years | 12 |
| labs and records | 10 years | 24 |

Question keywords select relevant scopes; an ambiguous whole-person question
uses all five. Comparison, trend, pattern, and correlation questions add the
analytics scope. The total configured ceiling is 76 items, and sanitized
reasoning data is further compacted to 48,000 characters. The complete reply
prompt is capped at 96,000 characters, including at most 40 recent memories and
16 bounded conversation messages. If protected contradiction/limitation
context cannot fit, generation fails rather than silently hiding it.

The labs/records adapter may retrieve up to 150 already-bounded Bundle items
internally, then deterministically retains at most 16 lab results and 8 source
records (reversed for record/imaging questions). This prevents document
category priority from displacing every analyte while keeping the prompt limit
at 24. Metabolic ranking selects glucose observations for CGM/TIR questions and
insulin treatments for pump/TDD questions.

Existing deterministic glucose and insulin calculations stay separate from the
LLM. Prior generated summaries and insurance data are excluded from evidence
selection, preventing circular reasoning and disclosure of billing identifiers.

## Claim and source contract

Evidence items receive deterministic `E#` aliases. Recent user memories use
`M#`; optional general medical references use `G#`. The prompt requires:

- every personal observation, calculation, correlation, or hypothesis to cite
  one or more selected `E#` aliases;
- lived-experience claims to cite `M#`;
- general medical facts to remain separate and use only supplied `G#`
  references; and
- machine-extracted labs with no approved/edited verification to be called
  unverified.

Before any reply is shown or persisted, the server removes invented aliases,
qualifies cited unverified labs, and replaces uncited personal-data statements
with an explicit unsupported-evidence message. Each retained statement is
classified as `observation`, `calculation`, `correlation`, `hypothesis`,
`general_information`, `user_memory`, or `safety_guidance`.

The assistant `ChatMessage` stores a compact `evidence` block containing:

- the content-addressed portfolio and per-scope input hashes;
- statement classifications and exact Evidence Bundle item IDs;
- normalized entity and opaque immutable-source IDs;
- authenticated source links;
- separately identified user-memory and external-general-source IDs;
- opposing evidence, both sides of unresolved contradictions, caveats, and
  retrieval-budget metadata.

No new schema or migration is required. Existing messages remain readable and
simply lack the new evidence controls.

## Evidence commands

Every newly grounded assistant message exposes three owner-checked commands:

- **Show evidence** — classifications, aliases, source links, and caveat count.
- **What argues against this?** — explicit opposing evidence and both sides of
  every selected unresolved contradiction.
- **What changed?** — rebuilds the same question-ranked portfolio and compares
  content hashes by scope.

The commands accept only an assistant message belonging to the deployment
owner. Missing and foreign-owner messages return the same not-found response.
They do not mutate source data, resolve contradictions, or make derived links
authoritative.

## General web grounding

Optional trusted-source research remains off by default. Only the existing
privacy-safe distilled general topic may leave the server. Web references are
stored and rendered separately from personal Evidence Bundle citations, so a
general article cannot be presented as proof of a personal inference.
