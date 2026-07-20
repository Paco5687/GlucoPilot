"""In-app bug reporter → GitHub issue (+ optional Projects v2 board).

Collects the user's description plus safe context (page, recent navigation,
role, time, browser — never health data). The app files the report itself:
every report is stored locally so nothing is lost, and — when a GitHub token is
configured — an issue is created via the REST API (and added to a Projects v2
board if one is configured). The reporter never sees GitHub: they type, submit,
done. No browser hand-off, no sign-in.
"""

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import db
from .auth import require_login, session_role
from .config import OWNER_EMAIL
from .db import config_value

log = logging.getLogger("glucopilot.bugreport")

router = APIRouter(dependencies=[Depends(require_login)])

GITHUB_API = "https://api.github.com"
GRAPHQL = "https://api.github.com/graphql"
DEFAULT_REPO = "Paco5687/GlucoPilot"


def _repo() -> str:
    return config_value("github_repo", DEFAULT_REPO)


def _build_issue(description: str, ctx: dict, role: str) -> tuple[str, str]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    trail = ctx.get("trail") or []
    body = "\n".join(
        [
            description.strip(),
            "",
            "---",
            "",
            f"- **Page:** `{ctx.get('page', '?')}`",
            f"- **Recent navigation:** {' → '.join(trail) if trail else 'n/a'}",
            f"- **Role:** {role}",
            f"- **When:** {ctx.get('time') or now}",
            f"- **App:** {ctx.get('app_url', '?')}",
            f"- **Browser:** {ctx.get('user_agent', '?')}",
            "",
            "_Filed via the in-app bug reporter. No health data is included automatically._",
        ]
    )
    first_line = description.strip().splitlines()[0] if description.strip() else "Bug report"
    title = "Bug: " + (first_line[:70] + ("…" if len(first_line) > 70 else ""))
    return title, body


async def _add_to_project(client: httpx.AsyncClient, headers: dict, issue_node_id: str) -> bool:
    number = config_value("github_project_number")
    if not number:
        return False
    owner = _repo().split("/")[0]
    for kind in ("user", "organization"):
        try:
            resp = await client.post(
                GRAPHQL,
                headers=headers,
                json={"query": f'query{{{kind}(login:"{owner}"){{projectV2(number:{int(number)}){{id}}}}}}'},
            )
            proj = ((resp.json().get("data") or {}).get(kind) or {}).get("projectV2")
        except Exception:
            proj = None
        if proj and proj.get("id"):
            await client.post(
                GRAPHQL,
                headers=headers,
                json={
                    "query": "mutation($p:ID!,$c:ID!){addProjectV2ItemById(input:{projectId:$p,contentId:$c}){item{id}}}",
                    "variables": {"p": proj["id"], "c": issue_node_id},
                },
            )
            return True
    return False


class BugBody(BaseModel):
    description: str
    context: dict[str, Any] = {}


async def _create_issue(title: str, issue_body: str, repo: str, token: str) -> dict[str, Any] | None:
    """Create the GitHub issue (and add it to the board). Returns the issue dict,
    or None on any failure — the caller has already stored the report locally, so
    a GitHub hiccup never loses the report or blocks the user."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            payload = {"title": title, "body": issue_body, "labels": ["bug"]}
            resp = await client.post(f"{GITHUB_API}/repos/{repo}/issues", headers=headers, json=payload)
            if resp.status_code == 422:  # label may not exist — retry without it
                resp = await client.post(
                    f"{GITHUB_API}/repos/{repo}/issues", headers=headers, json={"title": title, "body": issue_body}
                )
            if resp.status_code >= 400:
                log.warning("bug report github failed: %s %s", resp.status_code, resp.text[:300])
                return None
            issue = resp.json()
            try:
                await _add_to_project(client, headers, issue["node_id"])
            except Exception:
                log.exception("add-to-project failed (issue still created)")
            return issue
    except Exception:
        log.exception("bug report github create failed")
        return None


@router.post("/api/bug-report")
async def bug_report(request: Request, body: BugBody):
    if not body.description.strip():
        return JSONResponse({"detail": "Please describe the bug."}, status_code=400)

    role = session_role(request)
    title, issue_body = _build_issue(body.description, body.context, role)
    ctx = body.context or {}

    # The app files it: create the GitHub issue if a token is configured...
    token = config_value("github_token")
    issue = await _create_issue(title, issue_body, _repo(), token) if token else None

    # ...and always keep a local copy so nothing is ever lost.
    try:
        db.create_entity("BugReport", {
            "title": title,
            "description": body.description.strip(),
            "page": ctx.get("page"),
            "role": role,
            "status": "new",
            "github_url": issue.get("html_url") if issue else None,
            "github_number": issue.get("number") if issue else None,
            "created_date": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "owner_email": OWNER_EMAIL,
        })
    except Exception:
        log.exception("failed to store bug report locally")

    return {"ok": True, "github": issue is not None}
