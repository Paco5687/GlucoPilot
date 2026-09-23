"""Food logging with as little friction as possible: scan a barcode, photograph
a nutrition label, or tap a saved preset. Every log becomes an ordinary
``carb`` Treatment (so the correction, walking, and pattern engines see it
as they already see pump-reported carbs) carrying the fuller nutrition
detail — protein, fat, fiber, calories, servings — for later analysis.

Barcodes are resolved through Open Food Facts. Only the barcode leaves the
machine; no health data is involved, and the lookup can be switched off in
Settings. Label photos go to the configured vision model exactly as lab
reports do — with a local model they never leave the machine.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from . import db
from .config import OWNER_EMAIL
from .llm import invoke_llm
from .repositories import get_repositories

log = logging.getLogger("glucopilot.food")

OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{barcode}.json"
OFF_FIELDS = "product_name,brands,serving_size,serving_quantity,nutriments"
OFF_USER_AGENT = "GlucoPilot/1.0 (self-hosted personal health app)"
MACROS = ("carbs", "fiber", "sugars", "protein", "fat", "calories")
PRESET_LIMIT = 200
RECENT_LIMIT = 30

NUTRITION_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": ["string", "null"]},
        "brand": {"type": ["string", "null"]},
        "serving_label": {"type": ["string", "null"]},
        "calories": {"type": ["number", "null"]},
        "carbs": {"type": ["number", "null"]},
        "fiber": {"type": ["number", "null"]},
        "sugars": {"type": ["number", "null"]},
        "protein": {"type": ["number", "null"]},
        "fat": {"type": ["number", "null"]},
        "confidence": {"type": "number"},
    },
    "required": ["serving_label", "calories", "carbs", "fiber", "sugars", "protein", "fat", "confidence"],
}

NUTRITION_PROMPT = """You are reading a photographed food package for the person's own food log.

From the Nutrition Facts panel, report the PER-SERVING values as numbers:
serving_label (e.g. "1 cup (240 mL)", "2 cookies (28 g)"), calories, carbs
(total carbohydrate, grams), fiber (grams), sugars (grams), protein (grams),
fat (total fat, grams). If the product name or brand is visible anywhere on the
package, report them too.

Rules:
- Use null for anything not clearly visible. Never guess a number.
- Report per serving, not per container, unless the panel only shows one.
- confidence is 0 to 1 for how legible the panel was.
"""


def _get(entity_type: str, entity_id: str) -> dict[str, Any] | None:
    repositories = get_repositories()
    accessor = getattr(repositories, "entity", None)
    if callable(accessor):
        return accessor(entity_type).get(str(entity_id))
    rows = db.query_entities(entity_type, {"owner_email": OWNER_EMAIL}, "-created_date", 5000)
    return next((r for r in rows if str(r.get("id")) == str(entity_id)), None)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_barcode(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits if 6 <= len(digits) <= 14 else ""


def normalize_off_product(product: dict[str, Any]) -> dict[str, Any] | None:
    """Turn an Open Food Facts product into per-serving macros. Pure.

    Per-serving values are used when OFF has them; otherwise they are derived
    from per-100g values and the stated serving quantity. With neither, the
    product falls back to a 100 g serving so the person can still scale it."""
    n = product.get("nutriments") or {}
    per100 = {
        "carbs": _num(n.get("carbohydrates_100g")),
        "fiber": _num(n.get("fiber_100g")),
        "sugars": _num(n.get("sugars_100g")),
        "protein": _num(n.get("proteins_100g")),
        "fat": _num(n.get("fat_100g")),
        "calories": _num(n.get("energy-kcal_100g")),
    }
    if per100["carbs"] is None:
        return None
    serving_qty = _num(product.get("serving_quantity"))
    serving_label = str(product.get("serving_size") or "").strip()
    keys = {"carbs": "carbohydrates", "fiber": "fiber", "sugars": "sugars", "protein": "proteins", "fat": "fat", "calories": "energy-kcal"}
    per_serving = {}
    for macro, off in keys.items():
        direct = _num(n.get(f"{off}_serving"))
        if direct is not None:
            per_serving[macro] = round(direct, 1)
        elif serving_qty and per100[macro] is not None:
            per_serving[macro] = round(per100[macro] * serving_qty / 100, 1)
        else:
            per_serving[macro] = None
    if per_serving["carbs"] is None:
        per_serving = {k: (round(v, 1) if v is not None else None) for k, v in per100.items()}
        serving_label = "100 g"
        serving_qty = 100.0
    return {
        "name": str(product.get("product_name") or "").strip() or None,
        "brand": str(product.get("brands") or "").strip() or None,
        "serving_label": serving_label or (f"{serving_qty:g} g" if serving_qty else "1 serving"),
        "serving_grams": serving_qty,
        **per_serving,
        "per_100g": per100,
    }


async def lookup_barcode(barcode: str) -> dict[str, Any]:
    if db.config_value("food_barcode_lookup", "true").lower() == "false":
        return {"error": "Barcode lookup is turned off in Settings.", "_status": 403}
    code = _clean_barcode(barcode)
    if not code:
        return {"error": "That doesn't look like a product barcode.", "_status": 400}
    cached = db.query_entities("FoodProduct", {"owner_email": OWNER_EMAIL, "barcode": code}, "-created_date", 1)
    if cached:
        return {"ok": True, "product": cached[0], "cached": True}
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": OFF_USER_AGENT}) as client:
            response = await client.get(OFF_URL.format(barcode=code), params={"fields": OFF_FIELDS})
    except httpx.HTTPError as err:
        log.warning("open food facts unreachable: %s", type(err).__name__)
        return {"error": "Couldn't reach Open Food Facts. Try the label photo instead.", "_status": 502}
    if response.status_code == 404:
        return {"error": "Not in Open Food Facts yet — try a label photo, then save it as a preset.", "_status": 404}
    if response.status_code >= 400:
        return {"error": f"Open Food Facts returned {response.status_code}.", "_status": 502}
    data = response.json() if response.text else {}
    normalized = normalize_off_product(data.get("product") or {})
    if not normalized:
        return {"error": "That product has no carbohydrate data — try a label photo.", "_status": 404}
    product = db.create_entity("FoodProduct", {
        **normalized, "barcode": code, "source": "openfoodfacts", "fetched_at": _now_iso(), "owner_email": OWNER_EMAIL,
    })
    return {"ok": True, "product": product, "cached": False}


async def scan_label(image: str) -> dict[str, Any]:
    """image is "media/type|base64" as the records pipeline encodes pages."""
    if not image or "|" not in image:
        return {"error": "A label photo is required.", "_status": 400}
    try:
        result = await invoke_llm(NUTRITION_PROMPT, response_json_schema=NUTRITION_SCHEMA, max_tokens=600,
                                  images=[image], site="nutrition_label")
    except Exception as err:  # the model layer already logs details
        return {"error": f"Couldn't read the label: {err}", "_status": 502}
    if not isinstance(result, dict) or result.get("carbs") is None:
        return {"error": "Couldn't find a carbohydrate value on that label. Try a closer, straighter photo.", "_status": 422}
    return {"ok": True, "label": {k: result.get(k) for k in ("name", "brand", "serving_label", "confidence", *MACROS)}}


def _preset_payload(body: dict[str, Any]) -> dict[str, Any] | None:
    name = str(body.get("name") or "").strip()[:120]
    carbs = _num(body.get("carbs"))
    if not name or carbs is None or carbs < 0:
        return None
    return {
        "name": name,
        "brand": (str(body.get("brand") or "").strip()[:80] or None),
        "serving_label": str(body.get("serving_label") or "1 serving").strip()[:80],
        "default_servings": max(0.25, min(_num(body.get("default_servings")) or 1.0, 20.0)),
        "barcode": _clean_barcode(body.get("barcode")) or None,
        "source": body.get("source") if body.get("source") in ("barcode", "label", "manual") else "manual",
        **{m: (round(_num(body.get(m)), 1) if _num(body.get(m)) is not None else None) for m in MACROS},
        "carbs": round(carbs, 1),
    }


def list_presets() -> list[dict[str, Any]]:
    rows = db.query_entities("FoodPreset", {"owner_email": OWNER_EMAIL}, "-created_date", PRESET_LIMIT)
    return sorted(rows, key=lambda r: (-(r.get("use_count") or 0), str(r.get("last_used") or ""), r.get("name") or ""))


def log_food(body: dict[str, Any]) -> dict[str, Any]:
    """Create the carb Treatment. Amount is total carbohydrate for the servings
    logged; the fuller nutrition detail rides along for later analysis."""
    preset = None
    if body.get("preset_id"):
        preset = _get("FoodPreset", body["preset_id"])
        if not preset or preset.get("owner_email") != OWNER_EMAIL:
            return {"error": "That preset no longer exists.", "_status": 404}
    base = {**(preset or {}), **{k: v for k, v in body.items() if v is not None and k in ("name", "brand", "serving_label", *MACROS)}}
    carbs = _num(base.get("carbs"))
    if carbs is None:
        return {"error": "Carbohydrates per serving are required.", "_status": 400}
    servings = _num(body.get("servings"))
    servings = max(0.25, min(servings if servings else (base.get("default_servings") or 1.0), 20.0))
    ts = body.get("timestamp") or _now_iso()
    try:
        datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return {"error": "A valid time is required.", "_status": 400}
    name = str(base.get("name") or "Food").strip()[:120]
    totals = {m: (round(_num(base.get(m)) * servings, 1) if _num(base.get(m)) is not None else None) for m in MACROS}
    treatment = get_repositories().treatments.create({
        "type": "carb",
        "event_type": "Carbs",
        "timestamp": ts,
        "amount": round(carbs * servings, 1),
        "notes": f"{name} × {servings:g} {base.get('serving_label') or 'serving'}",
        "source": "manual",
        "food_name": name,
        "food_brand": base.get("brand"),
        "servings": servings,
        "serving_label": base.get("serving_label"),
        "protein_g": totals["protein"],
        "fat_g": totals["fat"],
        "fiber_g": totals["fiber"],
        "sugars_g": totals["sugars"],
        "calories": totals["calories"],
        "preset_id": preset["id"] if preset else None,
        "owner_email": OWNER_EMAIL,
    })
    if preset:
        db.update_entity("FoodPreset", preset["id"], {"use_count": (preset.get("use_count") or 0) + 1, "last_used": ts})
    return {"ok": True, "treatment": treatment}


def recent_logs() -> list[dict[str, Any]]:
    rows = get_repositories().treatments.query(
        {"owner_email": OWNER_EMAIL, "type": "carb", "source": "manual"}, "-timestamp", RECENT_LIMIT
    )
    return [r for r in rows if r.get("food_name")]


async def handle(body: dict[str, Any]) -> dict[str, Any]:
    action = body.get("action", "presets")
    if action == "lookup_barcode":
        return await lookup_barcode(body.get("barcode"))
    if action == "scan_label":
        return await scan_label(body.get("image"))
    if action == "presets":
        return {"presets": list_presets(), "recent": recent_logs()}
    if action == "save_preset":
        payload = _preset_payload(body)
        if payload is None:
            return {"error": "A name and carbohydrates per serving are required.", "_status": 400}
        if body.get("id"):
            existing = _get("FoodPreset", body["id"])
            if not existing or existing.get("owner_email") != OWNER_EMAIL:
                return {"error": "That preset no longer exists.", "_status": 404}
            return {"ok": True, "preset": db.update_entity("FoodPreset", existing["id"], payload)}
        return {"ok": True, "preset": db.create_entity("FoodPreset", {**payload, "use_count": 0, "owner_email": OWNER_EMAIL})}
    if action == "delete_preset":
        existing = _get("FoodPreset", body.get("id") or "")
        if existing and existing.get("owner_email") == OWNER_EMAIL:
            db.delete_entity("FoodPreset", existing["id"])
        return {"ok": True}
    if action == "log":
        return log_food(body)
    if action == "delete_log":
        existing = _get("Treatment", body.get("id") or "")
        if existing and existing.get("owner_email") == OWNER_EMAIL and existing.get("source") == "manual" and existing.get("type") == "carb":
            db.delete_entity("Treatment", existing["id"])
            return {"ok": True}
        return {"error": "Only manually logged food can be deleted here.", "_status": 400}
    return {"error": f"Unknown food action: {action}", "_status": 400}
