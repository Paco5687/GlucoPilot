"""LLM backend replacing Base44's Core.InvokeLLM.

Two providers, selected on the Settings page (llm_provider):
  - "anthropic": Anthropic Messages API; structured output via forced tool use.
  - "local": any OpenAI-compatible server (vLLM / Ollama). Structured output is
    requested via response_format json_object plus schema-in-prompt, with a
    defensive JSON extraction fallback — small local models are less reliable
    than forced tool use.
"""

import json
import re
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_admin
from .config import ANTHROPIC_API_URL
from .db import config_value

router = APIRouter(dependencies=[Depends(require_admin)])

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
LOCAL_URL_DEFAULT = "unix:///run/glucopilot/llm.sock"
LOCAL_MODEL_DEFAULT = "qwen3-vl-8b"


def _local_client_and_base(raw_url: str) -> tuple[httpx.AsyncClient, str]:
    """Support both unix:///path/to.sock (host vLLM via socket volume) and
    plain http(s):// OpenAI-compatible base URLs."""
    raw_url = raw_url.strip()
    if raw_url.startswith("unix://"):
        uds = raw_url[len("unix://"):]
        transport = httpx.AsyncHTTPTransport(uds=uds)
        # Host must be localhost: Ollama rejects other Host headers with 403
        # (DNS-rebinding guard); vLLM ignores it, so this is safe for both.
        return httpx.AsyncClient(transport=transport, timeout=300), "http://localhost/v1"
    return httpx.AsyncClient(timeout=300), raw_url.rstrip("/")


def _extract_json(text: str) -> Any:
    text = text.strip()
    # strip markdown fences and any <think> blocks defensively
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    # last resort: first {...} span
    brace = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if brace:
        try:
            return json.loads(brace.group(0))
        except ValueError:
            pass
    raise HTTPException(status_code=502, detail="LLM did not return valid JSON.")


async def _invoke_anthropic(
    prompt: str, response_json_schema: dict | None, max_tokens: int, images: list[str] | None = None
) -> Any:
    api_key = config_value("anthropic_api_key")
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="Anthropic API key is not configured. Add it on the Settings page (or switch to the local model).",
        )
    if images:
        content: Any = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64},
            }
            for media_type, b64 in (img.split("|", 1) for img in images)
        ] + [{"type": "text", "text": prompt}]
    else:
        content = prompt
    payload: dict[str, Any] = {
        "model": config_value("anthropic_model", "claude-sonnet-5"),
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content}],
    }
    if response_json_schema:
        payload["tools"] = [
            {
                "name": "structured_response",
                "description": "Return the structured response.",
                "input_schema": response_json_schema,
            }
        ]
        payload["tool_choice"] = {"type": "tool", "name": "structured_response"}

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            ANTHROPIC_API_URL,
            json=payload,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Anthropic API error: {response.text[:500]}")
    data = response.json()
    if response_json_schema:
        for block in data.get("content", []):
            if block.get("type") == "tool_use":
                return block.get("input")
        raise HTTPException(status_code=502, detail="LLM did not return structured output.")
    return "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")


async def _invoke_openai(prompt: str, response_json_schema: dict | None, max_tokens: int, images: list[str] | None) -> Any:
    api_key = config_value("openai_api_key")
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="OpenAI API key is not configured. Add it on the Settings page (or pick a different provider).",
        )
    if images:
        content: Any = [
            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}}
            for media_type, b64 in (img.split("|", 1) for img in images)
        ] + [{"type": "text", "text": prompt}]
    else:
        content = prompt
    payload: dict[str, Any] = {
        "model": config_value("openai_model", "gpt-4o-mini"),
        "max_completion_tokens": max_tokens,
        "messages": [{"role": "user", "content": content}],
    }
    if response_json_schema:
        # Force a function call so the reply is schema-shaped JSON.
        payload["tools"] = [
            {"type": "function", "function": {"name": "structured_response", "parameters": response_json_schema}}
        ]
        payload["tool_choice"] = {"type": "function", "function": {"name": "structured_response"}}

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            OPENAI_API_URL, json=payload, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"OpenAI API error: {response.text[:500]}")
    message = response.json()["choices"][0]["message"]
    if response_json_schema:
        calls = message.get("tool_calls") or []
        if calls:
            return json.loads(calls[0]["function"]["arguments"])
        raise HTTPException(status_code=502, detail="OpenAI did not return structured output.")
    return message.get("content") or ""


async def _invoke_local(
    prompt: str,
    response_json_schema: dict | None,
    max_tokens: int,
    images: list[str] | None = None,
    url_override: str | None = None,
    model_override: str | None = None,
) -> Any:
    raw_url = url_override or config_value("local_llm_url", LOCAL_URL_DEFAULT)
    model = model_override or config_value("local_llm_model", LOCAL_MODEL_DEFAULT)

    if response_json_schema:
        prompt = (
            f"{prompt}\n\n"
            f"Respond ONLY with a JSON object matching this JSON schema — no prose, no markdown fences:\n"
            f"{json.dumps(response_json_schema)}"
        )
    if images:
        # OpenAI-style multimodal content; Qwen3-VL handles data URLs.
        content: Any = [
            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}}
            for media_type, b64 in (img.split("|", 1) for img in images)
        ] + [{"type": "text", "text": prompt}]
    else:
        content = prompt
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content}],
    }
    if response_json_schema:
        payload["response_format"] = {"type": "json_object"}

    client, base_url = _local_client_and_base(raw_url)
    async with client:
        try:
            response = await client.post(f"{base_url}/chat/completions", json=payload)
        except (httpx.ConnectError, FileNotFoundError) as err:
            raise HTTPException(status_code=502, detail=f"Local LLM unreachable at {raw_url}: {err}")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Local LLM error: {response.text[:500]}")
    text = response.json()["choices"][0]["message"]["content"] or ""
    if response_json_schema:
        return _extract_json(text)
    # strip thinking blocks some local models emit
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


async def invoke_llm(
    prompt: str,
    response_json_schema: dict | None = None,
    max_tokens: int = 4000,
    images: list[str] | None = None,
    tier: str = "default",
) -> Any:
    """images: list of "media_type|base64" strings (e.g. "image/png|iVBOR...").

    tier="quality" opts a text-only task into the bigger, slower local model
    (e.g. Ollama gemma3:27b) when one is configured — used for the Visit Report
    narrative, where prose quality matters and calls are infrequent. Image
    tasks and frequent/interactive calls stay on the fast default (vision) model.
    With the Anthropic provider, tier is ignored (Claude already serves both).
    """
    provider = config_value("llm_provider", "anthropic").strip().lower()
    if provider == "local":
        if tier == "quality" and not images:
            q_url = config_value("quality_llm_url")
            q_model = config_value("quality_llm_model")
            if q_url and q_model:
                return await _invoke_local(
                    prompt, response_json_schema, max_tokens, None, url_override=q_url, model_override=q_model
                )
        return await _invoke_local(prompt, response_json_schema, max_tokens, images)
    if provider == "openai":
        return await _invoke_openai(prompt, response_json_schema, max_tokens, images)
    return await _invoke_anthropic(prompt, response_json_schema, max_tokens, images)


class _ThinkStripper:
    """Incrementally strips <think>...</think> spans from a streamed response,
    holding back partial tags that straddle chunk boundaries."""

    def __init__(self) -> None:
        self._buf = ""
        self._in = False

    @staticmethod
    def _partial_tail(s: str, tag: str) -> int:
        for n in range(min(len(s), len(tag) - 1), 0, -1):
            if tag.startswith(s[-n:]):
                return n
        return 0

    def feed(self, text: str) -> str:
        self._buf += text
        out: list[str] = []
        while True:
            if self._in:
                i = self._buf.find("</think>")
                if i == -1:
                    keep = self._partial_tail(self._buf, "</think>")
                    self._buf = self._buf[len(self._buf) - keep:] if keep else ""
                    break
                self._buf = self._buf[i + len("</think>"):]
                self._in = False
            else:
                i = self._buf.find("<think>")
                if i == -1:
                    keep = self._partial_tail(self._buf, "<think>")
                    out.append(self._buf[: len(self._buf) - keep] if keep else self._buf)
                    self._buf = self._buf[len(self._buf) - keep:] if keep else ""
                    break
                out.append(self._buf[:i])
                self._buf = self._buf[i + len("<think>"):]
                self._in = True
        return "".join(out)

    def flush(self) -> str:
        if self._in:
            return ""
        rest, self._buf = self._buf, ""
        return rest


async def _stream_local(prompt: str, max_tokens: int, url_override: str | None = None, model_override: str | None = None, stop: list[str] | None = None):
    raw_url = url_override or config_value("local_llm_url", LOCAL_URL_DEFAULT)
    model = model_override or config_value("local_llm_model", LOCAL_MODEL_DEFAULT)
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        # Anti-repetition sampling — the small local model otherwise loops.
        "temperature": 0.6,
        "top_p": 0.9,
        "frequency_penalty": 0.4,
        "presence_penalty": 0.3,
    }
    if stop:
        payload["stop"] = stop
    client, base_url = _local_client_and_base(raw_url)
    stripper = _ThinkStripper()
    async with client:
        try:
            async with client.stream("POST", f"{base_url}/chat/completions", json=payload) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    raise HTTPException(status_code=502, detail=f"Local LLM error: {body[:500]!r}")
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    delta = (chunk.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
                    if delta:
                        visible = stripper.feed(delta)
                        if visible:
                            yield visible
        except (httpx.ConnectError, FileNotFoundError) as err:
            raise HTTPException(status_code=502, detail=f"Local LLM unreachable at {raw_url}: {err}")
    tail = stripper.flush()
    if tail:
        yield tail


async def invoke_llm_stream(prompt: str, max_tokens: int = 700, tier: str = "default", stop: list[str] | None = None):
    """Yield reply text incrementally. The local provider streams token-by-token;
    cloud providers yield the full reply once (correct, just not chunked).

    tier="quality" streams from the bigger, slower local model (e.g. Ollama
    gemma3:27b) when one is configured — streaming makes that model usable
    interactively since tokens appear as they're generated."""
    provider = config_value("llm_provider", "anthropic").strip().lower()
    if provider == "local":
        url_override = model_override = None
        if tier == "quality":
            q_url = config_value("quality_llm_url")
            q_model = config_value("quality_llm_model")
            if q_url and q_model:
                url_override, model_override = q_url, q_model
        async for chunk in _stream_local(prompt, max_tokens, url_override, model_override, stop=stop):
            yield chunk
        return
    text = await invoke_llm(prompt, max_tokens=max_tokens, tier=tier)
    yield text if isinstance(text, str) else str(text)


class InvokeBody(BaseModel):
    prompt: str
    model: str | None = None  # accepted for shim compatibility; server picks the model
    response_json_schema: dict | None = None


@router.post("/api/llm/invoke")
async def invoke(body: InvokeBody):
    result = await invoke_llm(body.prompt, body.response_json_schema)
    return {"result": result}
