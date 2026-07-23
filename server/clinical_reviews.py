"""Attributable provider annotations with append-only owner review history."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from . import db
from .auth import current_user, require_admin, require_login, session_role
from .data_contracts import DEPLOYMENT_OWNER_ID


router = APIRouter(prefix="/api/clinical-reviews", dependencies=[Depends(require_login)])
PROVIDER_ACTIONS = {
    "annotate": "annotated",
    "mark_reviewed": "reviewed",
    "hypothesis_confirm": "hypothesis_confirmed",
    "hypothesis_reject": "hypothesis_rejected",
    "correction_confirm": "correction_confirmed",
    "question": "question_open",
}
TARGET_KINDS = {"evidence_item", "entity", "hypothesis", "correction", "brief"}
OWNER_ACTIONS = {"accept": "accepted", "dispute": "disputed"}


class ReviewActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_kind: str
    target_type: str
    target_id: str
    target_label: str = ""
    action: str
    text: str = ""
    evidence_bundle_id: str | None = None


class OwnerDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str
    reason: str = Field(min_length=1, max_length=4000)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _text(value: Any, limit: int, *, required: bool = False, label: str = "value") -> str:
    normalized = " ".join(str(value or "").split())
    if required and not normalized:
        raise HTTPException(status_code=400, detail=f"{label} is required")
    if len(normalized) > limit:
        raise HTTPException(status_code=400, detail=f"{label} exceeds {limit} characters")
    return normalized


def _state(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    return {
        "current_action": row["current_action"],
        "provider_status": row["provider_status"],
        "owner_status": row["owner_status"],
        "current_text": row["current_text"],
        "evidence_bundle_id": row["evidence_bundle_id"],
    }


def _actor(request: Request) -> dict[str, str]:
    role = session_role(request)
    user = current_user(role, request.session.get("provider_name", ""))
    label = _text(user.get("full_name"), 240, required=True, label="actor")
    stable = hashlib.sha256(f"{role}:{label}".encode()).hexdigest()[:24]
    return {"role": role, "id": f"{role}:{stable}", "label": label}


def _event(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["prior_state"] = json.loads(item.pop("prior_state_json"))
    item["new_state"] = json.loads(item.pop("new_state_json"))
    return item


def _thread(connection, row: Any, *, include_events: bool = True) -> dict[str, Any]:
    item = dict(row)
    if include_events:
        item["events"] = [
            _event(event)
            for event in connection.execute(
                "SELECT * FROM clinical_review_events WHERE thread_id=? "
                "ORDER BY created_at,id",
                (item["id"],),
            ).fetchall()
        ]
    return item


def _validate_action(body: ReviewActionBody) -> dict[str, Any]:
    action = _text(body.action, 40, required=True, label="action").lower()
    kind = _text(body.target_kind, 40, required=True, label="target kind").lower()
    if action not in PROVIDER_ACTIONS:
        raise HTTPException(status_code=400, detail="unsupported provider review action")
    if kind not in TARGET_KINDS:
        raise HTTPException(status_code=400, detail="unsupported review target kind")
    if action.startswith("hypothesis_") and kind != "hypothesis":
        raise HTTPException(status_code=400, detail="hypothesis decisions require a hypothesis target")
    if action == "correction_confirm" and kind != "correction":
        raise HTTPException(status_code=400, detail="correction confirmation requires a correction target")
    text = _text(body.text, 4000)
    if action in {"annotate", "question", "hypothesis_confirm", "hypothesis_reject"} and not text:
        raise HTTPException(status_code=400, detail="this review action requires explanatory text")
    bundle = _text(body.evidence_bundle_id, 500) or None
    if bundle and not bundle.startswith("urn:glucopilot:evidence-bundle:"):
        raise HTTPException(status_code=400, detail="invalid Evidence Bundle identity")
    return {
        "action": action,
        "target_kind": kind,
        "target_type": _text(body.target_type, 120, required=True, label="target type"),
        "target_id": _text(body.target_id, 500, required=True, label="target identity"),
        "target_label": _text(body.target_label, 500),
        "text": text,
        "evidence_bundle_id": bundle,
    }


@router.get("")
def list_reviews(
    target_kind: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    limit: int = 100,
):
    limit = max(1, min(int(limit), 200))
    where = ["owner_id=?"]
    values: list[Any] = [DEPLOYMENT_OWNER_ID]
    for field, value in (
        ("target_kind", target_kind),
        ("target_type", target_type),
        ("target_id", target_id),
    ):
        if value:
            where.append(f"{field}=?")
            values.append(_text(value, 500))
    values.append(limit)
    with db.connect() as connection:
        rows = connection.execute(
            f"SELECT * FROM clinical_review_threads WHERE {' AND '.join(where)} "
            "ORDER BY updated_at DESC,id LIMIT ?",
            values,
        ).fetchall()
        return {"reviews": [_thread(connection, row) for row in rows]}


@router.get("/{thread_id}")
def get_review(thread_id: str):
    with db.connect() as connection:
        row = connection.execute(
            "SELECT * FROM clinical_review_threads WHERE id=? AND owner_id=?",
            (_text(thread_id, 80), DEPLOYMENT_OWNER_ID),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="clinical review not found")
        return _thread(connection, row)


@router.post("/actions")
def provider_action(request: Request, body: ReviewActionBody):
    if session_role(request) != "provider":
        raise HTTPException(status_code=403, detail="A provider session is required.")
    value = _validate_action(body)
    if value["target_kind"] == "hypothesis":
        from .hypotheses import SqliteHypothesisRepository

        if not SqliteHypothesisRepository().get(value["target_id"]):
            raise HTTPException(status_code=404, detail="hypothesis target not found")
    actor = _actor(request)
    now = _now()
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """
            SELECT * FROM clinical_review_threads
            WHERE owner_id=? AND target_kind=? AND target_type=? AND target_id=?
            """,
            (
                DEPLOYMENT_OWNER_ID,
                value["target_kind"],
                value["target_type"],
                value["target_id"],
            ),
        ).fetchone()
        before = _state(dict(existing) if existing else None)
        thread_id = existing["id"] if existing else str(uuid.uuid4())
        target_label = value["target_label"] or (existing["target_label"] if existing else "")
        bundle_id = value["evidence_bundle_id"] or (
            existing["evidence_bundle_id"] if existing else None
        )
        after = {
            "current_action": value["action"],
            "provider_status": PROVIDER_ACTIONS[value["action"]],
            "owner_status": "pending",
            "current_text": value["text"],
            "evidence_bundle_id": bundle_id,
        }
        if existing:
            connection.execute(
                """
                UPDATE clinical_review_threads
                SET target_label=?,current_action=?,provider_status=?,owner_status='pending',
                    current_text=?,evidence_bundle_id=?,updated_at=?
                WHERE id=?
                """,
                (
                    target_label,
                    value["action"],
                    after["provider_status"],
                    value["text"],
                    bundle_id,
                    now,
                    thread_id,
                ),
            )
        else:
            connection.execute(
                """
                INSERT INTO clinical_review_threads (
                    id,owner_id,target_kind,target_type,target_id,target_label,
                    current_action,provider_status,owner_status,current_text,
                    evidence_bundle_id,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    thread_id,
                    DEPLOYMENT_OWNER_ID,
                    value["target_kind"],
                    value["target_type"],
                    value["target_id"],
                    target_label,
                    value["action"],
                    after["provider_status"],
                    "pending",
                    value["text"],
                    bundle_id,
                    now,
                    now,
                ),
            )
        connection.execute(
            """
            INSERT INTO clinical_review_events (
                id,thread_id,action,actor_role,actor_id,actor_label,reason,
                prior_state_json,new_state_json,evidence_bundle_id,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()),
                thread_id,
                value["action"],
                actor["role"],
                actor["id"],
                actor["label"],
                value["text"] or value["action"].replace("_", " "),
                json.dumps(before, sort_keys=True),
                json.dumps(after, sort_keys=True),
                bundle_id,
                now,
            ),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM clinical_review_threads WHERE id=?", (thread_id,)
        ).fetchone()
        return _thread(connection, row)


@router.post("/{thread_id}/owner-decision", dependencies=[Depends(require_admin)])
def owner_decision(request: Request, thread_id: str, body: OwnerDecisionBody):
    decision = _text(body.decision, 20, required=True, label="decision").lower()
    if decision not in OWNER_ACTIONS:
        raise HTTPException(status_code=400, detail="decision must be accept or dispute")
    reason = _text(body.reason, 4000, required=True, label="reason")
    actor = _actor(request)
    now = _now()
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM clinical_review_threads WHERE id=? AND owner_id=?",
            (_text(thread_id, 80), DEPLOYMENT_OWNER_ID),
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="clinical review not found")
        before = _state(dict(existing))
        after = {**before, "owner_status": OWNER_ACTIONS[decision]}
        connection.execute(
            "UPDATE clinical_review_threads SET owner_status=?,updated_at=? WHERE id=?",
            (OWNER_ACTIONS[decision], now, existing["id"]),
        )
        connection.execute(
            """
            INSERT INTO clinical_review_events (
                id,thread_id,action,actor_role,actor_id,actor_label,reason,
                prior_state_json,new_state_json,evidence_bundle_id,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()),
                existing["id"],
                f"owner_{decision}",
                actor["role"],
                actor["id"],
                actor["label"],
                reason,
                json.dumps(before, sort_keys=True),
                json.dumps(after, sort_keys=True),
                existing["evidence_bundle_id"],
                now,
            ),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM clinical_review_threads WHERE id=?", (existing["id"],)
        ).fetchone()
        return _thread(connection, row)


def companion_context(limit: int = 50) -> dict[str, list[dict[str, Any]]]:
    with db.connect() as connection:
        rows = connection.execute(
            """
            SELECT id,target_kind,target_type,target_id,target_label,current_action,
                   provider_status,owner_status,current_text,evidence_bundle_id,updated_at
            FROM clinical_review_threads
            WHERE owner_id=?
            ORDER BY updated_at DESC,id LIMIT ?
            """,
            (DEPLOYMENT_OWNER_ID, max(1, min(limit, 100))),
        ).fetchall()
    reviews = [
        {
            **dict(row),
            "current_text": str(row["current_text"])[:700],
        }
        for row in rows
    ]
    confirmed = [
        {
            **item,
            "semantic_class": "clinician_confirmation",
            "definitive_allowed": True,
        }
        for item in reviews
        if item["current_action"] == "hypothesis_confirm"
        and item["owner_status"] != "disputed"
    ]
    annotations = [
        {
            **item,
            "semantic_class": "provider_annotation",
            "definitive_allowed": False,
        }
        for item in reviews
        if item["current_action"] != "hypothesis_confirm"
    ]
    return {
        "clinician_confirmed_facts": confirmed,
        "provider_annotations": annotations,
        "owner_disputed_reviews": [
            item for item in reviews if item["owner_status"] == "disputed"
        ],
    }
