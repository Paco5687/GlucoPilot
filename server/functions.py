"""Dispatch for base44.functions.invoke(name, payload) calls from the SPA."""

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from . import cycle_inference, dexcom, dexcom_share, fingerstick, fitbit, glooko, google_health, health_summary, insights, insulin, nightscout, oura, patterns, tandem
from .auth import require_admin

log = logging.getLogger("glucopilot.functions")

router = APIRouter(dependencies=[Depends(require_admin)])


async def _dispatch(name: str, body: dict[str, Any]) -> Any:
    if name == "nightscout":
        return await nightscout.handle(body)
    if name == "ouraAuth":
        return await oura.handle_auth(body)
    if name == "ouraSync":
        return await oura.handle_sync(body)
    if name == "dexcom":
        return await dexcom.handle(body)
    if name == "tandem":
        return await tandem.handle(body)
    if name == "glooko":
        return await glooko.handle(body)
    if name == "dexcomShare":
        return await dexcom_share.handle(body)
    if name == "fitbit":
        return await fitbit.handle(body)
    if name == "googleHealth":
        return await google_health.handle(body)
    if name == "analyzePatterns":
        return await patterns.analyze()
    if name == "healthSummary":
        return await health_summary.handle(body)
    if name == "fingerstick":
        return await fingerstick.handle(body)
    if name == "insulin":
        return await insulin.handle(body)
    if name == "analyzeInsights":
        return await insights.analyze()
    if name == "inferCycles":
        return await cycle_inference.infer()
    return {"error": f"Unknown function: {name}", "_status": 404}


@router.post("/api/functions/{name}")
async def invoke(name: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        result = await _dispatch(name, body if isinstance(body, dict) else {})
    except HTTPException:
        raise
    except httpx.ConnectError as err:
        result = {"error": f"Could not reach the remote service — check the URL. ({err})", "_status": 502}
    except httpx.TimeoutException:
        result = {"error": "The remote service timed out.", "_status": 504}
    except Exception as err:
        log.exception("function %s failed", name)
        result = {"error": f"{type(err).__name__}: {err}", "_status": 500}
    status = 200
    if isinstance(result, dict) and "_status" in result:
        status = result.pop("_status")
    return JSONResponse(result, status_code=status)
