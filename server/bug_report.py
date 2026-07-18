"""In-app bug reporter → GitHub issue (+ optional Projects v2 board).

Collects the user's description plus safe context (page, recent navigation,
role, time, browser — never health data) and opens a GitHub issue via the REST
API using a configured token. If a project number is configured, the issue is
also added to that Projects v2 board (best-effort via GraphQL).

Without a token, returns a pre-filled "new issue" URL so the reporter can file
it under their own GitHub account (good default for public/OSS deployments).
"""

import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .auth import require_login, session_role
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


@router.post("/api/bug-report")
async def bug_report(request: Request, body: BugBody):
    if not body.description.strip():
        return JSONResponse({"detail": "Please describe the bug."}, status_code=400)

    role = session_role(request)
    title, issue_body = _build_issue(body.description, body.context, role)
    repo = _repo()
    token = config_value("github_token")

    # No server token: hand back a pre-filled new-issue URL.
    if not token:
        url = f"https://github.com/{repo}/issues/new?title={quote(title)}&body={quote(issue_body)}&labels=bug"
        return {"ok": False, "fallback_url": url}

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        payload = {"title": title, "body": issue_body, "labels": ["bug"]}
        resp = await client.post(f"{GITHUB_API}/repos/{repo}/issues", headers=headers, json=payload)
        if resp.status_code == 422:  # label may not exist — retry without it
            resp = await client.post(
                f"{GITHUB_API}/repos/{repo}/issues", headers=headers, json={"title": title, "body": issue_body}
            )
        if resp.status_code >= 400:
            log.warning("bug report failed: %s %s", resp.status_code, resp.text[:300])
            return JSONResponse({"detail": f"GitHub error ({resp.status_code}). Check the token/repo in Settings."}, status_code=502)
        issue = resp.json()
        on_board = False
        try:
            on_board = await _add_to_project(client, headers, issue["node_id"])
        except Exception:
            log.exception("add-to-project failed (issue still created)")
    return {"ok": True, "url": issue["html_url"], "number": issue["number"], "on_board": on_board}
