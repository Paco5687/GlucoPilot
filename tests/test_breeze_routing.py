"""Hybrid text routing for the Breeze provider.

BREEZE_ENABLED=true makes Breeze AVAILABLE for eligible calls; it does not make
it a global text replacement. These tests pin the deterministic pre-dispatch
decision: which call sites may use Breeze at all, that fit is measured (never
guessed) before any HTTP request, and that a failure after dispatch surfaces
without retry or fallback.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingMixIn, UnixStreamServer

import pytest

from server import breeze, llm

pytestmark = pytest.mark.risk_critical

GOOD_KEY = "bra_" + "s" * 40


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    routes: dict = {}
    seen: list = []

    def log_message(self, *args):
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
        type(self).seen.append(("GET", self.path, dict(self.headers)))
        self._respond(self.routes.get(("GET", self.path.split("?")[0]), (404, {"error": "nf"}, {})))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).seen.append(("POST", self.path, dict(self.headers), body))
        self._respond(self.routes.get(("POST", self.path.split("?")[0]), (404, {"error": "nf"}, {})), body)


class _Server(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True
    address_family = socket.AF_UNIX

    def get_request(self):
        request, _ = self.socket.accept()
        return request, ("localhost", 0)


@pytest.fixture
def router(tmp_path, monkeypatch):
    sock_path = str(tmp_path / "breeze.sock")
    key_path = tmp_path / "breeze.key"
    key_path.write_text(GOOD_KEY + "\n")

    handler = type("H", (_Handler,), {"routes": {}, "seen": []})
    server = _Server(sock_path, handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    monkeypatch.setenv("BREEZE_ENABLED", "true")
    monkeypatch.setenv("BREEZE_ROUTER_SOCKET", sock_path)
    monkeypatch.setenv("BREEZE_ROUTER_ORIGIN", "http://tensorbreeze")
    monkeypatch.setenv("BREEZE_CONNECTOR_API_KEY_FILE", str(key_path))
    monkeypatch.setenv("BREEZE_MODEL_ALIAS", "breeze-general-instruct")
    monkeypatch.setenv("BREEZE_VISION_POLICY", "local")
    monkeypatch.setenv("BREEZE_REQUEST_TIMEOUT_SECONDS", "10")
    monkeypatch.delenv("BREEZE_MAX_CONTEXT_TOKENS", raising=False)
    breeze._health.record_ok()
    breeze._reset_capability_cache()

    handler.routes[("GET", "/v1/models/breeze-general-instruct/status")] = (200, {"state": "ready"}, {})
    handler.routes[("POST", "/v1/chat/completions")] = (200, {
        "choices": [{"message": {"role": "assistant", "content": "breeze says hi"}}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 3},
    }, {"X-Request-ID": "req-r"})

    yield handler
    server.shutdown()
    server.server_close()
    breeze._reset_capability_cache()


def _use_breeze_provider(monkeypatch):
    monkeypatch.setattr(llm, "config_value", lambda name, default="": (
        "breeze" if name == "llm_provider" else default))


def _tokens(value):
    async def counter(_prompt):
        return value
    return counter


def _route(site, prompt="short prompt", max_tokens=100, counter=None):
    return asyncio.run(breeze.route_text_call(
        site, prompt, max_tokens, counter or _tokens(50)))


class TestRouteMatrix:
    def test_eligible_sites_route_to_breeze_when_they_fit(self, router):
        for site in ("companion_distill", "record_title_backfill", "companion_memory_extract"):
            route, details = _route(site)
            assert route == "breeze", site
            assert details["prompt_tokens"] == 50

    def test_every_kept_local_site_routes_local_by_policy(self, router):
        for site in ("companion_reply", "companion_repair", "health_summary",
                     "visit_narrative", "insights", "patterns",
                     "record_extraction", "insurance_extraction", "", "unknown_site"):
            route, _ = _route(site)
            assert route == "local_text", site
        # Policy, not fit: no completion request was ever attempted.
        assert not [c for c in router.seen if c[1] == "/v1/chat/completions"]

    def test_allowlist_matches_the_agreed_policy(self):
        assert breeze.BREEZE_ELIGIBLE_SITES == {
            "companion_distill", "record_title_backfill", "companion_memory_extract"}
        assert breeze.STATICALLY_TINY_SITES == {"companion_distill", "record_title_backfill"}

    def test_disabled_prevents_all_breeze_dispatch(self, router, monkeypatch):
        monkeypatch.setenv("BREEZE_ENABLED", "false")
        for site in ("companion_distill", "record_title_backfill", "companion_memory_extract"):
            route, _ = _route(site)
            assert route == "local_text", site
        assert not [c for c in router.seen if c[1].startswith("/v1")]


class TestFit:
    def test_oversized_call_routes_local_before_any_http_request(self, router):
        # 5,712-token companion-sized prompt against a 4,096 window.
        route, _ = _route("companion_memory_extract", max_tokens=500, counter=_tokens(5712))
        assert route == "local_text"
        assert not [c for c in router.seen if c[1] == "/v1/chat/completions"]

    def test_exact_budget_boundary(self, router):
        limit = breeze.DEFAULT_MAX_CONTEXT_TOKENS
        overhead = breeze.CHAT_TEMPLATE_OVERHEAD_TOKENS
        fits = limit - 500 - overhead
        route, _ = _route("companion_memory_extract", max_tokens=500, counter=_tokens(fits))
        assert route == "breeze"
        route, _ = _route("companion_memory_extract", max_tokens=500, counter=_tokens(fits + 1))
        assert route == "local_text"

    def test_missing_token_count_fails_closed_for_conditional_sites(self, router):
        route, _ = _route("companion_memory_extract", counter=_tokens(None))
        assert route == "local_text"
        assert not [c for c in router.seen if c[1] == "/v1/chat/completions"]

    def test_statically_tiny_sites_may_use_the_byte_guard(self, router):
        route, details = _route("companion_distill", prompt="tiny", max_tokens=24, counter=_tokens(None))
        assert route == "breeze"
        assert details["prompt_tokens"] == len(b"tiny")

        # ...but the byte bound is conservative: an implausibly large "tiny"
        # prompt still refuses.
        route, _ = _route("companion_distill", prompt="x" * 5000, max_tokens=24, counter=_tokens(None))
        assert route == "local_text"

    def test_output_reservation_counts_against_the_budget(self, router):
        # The reservation is real arithmetic, never a reduced max_tokens: 4,000
        # output plus a 100-token prompt overflows the 4,096 window and routes
        # local, while the same reservation with a 10-token prompt fits.
        route, _ = _route("companion_memory_extract", max_tokens=4000, counter=_tokens(100))
        assert route == "local_text"
        route, _ = _route("companion_memory_extract", max_tokens=4000, counter=_tokens(10))
        assert route == "breeze"

    def test_output_above_the_route_ceiling_routes_local(self, router):
        route, _ = _route("companion_memory_extract", max_tokens=5000, counter=_tokens(10))
        assert route == "local_text"


class TestCapabilityDiscovery:
    def test_discovered_limits_override_the_environment_fallback(self, router):
        router.routes[("GET", "/v1/models/breeze-general-instruct/status")] = (200, {
            "state": "ready",
            "capabilities": {"max_context_tokens": 8192, "max_output_tokens": 2048},
        }, {})
        breeze._reset_capability_cache()

        limits = asyncio.run(breeze.discovered_limits())
        assert limits == {"max_context_tokens": 8192, "max_output_tokens": 2048}

        # A 5,000-token prompt now fits the discovered 8,192 window...
        route, _ = _route("companion_memory_extract", max_tokens=500, counter=_tokens(5000))
        assert route == "breeze"
        # ...while output above the discovered 2,048 ceiling routes local.
        route, _ = _route("companion_memory_extract", max_tokens=3000, counter=_tokens(10))
        assert route == "local_text"

    def test_absent_capabilities_keep_the_compat_default(self, router):
        router.routes[("GET", "/v1/models/breeze-general-instruct/status")] = (200, {"state": "ready"}, {})
        breeze._reset_capability_cache()
        assert asyncio.run(breeze.discovered_limits()) == {
            "max_context_tokens": breeze.DEFAULT_MAX_CONTEXT_TOKENS,
            "max_output_tokens": breeze.MAX_OUTPUT_TOKENS,
        }

    def test_malformed_capabilities_never_mean_unlimited(self, router):
        router.routes[("GET", "/v1/models/breeze-general-instruct/status")] = (200, {
            "state": "ready",
            "capabilities": {"max_context_tokens": "lots", "max_output_tokens": -1},
        }, {})
        breeze._reset_capability_cache()
        assert asyncio.run(breeze.discovered_limits()) == {
            "max_context_tokens": breeze.DEFAULT_MAX_CONTEXT_TOKENS,
            "max_output_tokens": breeze.MAX_OUTPUT_TOKENS,
        }

    def test_discovery_failure_uses_fallback_and_is_not_cached(self, router, monkeypatch):
        monkeypatch.setenv("BREEZE_CONNECTOR_API_KEY_FILE", "/nonexistent/key")
        breeze._reset_capability_cache()
        assert asyncio.run(breeze.discovered_limits())["max_context_tokens"] == breeze.DEFAULT_MAX_CONTEXT_TOKENS
        assert breeze._capability_cache is None

    def test_env_fallback_is_configurable_but_never_unlimited(self, router, monkeypatch):
        monkeypatch.setenv("BREEZE_MAX_CONTEXT_TOKENS", "2048")
        router.routes[("GET", "/v1/models/breeze-general-instruct/status")] = (200, {"state": "ready"}, {})
        breeze._reset_capability_cache()
        assert asyncio.run(breeze.discovered_limits())["max_context_tokens"] == 2048
        monkeypatch.setenv("BREEZE_MAX_CONTEXT_TOKENS", "garbage")
        breeze._reset_capability_cache()
        assert asyncio.run(breeze.discovered_limits())["max_context_tokens"] == breeze.DEFAULT_MAX_CONTEXT_TOKENS


class TestEndToEndDispatch:
    def test_eligible_call_reaches_breeze_with_measured_tokens(self, router, monkeypatch):
        _use_breeze_provider(monkeypatch)
        monkeypatch.setattr(llm, "count_tokens", _tokens(41))

        result = asyncio.run(llm.invoke_llm("what to search", max_tokens=24, site="companion_distill"))
        assert result == "breeze says hi"
        posts = [c for c in router.seen if c[1] == "/v1/chat/completions"]
        assert len(posts) == 1

    def test_local_site_never_touches_breeze(self, router, monkeypatch):
        _use_breeze_provider(monkeypatch)

        async def local(prompt, schema, max_tokens, images=None, **kw):
            return "local reply"

        monkeypatch.setattr(llm, "_invoke_local", local)
        result = asyncio.run(llm.invoke_llm("big evidence prompt", max_tokens=1200, site="companion_reply"))
        assert result == "local reply"
        assert not [c for c in router.seen if c[1] == "/v1/chat/completions"]

    def test_images_route_to_local_vision_never_breeze(self, router, monkeypatch):
        _use_breeze_provider(monkeypatch)
        seen = {}

        async def local(prompt, schema, max_tokens, images=None, **kw):
            seen["images"] = images
            return {"ok": True}

        monkeypatch.setattr(llm, "_invoke_local", local)
        # Even an otherwise-eligible site: images always win the routing decision.
        asyncio.run(llm.invoke_llm("read", max_tokens=24, site="companion_distill",
                                   images=["image/png|AAAA"]))
        assert seen["images"] == ["image/png|AAAA"]
        assert not [c for c in router.seen if c[1] == "/v1/chat/completions"]

    @pytest.mark.parametrize("status", [429, 503])
    def test_failure_after_dispatch_surfaces_without_fallback(self, router, monkeypatch, status):
        _use_breeze_provider(monkeypatch)
        monkeypatch.setattr(llm, "count_tokens", _tokens(20))
        router.routes[("POST", "/v1/chat/completions")] = (status, {"error": "busy"}, {"Retry-After": "5"})

        async def must_not_run(*args, **kwargs):
            raise AssertionError("fell back to another provider after Breeze dispatch")

        monkeypatch.setattr(llm, "_invoke_local", must_not_run)
        monkeypatch.setattr(llm, "_invoke_anthropic", must_not_run)
        monkeypatch.setattr(llm, "_invoke_openai", must_not_run)

        with pytest.raises(breeze.BreezeError) as err:
            asyncio.run(llm.invoke_llm("hi", max_tokens=24, site="companion_distill"))
        assert err.value.retryable
        # One dispatch, no retry.
        assert len([c for c in router.seen if c[1] == "/v1/chat/completions"]) == 1

    def test_structured_output_stays_strictly_validated_through_routing(self, router, monkeypatch):
        _use_breeze_provider(monkeypatch)
        monkeypatch.setattr(llm, "count_tokens", _tokens(30))
        router.routes[("POST", "/v1/chat/completions")] = (200, {
            "choices": [{"message": {"role": "assistant", "content": json.dumps({"wrong": 1})}}],
            "usage": {},
        }, {})

        schema = {"type": "object", "required": ["memories"]}
        with pytest.raises(breeze.BreezeError) as err:
            asyncio.run(llm.invoke_llm("extract", response_json_schema=schema,
                                       max_tokens=500, site="companion_memory_extract"))
        assert "missing required field" in err.value.detail

    def test_stream_local_site_uses_real_local_streaming(self, router, monkeypatch):
        _use_breeze_provider(monkeypatch)

        async def fake_local_stream(prompt, max_tokens, url_override=None, model_override=None, stop=None):
            for piece in ("tok1 ", "tok2"):
                yield piece

        monkeypatch.setattr(llm, "_stream_local", fake_local_stream)

        async def collect():
            return [c async for c in llm.invoke_llm_stream("q", max_tokens=1200, site="companion_reply")]

        # The interactive reply keeps true token streaming via the local path.
        assert asyncio.run(collect()) == ["tok1 ", "tok2"]
        assert not [c for c in router.seen if c[1] == "/v1/chat/completions"]
