"""GlucoPilot — self-hosted diabetes analytics.

FastAPI backend serving the built React SPA plus a Base44-compatible API:
session auth, generic entity store, function dispatch (Nightscout / Oura /
Dexcom / pattern analysis), and a direct Anthropic LLM proxy.
"""

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import activity_position, auth, bug_report, clinical_reviews, clinician_briefs, companion, conditions, contradictions, dexcom, entities, episodes, evidence_bundle, functions, history, hypotheses, ingest, insurance, llm, management_burden, meds, profile, records, relationship_api, report, scheduler, settings_api, symptoms
from .config import DEMO_MODE, FRONTEND_DIST, env
from .db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    from . import seed_demo
    log = logging.getLogger("glucopilot")
    if DEMO_MODE:
        log.info("demo seed: %s", seed_demo.seed())
    elif seed_demo.clear_if_demo_leftover():
        log.info("cleared leftover demo data — starting fresh for real use")
    task = asyncio.create_task(scheduler.run())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=env("APP_SECRET_KEY") or secrets.token_urlsafe(32),
    same_site="lax",
    https_only=env("APP_PUBLIC_URL", "").startswith("https"),
)

app.include_router(auth.router)
app.include_router(entities.router)
app.include_router(functions.router)
app.include_router(llm.router)
app.include_router(dexcom.router)
app.include_router(settings_api.router)
app.include_router(records.router)
app.include_router(ingest.router)
app.include_router(report.router)
app.include_router(insurance.router)
app.include_router(profile.router)
app.include_router(conditions.router)
app.include_router(meds.router)
app.include_router(bug_report.router)
app.include_router(companion.router)
app.include_router(contradictions.router)
app.include_router(symptoms.router)
app.include_router(history.router)
app.include_router(hypotheses.router)
app.include_router(episodes.router)
app.include_router(activity_position.router)
app.include_router(management_burden.router)
app.include_router(clinician_briefs.router)
app.include_router(clinical_reviews.router)
app.include_router(relationship_api.router)
app.include_router(evidence_bundle.router)

if (FRONTEND_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")


@app.exception_handler(404)
async def spa_fallback(request: Request, exc):
    """Serve the SPA for client-side routes; JSON 404 for API paths."""
    path = request.url.path
    if path.startswith(("/api/", "/dexcom/", "/assets/")):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    index = FRONTEND_DIST / "index.html"
    if index.is_file():
        return FileResponse(index)
    return JSONResponse({"detail": "Frontend build not found. Run: cd frontend && npm run build"}, status_code=503)


@app.get("/")
def root(request: Request):
    if auth.setup_required():
        return RedirectResponse("/login", status_code=303)
    index = FRONTEND_DIST / "index.html"
    if index.is_file():
        return FileResponse(index)
    return JSONResponse({"detail": "Frontend build not found. Run: cd frontend && npm run build"}, status_code=503)


@app.get("/healthz")
def healthz():
    return {"ok": True}
