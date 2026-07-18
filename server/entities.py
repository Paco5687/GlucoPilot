"""Generic entity REST API consumed by the frontend's Base44-SDK shim."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db
from .auth import require_admin, require_login

router = APIRouter(prefix="/api/entities", dependencies=[Depends(require_login)])
# Writes additionally require admin — provider sessions are read-only.
admin_only = [Depends(require_admin)]


def _check_type(etype: str) -> str:
    if etype not in db.ENTITY_TYPES:
        raise HTTPException(status_code=404, detail=f"Unknown entity type: {etype}")
    return etype


class QueryBody(BaseModel):
    filter: dict[str, Any] | None = None
    sort: str | None = None
    limit: int | None = None
    skip: int = 0


class BulkBody(BaseModel):
    records: list[dict[str, Any]]


@router.post("/{etype}/query")
def query(etype: str, body: QueryBody):
    _check_type(etype)
    try:
        return db.query_entities(etype, body.filter, body.sort, body.limit, body.skip)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))


@router.post("/{etype}", dependencies=admin_only)
def create(etype: str, body: dict[str, Any]):
    _check_type(etype)
    return db.create_entity(etype, body)


@router.post("/{etype}/bulk", dependencies=admin_only)
def bulk_create(etype: str, body: BulkBody):
    _check_type(etype)
    return db.bulk_create_entities(etype, body.records)


@router.put("/{etype}/{rid}", dependencies=admin_only)
def update(etype: str, rid: str, body: dict[str, Any]):
    _check_type(etype)
    record = db.update_entity(etype, rid, body)
    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")
    return record


@router.delete("/{etype}/{rid}", dependencies=admin_only)
def delete(etype: str, rid: str):
    _check_type(etype)
    if not db.delete_entity(etype, rid):
        raise HTTPException(status_code=404, detail="Record not found")
    return {"ok": True}
