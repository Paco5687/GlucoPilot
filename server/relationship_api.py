"""Authenticated, budgeted read APIs over the governed relationship graph."""

from __future__ import annotations

import hashlib
import sqlite3
from collections import deque
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from . import db
from .auth import require_login
from .config import OWNER_EMAIL
from .relationships import SqliteRelationshipRepository, relationship_reads_enabled
from .repositories import RelationshipEdge


MAX_NEIGHBORS = 250
MAX_DEPTH = 4
MAX_EXPANSIONS = 1000
MAX_PATHS = 20
ORDERING = ("predicate", "from.type", "from.id", "to.type", "to.id", "relationship_id")

router = APIRouter(
    prefix="/api/relationships",
    dependencies=[Depends(require_login)],
)


def _opaque(value: str | None) -> str | None:
    if not value:
        return None
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _node(entity_type: str, entity_id: str) -> dict[str, str]:
    return {"type": entity_type, "id": entity_id}


def _edge_key(edge: RelationshipEdge) -> tuple[str, ...]:
    return (
        edge.predicate,
        edge.subject_type,
        edge.subject_id,
        edge.object_type,
        edge.object_id,
        edge.id or "",
    )


def _public_edge(edge: RelationshipEdge) -> dict[str, Any]:
    """Expose governed metadata without returning raw locators or input hashes."""
    return {
        "relationship_id": edge.id,
        "from": _node(edge.subject_type, edge.subject_id),
        "predicate": edge.predicate,
        "to": _node(edge.object_type, edge.object_id),
        "assertion": {
            "kind": edge.assertion_kind,
            "status": edge.assertion_status,
            "evidence_level": edge.evidence_level,
            "evidence_count": len(edge.evidence_ids),
            "evidence_refs": [_opaque(item) for item in edge.evidence_ids],
        },
        "confidence": {
            "label": edge.confidence_label,
            "score": edge.confidence_score,
            "method_ref": _opaque(edge.confidence_method),
            "calibration_ref": _opaque(edge.confidence_calibration_version),
        },
        "source": {
            "class": edge.source_class,
            "ref": _opaque(edge.source_id),
        },
        "version": {
            "generator_id": edge.generator_id,
            "generator_version": edge.generator_version,
            "input_data_version_ref": _opaque(edge.input_data_version),
        },
        "valid_time": {
            "kind": edge.valid_time_kind,
            "from": edge.valid_from,
            "to": edge.valid_to,
        },
        "generated_at": edge.generated_at,
    }


def _require_enabled() -> None:
    if not relationship_reads_enabled():
        raise HTTPException(status_code=503, detail="Relationship graph reads are disabled")


def _require_owned_node(
    connection: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
) -> None:
    found = connection.execute(
        """
        SELECT 1 FROM entities
        WHERE type=? AND id=? AND json_extract(data, '$.owner_email')=?
        """,
        (entity_type, entity_id, OWNER_EMAIL),
    ).fetchone()
    if not found:
        raise HTTPException(status_code=404, detail="Graph node not found")


def _adjacent(
    repository: SqliteRelationshipRepository,
    entity_type: str,
    entity_id: str,
    direction: Literal["outgoing", "incoming", "both"],
    limit: int,
) -> tuple[list[tuple[RelationshipEdge, tuple[str, str]]], bool]:
    requested = min(limit + 1, MAX_EXPANSIONS + 1)
    candidates: list[tuple[RelationshipEdge, tuple[str, str]]] = []
    if direction in {"outgoing", "both"}:
        candidates.extend(
            (edge, (edge.object_type, edge.object_id))
            for edge in repository.for_entity(
                OWNER_EMAIL,
                entity_type,
                entity_id,
                limit=requested,
            )
        )
    if direction in {"incoming", "both"}:
        candidates.extend(
            (edge, (edge.subject_type, edge.subject_id))
            for edge in repository.reverse_for_entity(
                OWNER_EMAIL,
                entity_type,
                entity_id,
                limit=requested,
            )
        )
    unique: dict[str, tuple[RelationshipEdge, tuple[str, str]]] = {}
    for edge, neighbor in candidates:
        unique[edge.id or repr(_edge_key(edge))] = (edge, neighbor)
    ordered = sorted(unique.values(), key=lambda item: _edge_key(item[0]))
    return ordered[:limit], len(ordered) > limit


def _neighbors(
    entity_type: str,
    entity_id: str,
    direction: Literal["outgoing", "incoming"],
    limit: int,
) -> dict[str, Any]:
    _require_enabled()
    with db.connect() as connection:
        _require_owned_node(connection, entity_type, entity_id)
        repository = SqliteRelationshipRepository(connection)
        adjacent, truncated = _adjacent(repository, entity_type, entity_id, direction, limit)
    return {
        "node": _node(entity_type, entity_id),
        "direction": direction,
        "relationships": [_public_edge(edge) for edge, _ in adjacent],
        "budget": {"item_limit": limit, "returned": len(adjacent), "truncated": truncated},
        "ordering": list(ORDERING),
    }


@router.get("/{entity_type}/{entity_id}/neighbors")
def outgoing_neighbors(
    entity_type: str,
    entity_id: str,
    limit: int = Query(default=50, ge=1, le=MAX_NEIGHBORS),
):
    return _neighbors(entity_type, entity_id, "outgoing", limit)


@router.get("/{entity_type}/{entity_id}/reverse-neighbors")
def incoming_neighbors(
    entity_type: str,
    entity_id: str,
    limit: int = Query(default=50, ge=1, le=MAX_NEIGHBORS),
):
    return _neighbors(entity_type, entity_id, "incoming", limit)


@router.get("/{entity_type}/{entity_id}/traverse")
def traverse(
    entity_type: str,
    entity_id: str,
    depth: int = Query(default=2, ge=1, le=MAX_DEPTH),
    limit: int = Query(default=100, ge=1, le=MAX_NEIGHBORS),
    direction: Literal["outgoing", "incoming", "both"] = Query(default="both"),
):
    _require_enabled()
    root = (entity_type, entity_id)
    visited = {root}
    discovered = [{"type": entity_type, "id": entity_id, "depth": 0}]
    relationships: dict[str, RelationshipEdge] = {}
    frontier = [root]
    expansions = 0
    truncated = False
    with db.connect() as connection:
        _require_owned_node(connection, *root)
        repository = SqliteRelationshipRepository(connection)
        for current_depth in range(1, depth + 1):
            next_frontier: list[tuple[str, str]] = []
            for current in sorted(frontier):
                remaining = MAX_EXPANSIONS - expansions
                if remaining <= 0:
                    truncated = True
                    break
                adjacent, has_more = _adjacent(repository, *current, direction, remaining)
                truncated = truncated or has_more
                for edge, neighbor in adjacent:
                    expansions += 1
                    key = edge.id or repr(_edge_key(edge))
                    if key not in relationships:
                        if len(relationships) >= limit:
                            truncated = True
                            break
                        relationships[key] = edge
                    if neighbor not in visited:
                        visited.add(neighbor)
                        next_frontier.append(neighbor)
                        discovered.append(
                            {"type": neighbor[0], "id": neighbor[1], "depth": current_depth}
                        )
                if truncated and (len(relationships) >= limit or expansions >= MAX_EXPANSIONS):
                    break
            frontier = sorted(set(next_frontier))
            if not frontier or len(relationships) >= limit or expansions >= MAX_EXPANSIONS:
                break
    ordered_edges = sorted(relationships.values(), key=_edge_key)
    return {
        "root": _node(*root),
        "direction": direction,
        "nodes": discovered,
        "relationships": [_public_edge(edge) for edge in ordered_edges],
        "budget": {
            "depth_limit": depth,
            "item_limit": limit,
            "expansion_limit": MAX_EXPANSIONS,
            "expanded": expansions,
            "returned_nodes": len(discovered),
            "returned_relationships": len(ordered_edges),
            "truncated": truncated,
        },
        "ordering": ["breadth_first", *ORDERING],
    }


@router.get("/{entity_type}/{entity_id}/evidence-paths")
def evidence_paths(
    entity_type: str,
    entity_id: str,
    target_type: str = Query(min_length=1, max_length=500),
    target_id: str = Query(min_length=1, max_length=500),
    depth: int = Query(default=3, ge=1, le=MAX_DEPTH),
    max_paths: int = Query(default=5, ge=1, le=MAX_PATHS),
):
    _require_enabled()
    root = (entity_type, entity_id)
    target = (target_type, target_id)
    queue: deque[tuple[tuple[str, str], tuple[tuple[str, str], ...], tuple[RelationshipEdge, ...]]] = deque()
    queue.append((root, (root,), ()))
    found: list[tuple[tuple[tuple[str, str], ...], tuple[RelationshipEdge, ...]]] = []
    expansions = 0
    truncated = False
    with db.connect() as connection:
        _require_owned_node(connection, *root)
        _require_owned_node(connection, *target)
        repository = SqliteRelationshipRepository(connection)
        while queue and len(found) < max_paths and expansions < MAX_EXPANSIONS:
            current, nodes, edges = queue.popleft()
            if len(edges) >= depth:
                continue
            remaining = MAX_EXPANSIONS - expansions
            adjacent, has_more = _adjacent(repository, *current, "both", remaining)
            truncated = truncated or has_more
            for edge, neighbor in adjacent:
                expansions += 1
                if neighbor in nodes:
                    continue
                next_nodes = (*nodes, neighbor)
                next_edges = (*edges, edge)
                if neighbor == target:
                    found.append((next_nodes, next_edges))
                    if len(found) >= max_paths:
                        truncated = bool(queue) or len(adjacent) > 1
                        break
                else:
                    queue.append((neighbor, next_nodes, next_edges))
                if expansions >= MAX_EXPANSIONS:
                    truncated = bool(queue)
                    break
    return {
        "start": _node(*root),
        "target": _node(*target),
        "paths": [
            {
                "nodes": [_node(*node) for node in nodes],
                "relationships": [_public_edge(edge) for edge in edges],
            }
            for nodes, edges in found
        ],
        "budget": {
            "depth_limit": depth,
            "path_limit": max_paths,
            "expansion_limit": MAX_EXPANSIONS,
            "expanded": expansions,
            "returned_paths": len(found),
            "truncated": truncated,
        },
        "ordering": ["shortest_path_first", *ORDERING],
    }
