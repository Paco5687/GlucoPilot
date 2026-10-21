"""Food logging with as little friction as possible: say what you ate, scan a
barcode, photograph a nutrition label, or tap a saved preset. Every log becomes an ordinary
``carb`` Treatment (so the correction, walking, and pattern engines see it
as they already see pump-reported carbs) carrying the fuller nutrition
detail — protein, fat, fiber, calories, servings — for later analysis.

Barcodes are resolved through Open Food Facts. Only the barcode leaves the
machine; no health data is involved, and the lookup can be switched off in
Settings. Label photos go to the configured vision model exactly as lab
reports do — with a local model they never leave the machine. Voice logs are
transcribed on this machine (``speech.py``); the words then go to the
configured text model for a per-food estimate, preferring the exact numbers of
any saved preset the person names. What glucose did after each logged meal is
measured read-only by ``meal_response.py``.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from . import db, meal_response, speech
from .activity_position import SqliteActivityPositionRepository, resolve_intervals
from .config import APP_TIMEZONE, OWNER_EMAIL
from .data_contracts import DEPLOYMENT_OWNER_ID
from .llm import invoke_llm
from .repositories import get_repositories

log = logging.getLogger("glucopilot.food")

OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{barcode}.json"
OFF_FIELDS = "product_name,brands,serving_size,serving_quantity,nutriments"
OFF_USER_AGENT = "GlucoPilot/1.0 (self-hosted personal health app)"
MACROS = ("carbs", "fiber", "sugars", "protein", "fat", "calories")
PRESET_LIMIT = 200
RECENT_LIMIT = 30
MEAL_ITEM_LIMIT = 12
PRESET_HINT_LIMIT = 8
TRANSCRIPT_LIMIT = 500

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


MEAL_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "portion": {"type": "string"},
        "preset_id": {"type": ["string", "null"]},
        "servings": {"type": ["number", "null"]},
        "carbs": {"type": "number"},
        "fiber": {"type": ["number", "null"]},
        "sugars": {"type": ["number", "null"]},
        "protein": {"type": ["number", "null"]},
        "fat": {"type": ["number", "null"]},
        "calories": {"type": ["number", "null"]},
    },
    "required": ["name", "portion", "preset_id", "servings", "carbs", "fiber", "sugars", "protein", "fat", "calories"],
}

MEAL_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "items": {"type": "array", "items": MEAL_ITEM_SCHEMA},
        "eaten_at": {"type": ["string", "null"]},
        "minutes_ago": {"type": ["integer", "null"]},
        "confidence": {"type": "number"},
    },
    "required": ["title", "items", "eaten_at", "minutes_ago", "confidence"],
}

MEAL_PROMPT = """You are estimating nutrition for the person's own food log from what they said they ate. They count carbohydrates to dose insulin, so the carb numbers matter most.

What they said: "{said}"
Their local time now: {now}
{known}
List each food they ate. For each one give:
- name: a short food name, e.g. "Gluten-free bread"
- portion: the amount eaten, e.g. "2 slices", "1 bag (28 g)", "1 cup"
- carbs, fiber, sugars, protein, fat in grams and calories, all for that portion

Rules:
- When no amount is given, assume one typical serving. Use typical US nutrition values.
- Split a mixed dish into its main parts when they carry different carbs (a sandwich is bread + filling + cheese + spread). Keep a packaged food or a single prepared dish as one item.
- If a food is one of their saved foods listed above, set preset_id to that id and servings to how many servings they ate. Otherwise preset_id and servings are null.
- Always estimate carbs, even when unsure. Never return null for carbs.
- title: a short name for the whole meal, e.g. "Chicken sandwich on GF bread".
- If they said a clock time ("at 8", "at noon"), set eaten_at to it as 24-hour "HH:MM" (use their local time to tell morning from evening). If they said a relative time ("an hour ago"), set minutes_ago. Otherwise both are null.
- confidence: 0 to 1, how close you think the carb total is.
- If what they said is not about food or drink, return an empty items list.
"""

# Words that say nothing about which food was meant, ignored when matching
# what was said against saved preset names.
_MATCH_STOPWORDS = frozenset(
    "and the with for had ate just about some one two three four half bag bags cup cups "
    "slice slices piece pieces serving servings small large medium breakfast lunch dinner snack".split()
)


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


def _valid_timestamp(value: Any) -> str | None:
    ts = value or _now_iso()
    try:
        datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts


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
        "source": body.get("source") if body.get("source") in ("barcode", "label", "manual", "voice") else "manual",
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
    ts = _valid_timestamp(body.get("timestamp"))
    if ts is None:
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


def _match_words(text: str) -> set[str]:
    words = set()
    for word in re.findall(r"[a-z]+", text.lower()):
        if len(word) < 3 or word in _MATCH_STOPWORDS:
            continue
        words.add(word[:-1] if len(word) > 3 and word.endswith("s") else word)
    return words


def preset_candidates(said: str) -> list[dict[str, Any]]:
    """Saved presets sharing a meaningful word with what was said, most-used
    first, so the model can reuse their exact package numbers."""
    spoken = _match_words(said)
    if not spoken:
        return []
    scored = [(len(spoken & _match_words(f"{p.get('name') or ''} {p.get('brand') or ''}")), p) for p in list_presets()]
    return [p for score, p in sorted(scored, key=lambda pair: -pair[0]) if score > 0][:PRESET_HINT_LIMIT]


def _preset_hint(preset: dict[str, Any]) -> str:
    parts = [f"{preset['carbs']:g} g carbs"]
    parts += [f"{preset[m]:g} g {m}" for m in ("fiber", "protein", "fat") if preset.get(m) is not None]
    if preset.get("calories") is not None:
        parts.append(f"{preset['calories']:g} kcal")
    brand = f" ({preset['brand']})" if preset.get("brand") else ""
    serving = preset.get("serving_label") or "1 serving"
    return f'- id={preset["id"]}: "{preset["name"]}"{brand}, per {serving}: ' + ", ".join(parts)


def _local_zone(name: Any) -> Any:
    for candidate in (name, APP_TIMEZONE):
        if not candidate:
            continue
        try:
            return ZoneInfo(str(candidate))
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return timezone.utc


# A clock time as Whisper writes it: "at 3:30", "at 330", "at 3 30", "at 12.15",
# "at 5 p.m.", "10.05am", "8.45 a.m.". A bare "at 5" is left to the model, since
# without minutes or am/pm it is too easily something else.
_SPOKEN_TIME = re.compile(
    r"(?:\b(?P<at>at|around|about)(?:\s+(?:about|around))?\s+)?"
    r"(?<![\d.:,$])\b(?:(?P<hm>\d{3,4})|(?P<h>\d{1,2})(?:(?:[:.]|\s)(?P<m>[0-5]\d))?)"
    r"(?:\s*(?P<mer>[ap])\.?\s?m\b\.?|\s*(?P<oclock>o'?\s?clock)\b)?"
    r"(?![.:,]?\d)"
    r"(?!\s*(?:g|grams?|mg|kg|ml|oz|ounces?|lbs?|pounds?|cal|calories|kcal|carbs?|cups?|%|percent)\b)",
    re.IGNORECASE,
)


def _clock_of(match: re.Match[str]) -> tuple[int, int, str | None] | None:
    if match.group("hm"):
        hour, minute = int(match.group("hm")[:-2]), int(match.group("hm")[-2:])
    else:
        hour, minute = int(match.group("h")), int(match.group("m") or 0)
    meridiem = (match.group("mer") or "").lower() or None
    if meridiem is None and not (match.group("at") and (match.group("hm") or match.group("m") or match.group("oclock"))):
        return None
    if minute > 59 or hour > (12 if meridiem else 23) or (meridiem and hour == 0):
        return None
    return hour, minute, meridiem


def parse_spoken_time(said: str, now_local: datetime) -> tuple[datetime | None, str]:
    """Find a clock time in what was said and return when it was (the most
    recent past one, so a bare "330" in the afternoon is 15:30 and "at 9" late
    at night is 21:00), plus the words with the time taken out, so the model
    can't mistake "330" for a portion size."""
    when = None
    kept, last = [], 0
    for match in _SPOKEN_TIME.finditer(said):
        clock = _clock_of(match)
        if clock is None:
            continue
        kept.append(said[last:match.start()])
        last = match.end()
        if when is None:
            when = _most_recent_clock(*clock, now_local)
    kept.append(said[last:])
    rest = re.sub(r" ([,.!?;:])", r"\1", " ".join("".join(kept).split()))
    return when, rest.strip(" ,;:")


def _most_recent_clock(hour: int, minute: int, meridiem: str | None, now_local: datetime) -> datetime:
    if meridiem:
        hours = [hour % 12 + (12 if meridiem == "p" else 0)]
    elif hour == 0 or hour > 12:
        hours = [hour]
    else:
        hours = [hour % 12, hour % 12 + 12]
    candidates = []
    for h in hours:
        when = now_local.replace(hour=h, minute=minute, second=0, microsecond=0)
        if when > now_local + timedelta(minutes=10):  # later than now -> yesterday
            when -= timedelta(days=1)
        candidates.append(when)
    return max(candidates)


def resolve_eaten_at(result: dict[str, Any], now_local: datetime) -> datetime | None:
    """A spoken clock time ("at 8") or relative time ("an hour ago") -> when.
    A clock time later than now means yesterday."""
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(result.get("eaten_at") or ""))
    if match and int(match.group(1)) < 24 and int(match.group(2)) < 60:
        when = now_local.replace(hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0)
        if when > now_local + timedelta(minutes=10):
            when -= timedelta(days=1)
        return when
    minutes = _num(result.get("minutes_ago"))
    if minutes and 0 < minutes <= 24 * 60:
        return now_local - timedelta(minutes=minutes)
    return None


def _clean_item(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()[:80]
    if not name:
        return None
    item: dict[str, Any] = {
        "name": name,
        "portion": str(raw.get("portion") or "").strip()[:60] or None,
        "preset_id": str(raw["preset_id"]) if raw.get("preset_id") else None,
        "servings": _num(raw.get("servings")),
    }
    for m in MACROS:
        value = _num(raw.get(m))
        item[m] = round(max(value, 0.0), 1) if value is not None else None
    return item


def _apply_preset(item: dict[str, Any], preset: dict[str, Any], servings: Any) -> dict[str, Any]:
    """Replace an estimate with the preset's exact per-serving numbers."""
    count = max(0.25, min(_num(servings) or preset.get("default_servings") or 1.0, 20.0))
    label = preset.get("serving_label") or "serving"
    values = {m: (round(_num(preset.get(m)) * count, 1) if _num(preset.get(m)) is not None else None) for m in MACROS}
    return {**item, "name": preset["name"], "portion": label if count == 1 else f"{count:g} × {label}",
            "preset_id": preset["id"], "servings": count, **values}


def _totals(items: list[dict[str, Any]]) -> dict[str, float | None]:
    totals: dict[str, float | None] = {}
    for m in MACROS:
        values = [i[m] for i in items if i.get(m) is not None]
        totals[m] = round(sum(values), 1) if values else None
    return totals


async def estimate_meal(body: dict[str, Any]) -> dict[str, Any]:
    """What was said (a recording, or typed text) -> foods with estimated
    nutrition. Nothing is logged until the person confirms."""
    said = str(body.get("text") or "")
    if body.get("audio"):
        try:
            said = await speech.transcribe(body["audio"])
        except speech.SpeechError as err:
            return {"error": str(err), "_status": 400}
        except Exception as err:
            log.warning("speech transcription failed: %s", type(err).__name__)
            return {"error": "Couldn't transcribe that recording. Try again, or type it.", "_status": 502}
    said = " ".join(said.split())[:TRANSCRIPT_LIMIT]
    if not said:
        message = "Didn't catch anything. Try again a little closer to the mic." if body.get("audio") else "Say or type what you ate."
        return {"error": message, "_status": 422, "transcript": ""}
    now_local = datetime.now(_local_zone(body.get("tz")))
    # A clock time is read here, not by the model: Whisper writes "at 3:30" as
    # "at 330" or "at 3.30", which the model misreads (or takes for a portion).
    spoken_when, food_said = parse_spoken_time(said, now_local)
    candidates = preset_candidates(food_said)
    known = ""
    if candidates:
        known = "\nTheir saved foods (exact nutrition from the package, per serving):\n" + "\n".join(_preset_hint(p) for p in candidates) + "\n"
    prompt = MEAL_PROMPT.format(said=food_said.replace('"', "'"), now=now_local.strftime("%a %H:%M"), known=known)
    try:
        result = await invoke_llm(prompt, response_json_schema=MEAL_SCHEMA, max_tokens=900, site="food_estimate")
    except Exception as err:  # the model layer already logs details
        return {"error": f"Couldn't estimate that: {err}", "_status": 502, "transcript": said}
    result = result if isinstance(result, dict) else {}
    raw_items = result.get("items") if isinstance(result.get("items"), list) else []
    by_id = {str(p["id"]): p for p in candidates}
    items = []
    for raw in raw_items[:MEAL_ITEM_LIMIT]:
        item = _clean_item(raw)
        if item is None:
            continue
        preset = by_id.get(item["preset_id"] or "")
        items.append(_apply_preset(item, preset, item["servings"]) if preset else {**item, "preset_id": None, "servings": None})
    if not items:
        return {"error": "That didn't sound like food. Try again, or type it.", "_status": 422, "transcript": said}
    when = spoken_when or resolve_eaten_at(result, now_local)
    confidence = _num(result.get("confidence"))
    return {
        "ok": True,
        "transcript": said,
        "meal": {
            "title": str(result.get("title") or "").strip()[:120] or items[0]["name"],
            "items": items,
            "totals": _totals(items),
            "confidence": max(0.0, min(confidence, 1.0)) if confidence is not None else None,
        },
        "timestamp": when.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z") if when else None,
    }


def log_meal(body: dict[str, Any]) -> dict[str, Any]:
    """Log a described meal as ONE carb Treatment (one meal, one carb event for
    the analysis engines), keeping the per-food breakdown alongside."""
    items = [i for i in (_clean_item(r) for r in (body.get("items") or [])[:MEAL_ITEM_LIMIT]) if i]
    if not items:
        return {"error": "Add at least one food.", "_status": 400}
    if any(i["carbs"] is None for i in items):
        return {"error": "Every food needs a carb amount.", "_status": 400}
    ts = _valid_timestamp(body.get("timestamp"))
    if ts is None:
        return {"error": "A valid time is required.", "_status": 400}
    name = str(body.get("name") or "").strip()[:120] or items[0]["name"]
    used_presets = []
    for item in items:
        preset = _get("FoodPreset", item["preset_id"]) if item["preset_id"] else None
        if preset and preset.get("owner_email") == OWNER_EMAIL:
            used_presets.append(preset)
        else:
            item["preset_id"] = None
    totals = _totals(items)
    described = "; ".join(f"{i['name']} ({i['portion']})" if i["portion"] else i["name"] for i in items)
    treatment = get_repositories().treatments.create({
        "type": "carb",
        "event_type": "Carbs",
        "timestamp": ts,
        "amount": totals["carbs"],
        "notes": f"{name}: {described}"[:500],
        "source": "manual",
        "entry_method": "voice" if body.get("entry_method") != "text" else "text",
        "food_name": name,
        "servings": 1,
        "serving_label": "meal",
        "protein_g": totals["protein"],
        "fat_g": totals["fat"],
        "fiber_g": totals["fiber"],
        "sugars_g": totals["sugars"],
        "calories": totals["calories"],
        "items": items,
        "transcript": str(body.get("transcript") or "").strip()[:TRANSCRIPT_LIMIT] or None,
        "owner_email": OWNER_EMAIL,
    })
    for preset in {p["id"]: p for p in used_presets}.values():
        db.update_entity("FoodPreset", preset["id"], {"use_count": (preset.get("use_count") or 0) + 1, "last_used": ts})
    if body.get("save_preset"):
        db.create_entity("FoodPreset", {
            "name": name, "brand": None, "serving_label": "1 meal", "default_servings": 1.0, "barcode": None,
            "source": "voice", **totals, "use_count": 1, "last_used": ts, "owner_email": OWNER_EMAIL,
        })
    return {"ok": True, "treatment": treatment}


def recent_logs() -> list[dict[str, Any]]:
    rows = get_repositories().treatments.query(
        {"owner_email": OWNER_EMAIL, "type": "carb", "source": "manual"}, "-timestamp", RECENT_LIMIT
    )
    return [r for r in rows if r.get("food_name")]


MEAL_RESPONSE_DAYS = 90
TEMP_BASAL_MAX_HOURS = 12


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _treatments_between(start: datetime, end: datetime) -> list[dict[str, Any]]:
    repository = get_repositories().treatments
    output: list[dict[str, Any]] = []
    while True:
        page = repository.query(
            {"owner_email": OWNER_EMAIL, "timestamp": {"$gte": _iso(start), "$lte": _iso(end)}},
            "timestamp", 5_000, len(output),
        )
        output.extend(page)
        if len(page) < 5_000:
            return output


def _typed_glucose_between(start: datetime, end: datetime) -> list[dict[str, Any]]:
    """CGM from the typed glucose_readings table, on its canonical UTC clock."""
    connection = db.connect()
    try:
        rows = connection.execute(
            "SELECT observed_at, value_mg_dl FROM glucose_readings "
            "WHERE owner_id=? AND observed_at >= ? AND observed_at <= ? ORDER BY observed_at, entity_id",
            (DEPLOYMENT_OWNER_ID, _iso(start), _iso(end)),
        ).fetchall()
    finally:
        connection.close()
    return [{"timestamp": row[0], "value": row[1]} for row in rows]


def _walking_between(start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Effective walking segments, with manual intervals taking precedence as
    on the activity page."""
    return [
        {"activity": row.get("activity"), **segment}
        for row in resolve_intervals(SqliteActivityPositionRepository().list(start, end))
        if row.get("activity") == "walking"
        for segment in row["effective_segments"]
    ]


def meal_response_for_range(start: datetime, end: datetime) -> dict[str, Any]:
    follow_end = end + timedelta(minutes=meal_response.FOLLOW_MINUTES)
    treatments = _treatments_between(
        start - timedelta(hours=TEMP_BASAL_MAX_HOURS, minutes=meal_response.INSULIN_BEFORE_MINUTES),
        end + timedelta(minutes=meal_response.INSULIN_AFTER_MINUTES),
    )
    readings = _typed_glucose_between(start - timedelta(minutes=meal_response.PRIOR_MINUTES + 10), follow_end)
    return meal_response.analyze(treatments, readings, _walking_between(start, follow_end), start=start, end=end)


async def handle(body: dict[str, Any]) -> dict[str, Any]:
    action = body.get("action", "presets")
    if action == "lookup_barcode":
        return await lookup_barcode(body.get("barcode"))
    if action == "scan_label":
        return await scan_label(body.get("image"))
    if action == "estimate_meal":
        return await estimate_meal(body)
    if action == "log_meal":
        return log_meal(body)
    if action == "presets":
        return {"presets": list_presets(), "recent": recent_logs()}
    if action == "meal_response":
        days = max(7, min(int(_num(body.get("days")) or MEAL_RESPONSE_DAYS), 365))
        end = datetime.now(timezone.utc)
        return meal_response_for_range(end - timedelta(days=days), end)
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
