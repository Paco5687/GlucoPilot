# Security Policy

GlucoPilot is a **self-hosted, single-user** health app. When you run it, your
health data lives in **your** deployment (a local SQLite database and a Docker
volume) on infrastructure **you** control. The maintainers never receive your
data, and the app has **no telemetry and makes no outbound "phone-home" calls** —
it only talks to the third-party services *you* explicitly connect (your CGM,
pump, wearables, etc.) and, for AI features, the model provider you configure
(which can be a **fully local** model, keeping PHI on your machine).

Because of that model, most of your data's safety is in the hands of whoever
operates the deployment. This policy covers vulnerabilities **in the GlucoPilot
code itself**.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately via GitHub's **[Report a vulnerability](https://github.com/Paco5687/GlucoPilot/security/advisories/new)**
(Security → Advisories → Report a vulnerability). Include:

- A description of the issue and its impact
- Steps to reproduce (a proof-of-concept if you have one)
- Affected version/commit and configuration

You can expect an initial acknowledgement within **7 days**. We'll work with you
on a fix and coordinate disclosure; credit is given to reporters who want it.

## In scope

- Authentication/session/authorization flaws (e.g. the admin/provider roles)
- Injection, SSRF, path traversal, or RCE in the backend
- Secrets or PHI leaking through logs, errors, or API responses
- Insecure defaults that expose data without the operator's intent
- Vulnerable dependencies with a practical exploit path

## Out of scope

- Issues that require an already-compromised host or malicious operator
- Findings against a deployment misconfiguration (e.g. exposing the app to the
  public internet without a reverse proxy / TLS, or reusing a weak
  `APP_SECRET_KEY`) — see [`docs/DEPLOY.md`](docs/DEPLOY.md) for hardening
- The behavior of third-party services or their unofficial APIs
- Anything about the accuracy of health analytics (it is **not** a medical
  device — see the README disclaimer)

## Supported versions

This is an actively developed project; fixes land on `main` and in the latest
release. Please reproduce against the latest `main` before reporting.

## Operator hardening (quick checklist)

- Set a strong, unique `APP_SECRET_KEY`; never commit your `.env`
- Run behind a reverse proxy with TLS; don't expose the raw port publicly
- Prefer the **local** AI model to keep PHI on-device
- Keep dependencies current (Dependabot PRs) and back up your data volume
