# Changelog

All notable changes to GlucoPilot are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/Paco5687/GlucoPilot/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Paco5687/GlucoPilot/releases/tag/v0.1.0
