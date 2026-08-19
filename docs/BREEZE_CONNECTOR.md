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
    BREEZE_MAX_CONTEXT_TOKENS=4096
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
  ceiling, or whose prompt plus output exceeds the configured total context,
  is refused with both numbers, not quietly clamped.
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

## Hybrid routing

`BREEZE_ENABLED=true` makes Breeze **available for eligible calls** — it is not
a global all-text replacement. With `llm_provider = breeze`, every call gets a
deterministic routing decision before any inference request:

1. Image input → the existing local vision provider, always.
2. Breeze-eligible text that fits the discovered context budget → Breeze.
3. Statically ineligible text, or text that does not fit → the existing local
   text provider (including its quality-tier diversion).
4. A failure after a Breeze request has begun — timeout, 401, 403, 429, 503,
   malformed output — surfaces as a failure. No retry, no fallback to local or
   cloud. Pre-dispatch local routing for a capability limit is a routing
   decision, not failure fallback.

Eligible call sites (`BREEZE_ELIGIBLE_SITES`): companion query distillation,
record title backfill, and memory extraction when it measurably fits. All
other sites — the interactive Companion reply (which keeps true local token
streaming), grounding repair, health summary, visit narrative, insights,
patterns, and both image extractors — stay local. No site ever has its
`max_tokens` reduced to make it eligible.

Fit is `prompt_tokens + requested_output + 32-token chat overhead` against the
discovered context window, using the local tokenizer via `count_tokens()`.
When token counting is unavailable, conditional sites route local; the two
statically tiny sites may fall back to a conservative byte-count upper bound.

Capability discovery reads `capabilities` from the authenticated model-status
endpoint (`max_context_tokens`, `max_output_tokens`, cached briefly).
Absent or malformed values fall back to `BREEZE_MAX_CONTEXT_TOKENS` (default
4096) and the fixed 4096 output ceiling — never to "unlimited".

Routing is logged PHI-free: site identifier, chosen route, reason (eligible /
site_policy / context_exceeded / output_exceeded / vision / disabled /
token_count_unavailable), token counts and the limit in force.

## Context sizing

Breeze's current route has a 4096-token total context window and separately
caps output at 4096 tokens. The Connector measures text before dispatch and
fails closed when prompt plus reserved output does not fit. The Companion
reply and health-summary prompts therefore remain ineligible for this alias;
they must not be truncated to make them fit.
