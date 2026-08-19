# TensorBreeze connector

GlucoPilot connects to TensorBreeze as an **independent application**. It keeps
its own packaging, database, authentication and deployment; Breeze is added
only as an LLM provider for **text** inference.

## What the operator provisions

Nothing in this repository configures SSH, DNS, Traefik, firewall rules, GPU
placement, node names, provider addresses or model files. The operator supplies:

| Thing | Default path |
|---|---|
| Router socket (mounted into the container) | `/run/glucopilot/breeze-router.sock` |
| Connector credential file (read-only) | `/run/secrets/breeze-connector.key` |

The credential is one line with the documented connector-key prefix. It is read
from the file **at request time** — never from the database, the Settings UI,
source, argv, logs, or any diagnostic output, and no endpoint returns it.

## Configuration

All values are environment variables (see `docker-compose.yml`):

    BREEZE_ENABLED=false                 # default; activation is a deliberate act
    BREEZE_ROUTER_SOCKET=/run/glucopilot/breeze-router.sock
    BREEZE_ROUTER_ORIGIN=http://tensorbreeze
    BREEZE_CONNECTOR_API_KEY_FILE=/run/secrets/breeze-connector.key
    BREEZE_MODEL_ALIAS=breeze-general-instruct
    BREEZE_REQUEST_TIMEOUT_SECONDS=360
    BREEZE_VISION_POLICY=local

Select the provider with `llm_provider = breeze` on the Settings page. The
Settings API reports non-secret connector status only (enabled, socket path,
alias, whether a usable credential is present) — never the credential itself.

## Boundaries

* **Text only.** Any call carrying images goes to the explicitly configured
  local vision model. Images are never stripped to make a request fit and never
  sent to the Breeze text route. If local vision is unavailable, the call fails.
* **No silent cloud fallback.** A Breeze failure raises. It never degrades to
  Anthropic or OpenAI — that would move health data off-premises unasked.
* **No silent truncation.** A request above the route's 4096 output-token
  ceiling is refused with both numbers, not quietly clamped.
* **Non-streaming.** Breeze answers in one piece today. `invoke_llm_stream()`
  makes one complete request and yields it as a single chunk, so the NDJSON
  interface above it is unchanged. Token streaming is not faked.
* **Structured output** uses strict `json_schema` and the result is validated
  against the caller's schema rather than trusted.

## Operational signals

* `GET /healthz` is keyless; `live=true` is required, `ready=false` is
  informational.
* `GET /v1/models` (authenticated) must expose the alias.
* `GET /v1/models/<alias>/status` reports `ready`, `busy`, `preparing`,
  `unavailable`, or `no_providers`.
* `Retry-After` is honored on 429 and 503. `401` is a bad or expired
  credential; `403` is a scope failure; `no_providers` is non-retryable and
  needs operator attention.
* A degraded router warns after three retryable 503s or ten minutes
  unavailable, and escalates after thirty.
* An ambiguous completion (e.g. a timeout that may have run) is **never**
  retried automatically.
* Logs carry status, latency, `X-Request-ID`, `X-Breeze-Job-ID`, queue wait,
  execution and total time, model alias and token counts — never prompts,
  completions, schemas, images, credentials or PHI.

## Context sizing

Breeze's current route caps output at 4096 tokens. Measure prompts before
enabling: `server/health_summary.py` and the Companion reply prompt are the
largest, and both exceed a 4096-token *context* window if that is also the
route's input limit. See the connector report for current measurements.
