"""In-app settings: API keys and app options, stored in the DB (app_settings
with a cfg_ prefix) and overriding .env values of the same name.

Secrets are never echoed back — GET returns configured/source status plus a
short hint. Sending an empty string clears the DB override (falls back to env).
"""

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_admin
from .db import config_value, get_setting, set_config_value

router = APIRouter(dependencies=[Depends(require_admin)])

SECRET_KEYS = ("anthropic_api_key", "openai_api_key", "oura_client_secret", "dexcom_client_secret", "fitbit_client_secret")
PLAIN_KEYS = (
    "llm_provider",
    "anthropic_model",
    "openai_model",
    "local_llm_url",
    "local_llm_model",
    "quality_llm_url",
    "quality_llm_model",
    "app_timezone",
    "sync_enabled",
    "oura_client_id",
    "dexcom_client_id",
    "fitbit_client_id",
)
READONLY_KEYS = ("app_public_url", "dexcom_redirect_uri", "dexcom_env")

PLAIN_DEFAULTS = {
    "llm_provider": "anthropic",
    "anthropic_model": "claude-sonnet-5",
    "openai_model": "gpt-4o-mini",
    "local_llm_url": "unix:///run/glucopilot/llm.sock",
    "local_llm_model": "qwen3-vl-8b",
    "quality_llm_url": "unix:///run/glucopilot/ollama.sock",
    "quality_llm_model": "gemma3:27b",
    "app_timezone": "America/New_York",
    "sync_enabled": "true",
}


def _source(name: str) -> str | None:
    if get_setting(f"cfg_{name}"):
        return "app"
    if os.getenv(name.upper()):
        return "env"
    return None


def _hint(value: str) -> str:
    if len(value) <= 6:
        return "•" * len(value)
    return f"{value[:3]}…{value[-3:]}"


@router.get("/api/settings")
def get_settings():
    from .ingest import ingest_token
    secrets = {}
    for name in SECRET_KEYS:
        value = config_value(name)
        secrets[name] = {
            "configured": bool(value),
            "source": _source(name),
            "hint": _hint(value) if value else None,
        }
    values = {name: config_value(name, PLAIN_DEFAULTS.get(name, "")) for name in PLAIN_KEYS}
    readonly = {name: os.getenv(name.upper(), "") for name in READONLY_KEYS}
    readonly["ingest_token"] = ingest_token()
    return {"secrets": secrets, "values": values, "readonly": readonly}


class SettingsUpdate(BaseModel):
    values: dict[str, Any] | None = None
    secrets: dict[str, str] | None = None


@router.put("/api/settings")
def update_settings(body: SettingsUpdate):
    for name, value in (body.values or {}).items():
        if name not in PLAIN_KEYS:
            raise HTTPException(status_code=400, detail=f"Unknown setting: {name}")
        set_config_value(name, str(value).strip())
    for name, value in (body.secrets or {}).items():
        if name not in SECRET_KEYS:
            raise HTTPException(status_code=400, detail=f"Unknown secret: {name}")
        set_config_value(name, value.strip())
    return get_settings()
