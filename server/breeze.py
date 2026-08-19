"""TensorBreeze connector — OpenAI-compatible text inference over a Unix socket.

GlucoPilot stays an independent application: its own packaging, database, auth
and deployment. This module only adds Breeze as an LLM *provider* for text.

Boundaries this module enforces, because health data is involved:

* **Text only.** Any call carrying images goes to the explicitly configured
  local vision model. `BREEZE_VISION_POLICY=local` is the only supported value;
  images are never stripped to make a request fit, and never sent here.
* **No silent cloud fallback.** A Breeze failure raises. It never degrades to
  Anthropic or OpenAI — that would move PHI off-premises without anyone asking.
* **No silent truncation.** Oversized output requests are rejected with the
  numbers, rather than quietly clamped to something that fits.
* **Credential from a protected file, read per request.** Never from the
  database, UI, source, argv, logs or diagnostics; never returned by any API.

Transport, credential provisioning, GPU placement and routing are the
operator's; this module consumes what it is given and nothing more.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException

from .config import env, env_bool

log = logging.getLogger("glucopilot.breeze")

# The route's ceiling. Requests above it are refused, not shrunk: quietly
# lowering a caller's max_tokens truncates clinical output mid-sentence.
MAX_OUTPUT_TOKENS = 4096

# Escalation thresholds for a degraded router (operator-facing, not user-facing).
RETRYABLE_503_WARN_COUNT = 3
UNAVAILABLE_WARN_SECONDS = 10 * 60
UNAVAILABLE_ESCALATE_SECONDS = 30 * 60

_STATUS_VALUES = {"ready", "busy", "preparing", "unavailable", "no_providers"}


def enabled() -> bool:
    return env_bool("BREEZE_ENABLED", False)


def socket_path() -> str:
    return env("BREEZE_ROUTER_SOCKET", "/run/glucopilot/breeze-router.sock")


def origin() -> str:
    return env("BREEZE_ROUTER_ORIGIN", "http://tensorbreeze")


def key_file() -> str:
    return env("BREEZE_CONNECTOR_API_KEY_FILE", "/run/secrets/breeze-connector.key")


def model_alias() -> str:
    return env("BREEZE_MODEL_ALIAS", "breeze-general-instruct")


def request_timeout() -> float:
    try:
        return float(env("BREEZE_REQUEST_TIMEOUT_SECONDS", "360"))
    except ValueError:
        return 360.0


def vision_policy() -> str:
    return env("BREEZE_VISION_POLICY", "local").strip().lower()


class BreezeError(HTTPException):
    """A Breeze failure. Carries no prompt, completion or credential material."""

    def __init__(self, status_code: int, detail: str, *, retryable: bool = False,
                 retry_after: float | None = None, request_id: str | None = None):
        super().__init__(status_code=status_code, detail=detail)
        self.retryable = retryable
        self.retry_after = retry_after
        self.request_id = request_id


@dataclass
class _Health:
    """Rolling view of router trouble, for operator-facing escalation only."""
    consecutive_503: int = 0
    unavailable_since: float | None = None

    def record_ok(self) -> None:
        self.consecutive_503 = 0
        self.unavailable_since = None

    def record_unavailable(self, *, is_503: bool) -> str | None:
        now = time.monotonic()
        if is_503:
            self.consecutive_503 += 1
        if self.unavailable_since is None:
            self.unavailable_since = now
        elapsed = now - self.unavailable_since
        if elapsed >= UNAVAILABLE_ESCALATE_SECONDS:
            return "escalate"
        if self.consecutive_503 >= RETRYABLE_503_WARN_COUNT or elapsed >= UNAVAILABLE_WARN_SECONDS:
            return "warn"
        return None


_health = _Health()


def _read_credential() -> str:
    """Load and format-check the connector key. Never logged or returned.

    Checked at request time rather than cached so a rotated or revoked key
    takes effect immediately, and so a key removed from under a running
    container fails closed.
    """
    path = Path(key_file())
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise BreezeError(503, f"Breeze credential file is missing at {path}. Ask the operator to provision it.")
    except PermissionError:
        raise BreezeError(503, f"Breeze credential file at {path} is not readable by this container.")
    except OSError as err:
        raise BreezeError(503, f"Breeze credential file at {path} could not be read ({type(err).__name__}).")

    lines = [line for line in raw.splitlines() if line.strip()]
    if len(lines) != 1:
        raise BreezeError(503, "Breeze credential file must contain exactly one non-empty line.")
    token = lines[0].strip()
    if not token.startswith("bra_") or len(token) <= len("bra_"):
        # Says what is wrong without echoing any part of the value — and without
        # repeating the prefix, so "no credential-shaped text in any message or
        # log" stays an absolute, easily-audited rule.
        raise BreezeError(503, "Breeze credential is malformed (expected one line with the documented connector-key prefix).")
    return token


def _client() -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(uds=socket_path())
    return httpx.AsyncClient(transport=transport, base_url=origin(), timeout=request_timeout())


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _telemetry(response: httpx.Response, body: dict[str, Any] | None, total_ms: float) -> dict[str, Any]:
    """Operational fields only — deliberately no prompt, completion or schema."""
    usage = (body or {}).get("usage") or {}
    return {
        "status": response.status_code,
        "total_ms": round(total_ms, 1),
        "request_id": response.headers.get("X-Request-ID"),
        "job_id": response.headers.get("X-Breeze-Job-ID"),
        "queue_wait_ms": response.headers.get("X-Breeze-Queue-Wait-Ms"),
        "execution_ms": response.headers.get("X-Breeze-Execution-Ms"),
        "model": model_alias(),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
    }


def _raise_for_status(response: httpx.Response) -> None:
    code = response.status_code
    if code < 400:
        return
    request_id = response.headers.get("X-Request-ID")
    if code == 401:
        _health.record_unavailable(is_503=False)
        raise BreezeError(502, "Breeze rejected the connector credential (401). It may be expired or revoked.",
                          request_id=request_id)
    if code == 403:
        _health.record_unavailable(is_503=False)
        raise BreezeError(502, "Breeze denied this request's scope (403). Operator attention required.",
                          request_id=request_id)
    if code in (429, 503):
        retry_after = _retry_after(response)
        level = _health.record_unavailable(is_503=(code == 503))
        if level:
            log.warning("breeze router degraded (%s): status=%s consecutive_503=%s",
                        level, code, _health.consecutive_503)
        raise BreezeError(503, f"Breeze is temporarily unavailable ({code}).",
                          retryable=True, retry_after=retry_after, request_id=request_id)
    _health.record_unavailable(is_503=False)
    raise BreezeError(502, f"Breeze request failed with status {code}.", request_id=request_id)


async def health() -> dict[str, Any]:
    """Keyless liveness. `ready=false` is informational, not a failure."""
    async with _client() as client:
        try:
            response = await client.get("/healthz")
        except (httpx.ConnectError, httpx.TimeoutException, FileNotFoundError, OSError) as err:
            raise BreezeError(503, f"Breeze router socket is unreachable at {socket_path()} ({type(err).__name__}).")
    if response.status_code != 200:
        raise BreezeError(503, f"Breeze /healthz returned {response.status_code}.")
    body = response.json() if response.text else {}
    if not body.get("live"):
        raise BreezeError(503, "Breeze router reports live=false.")
    return {
        "live": True,
        "ready": bool(body.get("ready")),
        "component": body.get("component"),
    }


async def models() -> list[str]:
    """Authenticated discovery. The configured alias must be present."""
    token = _read_credential()
    async with _client() as client:
        response = await client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    _raise_for_status(response)
    body = response.json() if response.text else {}
    return [entry.get("id") for entry in (body.get("data") or []) if entry.get("id")]


async def model_status() -> str:
    token = _read_credential()
    async with _client() as client:
        response = await client.get(
            f"/v1/models/{model_alias()}/status", headers={"Authorization": f"Bearer {token}"}
        )
    _raise_for_status(response)
    body = response.json() if response.text else {}
    status = str(body.get("state") or "").strip().lower()
    return status if status in _STATUS_VALUES else "unavailable"


def _strict_response_format(schema: dict[str, Any], name: str) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "strict": True, "schema": schema},
    }


def _schema_name(response_json_schema: dict[str, Any]) -> str:
    """A stable, route-safe name. Derived from the schema's own shape so it does
    not change between identical calls, and carries no PHI."""
    title = str(response_json_schema.get("title") or "").strip()
    candidate = title or "glucopilot_structured_output"
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in candidate)[:60]
    return safe or "glucopilot_structured_output"


async def complete(
    prompt: str,
    response_json_schema: dict[str, Any] | None,
    max_tokens: int,
    *,
    images: list[str] | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    schema_name: str | None = None,
) -> Any:
    """One non-streaming completion. Text only; images are a caller error here."""
    if images:
        # Defence in depth — llm.invoke_llm routes image calls to local vision
        # before reaching this module.
        raise BreezeError(500, "Breeze is a text-only route; image requests must use the local vision model.")
    if vision_policy() != "local":
        raise BreezeError(500, f"Unsupported BREEZE_VISION_POLICY={vision_policy()!r}; only 'local' is supported.")
    if max_tokens > MAX_OUTPUT_TOKENS:
        raise BreezeError(
            400,
            f"Requested {max_tokens} output tokens; the Breeze route allows at most {MAX_OUTPUT_TOKENS}. "
            "Refusing rather than silently reducing the limit.",
        )

    token = _read_credential()
    # Exactly the permitted fields — no stream/stop/penalties/tools/model paths.
    payload: dict[str, Any] = {
        "model": model_alias(),
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if top_p is not None:
        payload["top_p"] = top_p
    if response_json_schema:
        payload["response_format"] = _strict_response_format(
            response_json_schema, schema_name or _schema_name(response_json_schema)
        )

    started = time.monotonic()
    async with _client() as client:
        try:
            response = await client.post(
                "/v1/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
        except (httpx.ConnectError, FileNotFoundError, OSError) as err:
            raise BreezeError(503, f"Breeze router socket is unreachable at {socket_path()} ({type(err).__name__}).")
        except httpx.TimeoutException:
            # Ambiguous: the job may have run. Never auto-retried.
            raise BreezeError(504, f"Breeze request timed out after {request_timeout():.0f}s. Not retried automatically.")
    total_ms = (time.monotonic() - started) * 1000
    _raise_for_status(response)

    body = response.json() if response.text else {}
    log.info("breeze completion %s", json.dumps(_telemetry(response, body, total_ms), default=str))
    _health.record_ok()

    choices = body.get("choices") or []
    if not choices:
        raise BreezeError(502, "Breeze returned no choices.")
    content = (choices[0].get("message") or {}).get("content") or ""
    if not response_json_schema:
        return content.strip()

    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        raise BreezeError(502, "Breeze structured output was not valid JSON.")
    _validate_against_schema(parsed, response_json_schema)
    return parsed


def _validate_against_schema(value: Any, schema: dict[str, Any]) -> None:
    """Check the strict-mode result actually satisfies the caller's schema.

    Structured output is how downstream code avoids parsing prose, so a shape
    mismatch has to fail loudly rather than propagate a half-built object.
    """
    expected = schema.get("type")
    if expected == "object" and not isinstance(value, dict):
        raise BreezeError(502, "Breeze structured output did not match the requested schema (expected an object).")
    if expected == "array" and not isinstance(value, list):
        raise BreezeError(502, "Breeze structured output did not match the requested schema (expected an array).")
    for field in schema.get("required") or []:
        if isinstance(value, dict) and field not in value:
            raise BreezeError(502, f"Breeze structured output is missing required field {field!r}.")


def status_summary() -> dict[str, Any]:
    """Non-secret configuration for the Settings page.

    Reports only whether the credential file is present and well-formed —
    never its path contents, and never any part of the value.
    """
    credential_state = "missing"
    if enabled():
        try:
            _read_credential()
            credential_state = "present"
        except BreezeError:
            credential_state = "unusable"
    else:
        credential_state = "present" if Path(key_file()).exists() else "missing"
    return {
        "enabled": enabled(),
        "socket": socket_path(),
        "origin": origin(),
        "model_alias": model_alias(),
        "vision_policy": vision_policy(),
        "request_timeout_seconds": request_timeout(),
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "socket_present": Path(socket_path()).exists(),
        "credential": credential_state,
    }
