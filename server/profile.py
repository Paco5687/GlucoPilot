"""Body profile — height, weight, date of birth, sex.

The basis for body-relative dosing analytics: BMI, TDD/kg, and the insulin
resistance / absorption estimates. Stored canonically in metric (height_cm,
weight_kg) with a display-units preference; a WeightLog point is appended on
each weight change so weight can trend over time.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from . import db
from .auth import require_admin, require_login
from .config import OWNER_EMAIL

log = logging.getLogger("glucopilot.profile")

router = APIRouter()

FIELDS = ("height_cm", "weight_kg", "date_of_birth", "sex", "units")


def _get() -> dict[str, Any] | None:
    rows = db.query_entities("HealthProfile", {"owner_email": OWNER_EMAIL}, "-created_date", 1)
    return rows[0] if rows else None


def _age(dob: str | None) -> int | None:
    if not dob:
        return None
    try:
        d = datetime.fromisoformat(str(dob)).date()
    except ValueError:
        return None
    today = datetime.now(timezone.utc).date()
    return today.year - d.year - ((today.month, today.day) < (d.month, d.day))


def get_profile() -> dict[str, Any]:
    """Profile + derived age/BMI. Other modules call this for body-relative math."""
    row = _get() or {}
    height_cm = row.get("height_cm")
    weight_kg = row.get("weight_kg")
    bmi = None
    if height_cm and weight_kg:
        try:
            bmi = round(float(weight_kg) / ((float(height_cm) / 100) ** 2), 1)
        except (TypeError, ValueError, ZeroDivisionError):
            bmi = None
    return {
        "height_cm": height_cm,
        "weight_kg": weight_kg,
        "date_of_birth": row.get("date_of_birth") or "",
        "sex": row.get("sex") or "",
        "units": row.get("units") or "imperial",
        "age": _age(row.get("date_of_birth")),
        "bmi": bmi,
    }


@router.get("/api/profile", dependencies=[Depends(require_login)])
def get():
    return get_profile()


class ProfileBody(BaseModel):
    height_cm: float | None = None
    weight_kg: float | None = None
    date_of_birth: str | None = None
    sex: str | None = None
    units: str | None = None


@router.put("/api/profile", dependencies=[Depends(require_admin)])
def save(body: ProfileBody):
    existing = _get()
    prior_weight = (existing or {}).get("weight_kg")
    data = {
        "height_cm": body.height_cm,
        "weight_kg": body.weight_kg,
        "date_of_birth": (body.date_of_birth or "").strip(),
        "sex": (body.sex or "").strip().lower(),
        "units": (body.units or "imperial").strip().lower(),
    }
    if existing:
        db.update_entity("HealthProfile", existing["id"], data)
    else:
        db.create_entity("HealthProfile", {**data, "owner_email": OWNER_EMAIL})
    # Append a WeightLog point on a new/changed weight so it trends.
    if body.weight_kg and body.weight_kg != prior_weight:
        db.create_entity("WeightLog", {
            "weight_kg": float(body.weight_kg),
            "date": datetime.now(timezone.utc).date().isoformat(),
            "owner_email": OWNER_EMAIL,
        })
    return get_profile()
