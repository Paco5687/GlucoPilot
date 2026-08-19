"""TensorBreeze connector: transport, credential handling, and safety rails.

Every test drives a real Unix socket so the transport itself is exercised, not
mocked away. The rails under test are the ones that protect health data: images
never reach the text route, oversized output is refused rather than shrunk, a
failure never becomes a cloud fallback, and nothing sensitive reaches a log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingMixIn, UnixStreamServer

import pytest
from fastapi import HTTPException

from server import breeze

pytestmark = pytest.mark.risk_critical

GOOD_KEY = "bra_" + "s" * 40


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    routes: dict = {}
    seen: list = []

    def log_message(self, *args):  # keep pytest output clean
        pass

    def _respond(self, spec, body_in=None):
        status, payload, headers = spec(body_in) if callable(spec) else spec
        raw = json.dumps(payload).encode()
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, str(value))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        spec = self.routes.get(("GET", self.path.split("?")[0]))
        type(self).seen.append(("GET", self.path, dict(self.headers)))
        if spec is None:
            self._respond((404, {"error": "not found"}, {}))
            return
        self._respond(spec)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).seen.append(("POST", self.path, dict(self.headers), body))
        spec = self.routes.get(("POST", self.path.split("?")[0]))
        if spec is None:
            self._respond((404, {"error": "not found"}, {}))
            return
        self._respond(spec, body)


class _Server(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True
    address_family = socket.AF_UNIX

    def get_request(self):
        request, _ = self.socket.accept()
        return request, ("localhost", 0)


@pytest.fixture
def router(tmp_path, monkeypatch):
    """A real Unix-socket HTTP server standing in for the Breeze router."""
    sock_path = str(tmp_path / "breeze.sock")
    key_path = tmp_path / "breeze.key"
    key_path.write_text(GOOD_KEY + "\n")
    os.chmod(key_path, 0o600)  # owner-only, and rewritable by rotation tests

    handler = type("H", (_Handler,), {"routes": {}, "seen": []})
    server = _Server(sock_path, handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    monkeypatch.setenv("BREEZE_ENABLED", "true")
    monkeypatch.setenv("BREEZE_ROUTER_SOCKET", sock_path)
    monkeypatch.setenv("BREEZE_ROUTER_ORIGIN", "http://tensorbreeze")
    monkeypatch.setenv("BREEZE_CONNECTOR_API_KEY_FILE", str(key_path))
    monkeypatch.setenv("BREEZE_MODEL_ALIAS", "breeze-general-instruct")
    monkeypatch.setenv("BREEZE_VISION_POLICY", "local")
    monkeypatch.setenv("BREEZE_REQUEST_TIMEOUT_SECONDS", "10")
    breeze._health.record_ok()

    yield handler, key_path
    server.shutdown()
    server.server_close()


def _completion(content, usage=None, headers=None):
    return (200, {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": usage or {"prompt_tokens": 11, "completion_tokens": 7},
    }, headers or {"X-Request-ID": "req-1", "X-Breeze-Job-ID": "job-1"})


class TestDiscovery:
    def test_healthz_is_keyless_and_requires_live(self, router):
        handler, _ = router
        handler.routes[("GET", "/healthz")] = (200, {"live": True, "ready": False, "component": "Breeze Router"}, {})

        result = asyncio.run(breeze.health())

        assert result == {"live": True, "ready": False, "component": "Breeze Router"}
        # ready=false is informational, not an error — the call above succeeded.
        health_calls = [c for c in handler.seen if c[1] == "/healthz"]
        assert "Authorization" not in health_calls[0][2]

    def test_healthz_rejects_not_live(self, router):
        handler, _ = router
        handler.routes[("GET", "/healthz")] = (200, {"live": False, "component": "Breeze Router"}, {})
        with pytest.raises(breeze.BreezeError) as err:
            asyncio.run(breeze.health())
        assert err.value.status_code == 503

    def test_models_are_authenticated_and_expose_the_alias(self, router):
        handler, _ = router
        handler.routes[("GET", "/v1/models")] = (200, {"data": [{"id": "breeze-general-instruct"}]}, {})

        assert "breeze-general-instruct" in asyncio.run(breeze.models())
        call = [c for c in handler.seen if c[1] == "/v1/models"][0]
        assert call[2]["Authorization"] == f"Bearer {GOOD_KEY}"

    def test_model_status_values(self, router):
        handler, _ = router
        for reported in ("ready", "busy", "preparing", "unavailable", "no_providers"):
            handler.routes[("GET", "/v1/models/breeze-general-instruct/status")] = (200, {"state": reported}, {})
            assert asyncio.run(breeze.model_status()) == reported

    def test_model_capacity_exposes_route_limits(self, router):
        handler, _ = router
        handler.routes[("GET", "/v1/models/breeze-general-instruct/status")] = (200, {
            "state": "ready",
            "capabilities": {
                "max_context_tokens": 4096,
                "max_output_tokens": 4096,
                "supports_json_schema": True,
                "supports_streaming": False,
            },
        }, {})

        assert asyncio.run(breeze.model_capacity()) == {
            "state": "ready",
            "max_context_tokens": 4096,
            "max_output_tokens": 4096,
            "supports_json_schema": True,
            "supports_streaming": False,
        }

    def test_unknown_status_is_treated_as_unavailable(self, router):
        handler, _ = router
        handler.routes[("GET", "/v1/models/breeze-general-instruct/status")] = (200, {"state": "banana"}, {})
        assert asyncio.run(breeze.model_status()) == "unavailable"


class TestCompletion:
    def test_plain_completion_sends_only_permitted_fields(self, router):
        handler, _ = router
        handler.routes[("POST", "/v1/chat/completions")] = _completion(" hello from breeze ")

        result = asyncio.run(breeze.complete("say hi", None, 100))

        assert result == "hello from breeze"
        body = [c for c in handler.seen if c[1] == "/v1/chat/completions"][0][3]
        assert set(body) <= {"model", "messages", "temperature", "top_p", "max_tokens", "response_format"}
        # Explicitly never sent — the route does not accept them.
        for banned in ("stream", "stop", "frequency_penalty", "presence_penalty", "tools", "functions"):
            assert banned not in body
        assert body["model"] == "breeze-general-instruct"

    def test_structured_output_uses_strict_json_schema(self, router):
        handler, _ = router
        schema = {"type": "object", "title": "memories", "properties": {"a": {"type": "string"}}, "required": ["a"]}
        handler.routes[("POST", "/v1/chat/completions")] = _completion(json.dumps({"a": "value"}))

        result = asyncio.run(breeze.complete("extract", schema, 200))

        assert result == {"a": "value"}
        body = [c for c in handler.seen if c[1] == "/v1/chat/completions"][0][3]
        fmt = body["response_format"]
        assert fmt["type"] == "json_schema"
        assert fmt["json_schema"]["strict"] is True
        assert fmt["json_schema"]["name"] == "memories"
        assert fmt["json_schema"]["schema"] == schema

    def test_structured_output_is_validated_not_trusted(self, router):
        handler, _ = router
        schema = {"type": "object", "required": ["needed"]}
        handler.routes[("POST", "/v1/chat/completions")] = _completion(json.dumps({"other": 1}))

        with pytest.raises(breeze.BreezeError) as err:
            asyncio.run(breeze.complete("extract", schema, 200))
        assert "missing required field" in err.value.detail

    def test_non_json_structured_response_fails_loudly(self, router):
        handler, _ = router
        handler.routes[("POST", "/v1/chat/completions")] = _completion("I'm afraid I can't do that")
        with pytest.raises(breeze.BreezeError) as err:
            asyncio.run(breeze.complete("extract", {"type": "object"}, 200))
        assert "not valid JSON" in err.value.detail

    def test_oversized_output_is_refused_not_reduced(self, router):
        handler, _ = router
        handler.routes[("POST", "/v1/chat/completions")] = _completion("should never be reached")

        with pytest.raises(breeze.BreezeError) as err:
            asyncio.run(breeze.complete("big", None, breeze.MAX_OUTPUT_TOKENS + 1))

        assert err.value.status_code == 400
        assert str(breeze.MAX_OUTPUT_TOKENS) in err.value.detail
        # Refused before any request left the process.
        assert not [c for c in handler.seen if c[1] == "/v1/chat/completions"]

    def test_oversized_total_context_is_refused_not_truncated(self, router):
        handler, _ = router
        handler.routes[("POST", "/v1/chat/completions")] = _completion("never reached")

        with pytest.raises(breeze.BreezeError) as err:
            asyncio.run(breeze.complete(
                "clinical evidence",
                None,
                1200,
                prompt_tokens=5712,
            ))

        assert err.value.status_code == 400
        assert "silently truncating" in err.value.detail
        assert not [c for c in handler.seen if c[1] == "/v1/chat/completions"]

    def test_images_never_reach_the_text_route(self, router):
        handler, _ = router
        handler.routes[("POST", "/v1/chat/completions")] = _completion("should never be reached")

        with pytest.raises(breeze.BreezeError) as err:
            asyncio.run(breeze.complete("read this", None, 100, images=["image/png|AAAA"]))

        assert "text-only" in err.value.detail
        assert not [c for c in handler.seen if c[1] == "/v1/chat/completions"]


class TestCredential:
    def test_missing_credential_file_fails_closed(self, router, monkeypatch):
        handler, _ = router
        monkeypatch.setenv("BREEZE_CONNECTOR_API_KEY_FILE", "/nonexistent/breeze.key")
        with pytest.raises(breeze.BreezeError) as err:
            asyncio.run(breeze.complete("hi", None, 10))
        assert err.value.status_code == 503

    @pytest.mark.parametrize("contents", ["not-a-key\n", "bra_\n", "bra_abc\nbra_def\n", "", "   \n"])
    def test_malformed_credentials_are_rejected_without_echoing(self, router, contents):
        _, key_path = router
        key_path.write_text(contents)
        with pytest.raises(breeze.BreezeError) as err:
            asyncio.run(breeze.complete("hi", None, 10))
        # The message must never contain the file's contents.
        assert contents.strip() not in err.value.detail or not contents.strip()

    def test_credential_is_reread_each_request(self, router):
        handler, key_path = router
        handler.routes[("POST", "/v1/chat/completions")] = _completion("ok")
        asyncio.run(breeze.complete("first", None, 10))

        rotated = "bra_" + "r" * 40
        key_path.write_text(rotated + "\n")
        asyncio.run(breeze.complete("second", None, 10))

        posts = [c for c in handler.seen if c[1] == "/v1/chat/completions"]
        assert posts[-1][2]["Authorization"] == f"Bearer {rotated}"


class TestErrors:
    def test_401_reports_credential_failure(self, router):
        handler, _ = router
        handler.routes[("POST", "/v1/chat/completions")] = (401, {"error": "unauthorized"}, {})
        with pytest.raises(breeze.BreezeError) as err:
            asyncio.run(breeze.complete("hi", None, 10))
        assert "credential" in err.value.detail.lower()
        assert not err.value.retryable

    def test_403_reports_scope_failure(self, router):
        handler, _ = router
        handler.routes[("POST", "/v1/chat/completions")] = (403, {"error": "forbidden"}, {})
        with pytest.raises(breeze.BreezeError) as err:
            asyncio.run(breeze.complete("hi", None, 10))
        assert "scope" in err.value.detail.lower()
        assert not err.value.retryable

    @pytest.mark.parametrize("status", [429, 503])
    def test_429_and_503_are_retryable_and_honor_retry_after(self, router, status):
        handler, _ = router
        handler.routes[("POST", "/v1/chat/completions")] = (status, {"error": "busy"}, {"Retry-After": "12"})
        with pytest.raises(breeze.BreezeError) as err:
            asyncio.run(breeze.complete("hi", None, 10))
        assert err.value.retryable is True
        assert err.value.retry_after == 12.0

    def test_no_providers_is_surfaced_for_operator_attention(self, router):
        handler, _ = router
        handler.routes[("GET", "/v1/models/breeze-general-instruct/status")] = (200, {"state": "no_providers"}, {})
        # Non-retryable by nature: reported as-is so a caller does not spin.
        assert asyncio.run(breeze.model_status()) == "no_providers"

    def test_repeated_503s_warn_for_the_operator(self, router, caplog):
        handler, _ = router
        handler.routes[("POST", "/v1/chat/completions")] = (503, {"error": "busy"}, {})
        with caplog.at_level(logging.WARNING, logger="glucopilot.breeze"):
            for _ in range(breeze.RETRYABLE_503_WARN_COUNT):
                with pytest.raises(breeze.BreezeError):
                    asyncio.run(breeze.complete("hi", None, 10))
        assert any("degraded" in record.message for record in caplog.records)

    def test_unreachable_socket_does_not_fall_back(self, router, monkeypatch):
        monkeypatch.setenv("BREEZE_ROUTER_SOCKET", "/nonexistent/breeze.sock")
        with pytest.raises(breeze.BreezeError) as err:
            asyncio.run(breeze.complete("hi", None, 10))
        assert err.value.status_code == 503
        assert "unreachable" in err.value.detail


class TestLogging:
    def test_logs_carry_telemetry_but_no_prompt_completion_or_credential(self, router, caplog):
        handler, _ = router
        secret_prompt = "Emily's fasting glucose was 142 mg/dL on 2026-08-01"
        secret_reply = "Her A1c trend suggests..."
        handler.routes[("POST", "/v1/chat/completions")] = _completion(
            secret_reply, usage={"prompt_tokens": 321, "completion_tokens": 45},
            headers={"X-Request-ID": "req-9", "X-Breeze-Job-ID": "job-9",
                     "X-Breeze-Queue-Wait-Ms": "12", "X-Breeze-Execution-Ms": "340"},
        )
        with caplog.at_level(logging.INFO, logger="glucopilot.breeze"):
            asyncio.run(breeze.complete(secret_prompt, None, 100))

        logged = "\n".join(record.getMessage() for record in caplog.records)
        # Operational fields present...
        assert "req-9" in logged and "job-9" in logged
        assert "321" in logged and "45" in logged
        assert "breeze-general-instruct" in logged
        # ...and nothing that could carry PHI or a credential.
        assert "Emily" not in logged and "142" not in logged
        assert secret_reply not in logged
        assert GOOD_KEY not in logged and "bra_" not in logged

    def test_status_summary_never_exposes_the_credential(self, router):
        summary = breeze.status_summary()
        assert summary["credential"] == "present"
        assert GOOD_KEY not in json.dumps(summary)
        assert "bra_" not in json.dumps(summary)
        assert summary["enabled"] is True
        assert summary["max_output_tokens"] == breeze.MAX_OUTPUT_TOKENS
        assert summary["max_context_tokens"] == breeze.DEFAULT_MAX_CONTEXT_TOKENS


class TestProviderDispatch:
    """llm.invoke_llm must route by content type, and never degrade to cloud."""

    def test_text_goes_to_breeze_images_go_to_local_vision(self, router, monkeypatch):
        from server import llm

        monkeypatch.setattr(llm, "config_value", lambda name, default="": {
            "llm_provider": "breeze",
            "local_llm_url": "unix:///run/glucopilot/llm.sock",
            "local_llm_model": "qwen3-vl-8b",
        }.get(name, default))

        calls = {}

        async def fake_breeze(prompt, schema, max_tokens, **kw):
            calls["breeze"] = True
            return "text answer"

        async def fake_local(prompt, schema, max_tokens, images=None, **kw):
            calls["local_vision"] = images
            return {"ok": True}

        monkeypatch.setattr(llm.breeze, "complete", fake_breeze)
        monkeypatch.setattr(llm, "_invoke_local", fake_local)

        assert asyncio.run(llm.invoke_llm("plain text", max_tokens=100)) == "text answer"
        assert calls == {"breeze": True}

        calls.clear()
        asyncio.run(llm.invoke_llm("read this", max_tokens=100, images=["image/png|AAAA"]))
        # The image reached local vision intact — not stripped, not sent to Breeze.
        assert calls == {"local_vision": ["image/png|AAAA"]}

    def test_breeze_failure_never_falls_back_to_a_cloud_provider(self, router, monkeypatch):
        from server import llm

        monkeypatch.setattr(llm, "config_value", lambda name, default="": (
            "breeze" if name == "llm_provider" else default))

        async def boom(*args, **kwargs):
            raise breeze.BreezeError(503, "router down")

        async def must_not_run(*args, **kwargs):
            raise AssertionError("cloud provider was called — PHI must never leave on fallback")

        monkeypatch.setattr(llm.breeze, "complete", boom)
        monkeypatch.setattr(llm, "_invoke_anthropic", must_not_run)
        monkeypatch.setattr(llm, "_invoke_openai", must_not_run)

        with pytest.raises(breeze.BreezeError):
            asyncio.run(llm.invoke_llm("anything", max_tokens=10))

    def test_breeze_provider_requires_explicit_enablement(self, router, monkeypatch):
        from server import llm

        monkeypatch.setattr(llm, "config_value", lambda name, default="": (
            "breeze" if name == "llm_provider" else default))
        monkeypatch.setenv("BREEZE_ENABLED", "false")

        with pytest.raises(HTTPException) as err:
            asyncio.run(llm.invoke_llm("anything", max_tokens=10))

        assert err.value.status_code == 503
        assert "BREEZE_ENABLED is false" in err.value.detail

    def test_stream_yields_one_complete_chunk(self, router, monkeypatch):
        from server import llm

        monkeypatch.setattr(llm, "config_value", lambda name, default="": (
            "breeze" if name == "llm_provider" else default))

        async def fake_breeze(prompt, schema, max_tokens, **kw):
            assert schema is None
            return "a complete reply"

        monkeypatch.setattr(llm.breeze, "complete", fake_breeze)

        async def collect():
            return [chunk async for chunk in llm.invoke_llm_stream("hi", max_tokens=50, stop=["\n\n—"])]

        chunks = asyncio.run(collect())
        # One chunk, not fake token-sized slices.
        assert chunks == ["a complete reply"]
