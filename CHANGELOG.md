# Changelog

All notable changes to GlucoPilot are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Pump settings from Glooko** — carb ratios, correction factor (ISF), targets,
  correction threshold, active insulin time, max bolus/basal and basal programs,
  synced once a day from Glooko's settings snapshots. Shown read-only on the
  Insulin and Connections pages and in the Visit Report, each with the
  snapshot's as-of date and age; the Companion can answer from the latest one.

### Changed
- **One report** — the Clinician Brief is merged into the Visit Report. A
  "For" selector (or `/report?for=<specialty>`) gives the Brief's specialty
  views: each shows its own data domains, with labs and documents filtered by
  its keywords and insurance left out. Lab results, imaging studies and other
  clinical documents link to the uploaded document they came from, and the
  report lists the clinical documents on file. `/brief` now opens the Visit
  Report; Share Export still builds briefs.
- **Editable discussion points** — the owner edits the Visit Report's "Points
  to discuss" before printing; the edit is saved per view and prints in place
  of the suggested points until cleared. Suggested points no longer raise
  correlations between wearable signals and glucose.
- **Visit Report printing** — no forced page break after the summary, so a
  long summary no longer leaves a mostly blank page; section headings stay
  with the content after them.

### Fixed
- **Voice food log matching** — a saved food is offered to the estimate only
  when what was said names it: most of its name, or a brand word no USDA food
  has. "Yogurt bowl" and "Greek yogurt bowl with blueberries" no longer log as
  the saved yogurt bites. A bare hour ending the sentence ("rice bowl at 12.")
  sets the meal time instead of leaving it to the model, which sometimes dropped
  it. Whisper's "cash browns" is logged as hash browns, and hash browns are in
  its vocabulary prompt.
- **Voice food log portions** — counting a food's pieces no longer multiplies
  by a whole bag ("10 crackers" was 10 snack bags, about 276 g of carbs; it is
  now 10 crackers). "1 serving" is USDA's typical amount unless USDA lists a
  serving itself, so a serving of crackers is no longer a snack bag and a
  serving of raisins no longer one raisin. "1 medium" potato is the any-size
  potato (170 g), not a 60 g baby potato.
- **Visit Report weight** — the headline is change from the 12-month high to
  the latest weight. Change since the first entry is shown as secondary context.
- **Visit Report narrative** — the prompt is capped at 8,000 tokens (less when
  the model's window is smaller), shedding evidence, contradictions and lab
  detail in steps until it fits.
- **Lab flags** — when a result has a numeric value and reference range, the
  range decides high/low/normal; a stored flag the range contradicts is noted
  and not used.
- **Lab results read the same everywhere** — the Visit Report, Clinician Brief
  and Companion share one table of test-name synonyms, and the Companion now
  takes a lab's flag from its value and reference range when the source's flag
  contradicts it, as the Report and Brief already did.
- **Clinician Brief** — concerns are ranked (weight loss, hypoglycemia,
  reported symptoms, then one grouped abnormal-labs entry); each lab test is
  listed once; reassuring, opposing and limitation entries show their label,
  value and reason.

## [0.1.2] — 2026-07-21

### Added
- **Companion web grounding** — optional, off by default. The Companion searches
  trusted medical sources (NIH's MedlinePlus + PubMed, plus an optional
  allowlisted web-search provider) and cites them inline instead of recalling
  specifics from memory — cutting hallucination. Only the general medical topic
  of your question is sent out, never your records. (#85)

### Changed
- **One Overview page** — the former Insights and Patterns tabs are folded into
  Overview (Insights was redundant with its cross-domain connections, and already
  fed them). Overview now shows metrics, connections, on-track / worth-watching,
  and a "Patterns in your glucose" section; Refresh regenerates the summary and
  re-scans patterns together.
- **Glooko** — polling reduced from every 30 minutes to every 6 hours (the pump
  uploads to Glooko roughly once a night).

### Fixed
- **Visit Report and Overview no longer hang** — both moved off the GPU-starved
  quality (27B) model to the fast model, generating in seconds instead of timing
  out.

## [0.1.1] — 2026-07-21

### Added
- **Health History** — a standing background narrative plus a typed medical
  timeline (diagnoses, exposures, injuries, prescriptions, ER/hospital visits,
  appointments, doctor advice). Feeds the Companion and the Visit Report. (#67)
- **Records: topic search** — search a topic (e.g. "mold", "thyroid") to surface
  every related lab, matching analyte name, family, and category. (#71)
- **Records: unit conversion** — a curated conversion table normalizes the same
  analyte across units into one trend, with a Conventional / SI display toggle. (#76)

### Fixed
- **Dashboard** — collapsed duplicate heart-rate charts and the Oura/Fitbit
  metric overlap into one clear view (Fitbit for live physiology, Oura for its
  scores). (#72)
- **Bug reporter** — now files reports in-app instead of bouncing the reporter to
  GitHub's sign-in; nothing is lost regardless of GitHub configuration. (#77)
- **Records: cleaner lab trends** — the same test written different ways (e.g.
  serum "Cortisol" vs "Cortisol, A.M.") now merges into one continuous trend,
  while genuinely different tests — specimens, metabolites, and timed-panel
  points — stay separate.

### Changed
- **CI** — bumped the `docker/*` actions to their Node 24 majors, clearing the
  Node 20 deprecation warning. (#75)

## [0.1.0] — 2026-07-19

First public release. A self-hosted, single-user personal health platform
centered on Type 1 diabetes, with a health **Companion** that reasons across all
of your data.

### AI & assistants
- **Companion** — streaming chat grounded in your real records, with a persistent
  memory of your lived experience, multiple conversation threads, markdown
  answers, and a Fast/Deep local-model switch. Surfaces patterns and questions
  for your care team; never diagnoses or advises dosing.
- **Overview** — a cross-domain AI health summary spotting connections across your
  whole picture, not just glucose.
- **Visit Report** — a printable 90-day clinical summary (AGP, TIR, per-phase
  metrics, labs, conditions, medications, symptoms) with an AI "quarter in review".
- **Records** — upload lab reports and imaging (PDF/photo); a local vision model
  extracts values into per-analyte trend charts.
- **Pluggable LLM layer** — Anthropic API or any local OpenAI-compatible server
  (vLLM / Ollama), including a graceful on-demand quality model. With a local
  model, no health data leaves the machine.

### Data sources
- Glucose — Dexcom Share (real-time), Dexcom API v3, Nightscout.
- Insulin pump — Tandem Source (via `tconnectsync`), Glooko fallback.
- Wearables — Oura Ring (sleep / readiness / HRV / temperature) and
  Fitbit / Google Health (steps / heart rate / sleep / SpO₂ / breathing rate),
  including near-real-time heart rate.
- Cycle — menstrual phases inferred automatically from Oura nightly temperature.
- CSV / Base44 export bulk import.

### Tracking & analysis
- **Dashboard** — real-time glucose, TIR/GMI/CV, AGP, treatment timeline, live
  heart rate, and wearable overlays.
- **Explorer** — a zoomable/pannable glucose canvas with insulin, basal bands, IOB.
- **Patterns** — statistical + AI detection of recurring highs/lows, spikes, etc.
- **Insights** — cross-domain correlations (glucose × sleep × activity × cycle).
- **Insulin** — total daily dose, estimated insulin resistance, and
  correction-response / absorption statistics.
- **Wearables** — sleep, activity, HR/HRV, and SpO₂ deep-dives with glucose overlays.
- **Compare** — period-over-period comparison.
- **Symptom journal** — a nightly check-in (severity, duration, notes) woven into
  the Companion, analytics, and the report.
- **Clinical picture** — conditions, medications & allergies, profile, and
  insurance, entered once and fed to the AI and the Visit Report.
- **Fingerstick** logging with CGM matching.

### Platform
- One container (FastAPI + SQLite + a React/Vite/Tailwind SPA); state in a single
  Docker volume.
- **One-command local installer** (`install.sh`) plus `docker-compose.local.yml`.
- Server deploy behind Traefik / nginx / Caddy (`docs/DEPLOY.md`).
- Demo mode with synthetic seed data.
- Read-only provider login (role-gated) for sharing with a clinician.
- Argon2 password hashing and session auth.
- No telemetry and no phone-home.
- CI/CD via GitHub Actions; prebuilt images published to GHCR.

[Unreleased]: https://github.com/Paco5687/GlucoPilot/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/Paco5687/GlucoPilot/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Paco5687/GlucoPilot/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Paco5687/GlucoPilot/releases/tag/v0.1.0
