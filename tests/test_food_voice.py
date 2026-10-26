"""Voice food logging: on-box transcription, the per-food estimate, preset
reuse, USDA table grounding, spoken times, and logging a described meal as one
carb Treatment."""

import asyncio
import base64
import gzip
import io
import math
import re
import struct
import time
import wave
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from server import breeze, db, food, nutrition, speech
from server.migrations import run_migrations
from server.repositories import get_repositories


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "data" / "app.sqlite3"
    path.parent.mkdir()
    run_migrations(path)
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


def _tone_wav(seconds=1.0, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = int(seconds * rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(frames)))
    return buf.getvalue()


def _audio_payload(data):
    return "audio/wav|" + base64.b64encode(data).decode()


def _preset(**fields):
    return db.create_entity("FoodPreset", {"serving_label": "1 serving", "default_servings": 1.0, "use_count": 0,
                                           "owner_email": food.OWNER_EMAIL, **fields})


# ── speech ────────────────────────────────────────────────────────────────


def test_decode_audio_resamples_to_16k_mono():
    samples = speech.decode_audio(_tone_wav(seconds=1.0, rate=44100))
    assert abs(samples.size - 16000) < 400
    assert samples.dtype.name == "float32" and float(abs(samples).max()) <= 1.0


def test_decode_audio_rejects_garbage_and_overlong_clips(monkeypatch):
    with pytest.raises(speech.SpeechError):
        speech.decode_audio(b"not audio at all" * 50)
    monkeypatch.setattr(speech, "MAX_SECONDS", 1)
    with pytest.raises(speech.SpeechError, match="limited to 1 seconds"):
        speech.decode_audio(_tone_wav(seconds=2.0))


@pytest.mark.parametrize("payload", ["", "no-separator", "audio/webm|***not base64***", "audio/webm|"])
def test_transcribe_rejects_bad_payloads(payload):
    with pytest.raises(speech.SpeechError):
        asyncio.run(speech.transcribe(payload))


def test_transcribe_rejects_oversized_recordings(monkeypatch):
    monkeypatch.setattr(speech, "MAX_AUDIO_BYTES", 10)
    with pytest.raises(speech.SpeechError, match="too large"):
        asyncio.run(speech.transcribe(_audio_payload(_tone_wav(0.1))))


def test_transcribe_runs_the_local_model_on_decoded_samples(monkeypatch):
    seen = {}

    class Segment:
        def __init__(self, text):
            self.text = text

    class FakeModel:
        def transcribe(self, samples, **opts):
            seen["samples"], seen["opts"] = samples, opts
            return iter([Segment(" One bag of"), Segment(" yogurt bites. ")]), None

    monkeypatch.setattr(speech, "_load_model", lambda: FakeModel())
    text = asyncio.run(speech.transcribe(_audio_payload(_tone_wav(0.5))))
    assert text == "One bag of yogurt bites."
    assert abs(seen["samples"].size - 8000) < 400
    assert seen["opts"]["language"] == "en" and seen["opts"]["vad_filter"] is True


def test_transcribe_puts_right_a_known_mishearing(monkeypatch):
    class FakeModel:
        def transcribe(self, samples, **opts):
            return iter([type("Segment", (), {"text": " Cash browns corned beef at 10.45 a.m."})()]), None

    monkeypatch.setattr(speech, "_load_model", lambda: FakeModel())
    assert asyncio.run(speech.transcribe(_audio_payload(_tone_wav(0.5)))) == "Hash browns corned beef at 10.45 a.m."


@pytest.mark.parametrize("heard, meant", [
    ("eggs and cash brown", "eggs and hash brown"),
    ("CASH BROWNS", "Hash BROWNS"),
    ("paid cash for a muffin", "paid cash for a muffin"),
    ("cashews", "cashews"),
])
def test_put_right(heard, meant):
    assert speech.put_right(heard) == meant


# ── estimate ──────────────────────────────────────────────────────────────

SANDWICH = {
    "title": "Chicken sandwich on GF bread",
    "items": [
        {"name": "Gluten-free bread", "portion": "2 slices", "preset_id": None, "servings": None,
         "carbs": 34, "fiber": 4, "sugars": 3, "protein": 4, "fat": 4, "calories": 180},
        {"name": "Chicken breast", "portion": "3 oz", "preset_id": None, "servings": None,
         "carbs": 0, "fiber": 0, "sugars": 0, "protein": 26, "fat": 3, "calories": 130},
        {"name": "Swiss cheese", "portion": "1 slice", "preset_id": None, "servings": None,
         "carbs": 0.4, "fiber": None, "sugars": 0, "protein": 8, "fat": 8, "calories": 106},
    ],
    "eaten_at": None, "minutes_ago": None, "confidence": 0.7,
}


def test_estimate_from_text_grounds_items_in_usda(database, monkeypatch):
    seen = []

    async def fake_invoke(prompt, response_json_schema=None, max_tokens=0, images=None, site=""):
        seen.append({"prompt": prompt, "site": site, "schema": response_json_schema, "images": images})
        if site == "food_match":
            return {"matches": [{"food": "Chicken breast", "usda": "Chicken breast, grilled without sauce, skin not eaten"}]}
        return SANDWICH

    monkeypatch.setattr(food, "invoke_llm", fake_invoke)
    r = asyncio.run(food.handle({"action": "estimate_meal", "text": "chicken sandwich on gluten free bread with swiss cheese"}))
    assert r["ok"] and r["transcript"] == "chicken sandwich on gluten free bread with swiss cheese"
    estimate, match = seen
    assert estimate["site"] == "food_estimate" and estimate["schema"] is food.MEAL_SCHEMA and not estimate["images"]
    assert "chicken sandwich on gluten free bread" in estimate["prompt"] and "Their saved foods" not in estimate["prompt"]
    # Bread and cheese were said by their USDA names; only the chicken needed choosing.
    assert match["site"] == "food_match" and match["schema"] is food.MATCH_SCHEMA
    assert 'Food: "Chicken breast" (3 oz)' in match["prompt"] and "Gluten-free bread" not in match["prompt"]
    meal = r["meal"]
    assert meal["title"] == "Chicken sandwich on GF bread" and len(meal["items"]) == 3
    bread, chicken, cheese = meal["items"]
    assert bread["source"] == "usda" and bread["fdc_name"] == "Bread, gluten free" and bread["grams"] == 56
    assert bread["carbs"] == 25.6 and bread["fiber"] == 2.4  # USDA, not the model's 34 g
    assert chicken["source"] == "usda" and chicken["carbs"] == 0 and chicken["grams"] == 85.1
    assert cheese["source"] == "usda" and cheese["fdc_name"] == "Cheese, Swiss" and cheese["carbs"] == 0.3
    assert meal["totals"]["carbs"] == 25.9
    assert meal["confidence"] == 0.7 and r["timestamp"] is None


def test_estimate_uses_a_named_presets_exact_numbers(database, monkeypatch):
    bites = _preset(name="Yogurt Bites", brand="Gerber", serving_label="1 bag (28 g)", carbs=21, fiber=0, protein=3, fat=1, calories=110)
    _preset(name="Oat bar", carbs=27)
    seen = {}

    async def fake_invoke(prompt, **kwargs):
        seen["prompt"] = prompt
        return {"title": "Yogurt bites", "items": [
            {"name": "yogurt bites", "portion": "2 bags", "preset_id": bites["id"], "servings": 2,
             "carbs": 30, "fiber": 0, "sugars": 9, "protein": 2, "fat": 1, "calories": 100},
        ], "eaten_at": None, "minutes_ago": None, "confidence": 0.9}

    monkeypatch.setattr(food, "invoke_llm", fake_invoke)
    r = asyncio.run(food.estimate_meal({"text": "two bags of yogurt bites"}))
    assert f'id={bites["id"]}: "Yogurt Bites" (Gerber), per 1 bag (28 g): 21 g carbs' in seen["prompt"]
    assert "Oat bar" not in seen["prompt"]
    item = r["meal"]["items"][0]
    assert item["source"] == "preset" and item["fdc_id"] is None  # the saved food beats the table
    assert item["preset_id"] == bites["id"] and item["servings"] == 2
    assert item["name"] == "Yogurt Bites" and item["portion"] == "2 × 1 bag (28 g)"
    assert item["carbs"] == 42 and item["protein"] == 6 and item["calories"] == 220
    assert item["sugars"] is None  # the preset has none -> the model's guess is not kept


def test_estimate_ignores_a_preset_id_it_was_not_offered(database, monkeypatch):
    async def fake_invoke(prompt, **kwargs):
        return {**SANDWICH, "items": [{**SANDWICH["items"][0], "preset_id": "made-up", "servings": 3}]}

    monkeypatch.setattr(food, "invoke_llm", fake_invoke)
    item = asyncio.run(food.estimate_meal({"text": "toast"}))["meal"]["items"][0]
    assert item["preset_id"] is None and item["servings"] is None
    assert item["source"] == "usda" and item["carbs"] == 25.6


def test_estimate_from_audio_transcribes_first(database, monkeypatch):
    async def fake_transcribe(audio):
        assert audio == "audio/webm|abc"
        return "  one bag of   yogurt bites "

    async def fake_invoke(prompt, **kwargs):
        assert 'What they said: "one bag of yogurt bites"' in prompt
        return SANDWICH

    monkeypatch.setattr(speech, "transcribe", fake_transcribe)
    monkeypatch.setattr(food, "invoke_llm", fake_invoke)
    r = asyncio.run(food.estimate_meal({"audio": "audio/webm|abc"}))
    assert r["ok"] and r["transcript"] == "one bag of yogurt bites"


def test_estimate_reports_speech_and_model_failures(database, monkeypatch):
    async def bad_audio(audio):
        raise speech.SpeechError("Couldn't read that recording.")

    monkeypatch.setattr(speech, "transcribe", bad_audio)
    r = asyncio.run(food.estimate_meal({"audio": "audio/webm|abc"}))
    assert r["_status"] == 400 and r["error"] == "Couldn't read that recording."

    async def silence(audio):
        return ""

    monkeypatch.setattr(speech, "transcribe", silence)
    r = asyncio.run(food.estimate_meal({"audio": "audio/webm|abc"}))
    assert r["_status"] == 422 and "Didn't catch anything" in r["error"]

    async def down(prompt, **kwargs):
        raise RuntimeError("model offline")

    monkeypatch.setattr(food, "invoke_llm", down)
    r = asyncio.run(food.estimate_meal({"text": "a banana"}))
    assert r["_status"] == 502 and r["transcript"] == "a banana"

    async def not_food(prompt, **kwargs):
        return {"title": "", "items": [], "eaten_at": None, "minutes_ago": None, "confidence": 0}

    monkeypatch.setattr(food, "invoke_llm", not_food)
    r = asyncio.run(food.estimate_meal({"text": "remind me to call the doctor"}))
    assert r["_status"] == 422 and r["transcript"] == "remind me to call the doctor"

    async def malformed(prompt, **kwargs):
        return {"title": "x", "items": {"name": "not a list"}}

    monkeypatch.setattr(food, "invoke_llm", malformed)
    assert asyncio.run(food.estimate_meal({"text": "toast"}))["_status"] == 422


def test_estimate_returns_a_spoken_time_as_utc(database, monkeypatch):
    async def fake_invoke(prompt, **kwargs):
        return {**SANDWICH, "minutes_ago": 60}

    monkeypatch.setattr(food, "invoke_llm", fake_invoke)
    before = datetime.now(timezone.utc)
    r = asyncio.run(food.estimate_meal({"text": "a sandwich an hour ago", "tz": "America/Chicago"}))
    ts = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
    assert r["timestamp"].endswith("Z") and abs((before - ts).total_seconds() - 3600) < 60


NOW = datetime(2026, 10, 16, 12, 40, tzinfo=ZoneInfo("America/New_York"))


@pytest.mark.parametrize("result, expected", [
    ({"eaten_at": "08:00"}, datetime(2026, 10, 16, 8, 0, tzinfo=NOW.tzinfo)),
    ({"eaten_at": "20:30"}, datetime(2026, 10, 15, 20, 30, tzinfo=NOW.tzinfo)),  # later than now -> yesterday
    ({"eaten_at": "12:45"}, datetime(2026, 10, 16, 12, 45, tzinfo=NOW.tzinfo)),  # a few minutes ahead is still today
    ({"minutes_ago": 90}, datetime(2026, 10, 16, 11, 10, tzinfo=NOW.tzinfo)),
    ({"eaten_at": "08:00", "minutes_ago": 15}, datetime(2026, 10, 16, 8, 0, tzinfo=NOW.tzinfo)),
    ({"eaten_at": None, "minutes_ago": None}, None),
    ({"eaten_at": "25:00"}, None),
    ({"eaten_at": "noon"}, None),
    ({"minutes_ago": 5000}, None),
])
def test_resolve_eaten_at(result, expected):
    assert food.resolve_eaten_at(result, NOW) == expected


def test_unknown_timezone_falls_back_to_the_app_zone():
    assert food._local_zone("Not/AZone") == ZoneInfo(food.APP_TIMEZONE)
    assert food._local_zone("Europe/Paris") == ZoneInfo("Europe/Paris")


def test_preset_candidates_match_meaningful_words(database):
    bites = _preset(name="Yogurt Bites", brand="Gerber", carbs=21)
    _preset(name="Oat bar", carbs=27)
    _preset(name="Bag of chips", carbs=15)
    assert [p["id"] for p in food.preset_candidates("one bag of yogurt bite")] == [bites["id"]]
    assert food.preset_candidates("a bag") == []


def test_preset_candidates_need_more_than_one_everyday_word(database):
    bites = _preset(name="Yoggies yogurt bites", carbs=15)
    # One shared word that any yogurt carries isn't the saved bites.
    assert food.preset_candidates("Yogurt bowl") == []
    assert food.preset_candidates("Greek yogurt bowl with blueberries") == []
    # Most of its name, or its brand alone, is.
    assert [p["id"] for p in food.preset_candidates("Two bags of yogurt bites.")] == [bites["id"]]
    assert [p["id"] for p in food.preset_candidates("some Yoggies")] == [bites["id"]]
    assert nutrition.is_food_word("yogurt") and not nutrition.is_food_word("yoggies")


# ── USDA table ────────────────────────────────────────────────────────────


def _food(name):
    return next(f for f in nutrition.table().foods if f.name == name)


def test_bundled_table_is_usda_with_all_macros():
    t = nutrition.table()
    assert len(t.foods) > 7000 and "USDA FoodData Central" in gzip.open(nutrition.TABLE_PATH).read(200).decode()
    assert all(len(f.per_100g) == len(nutrition.MACROS) and f.per_100g[0] is not None for f in t.foods)
    assert {f.dataset for f in t.foods} == {"fndds", "sr"}


@pytest.mark.parametrize("text, quantity, unit, size, stated", [
    ("2 slices", 2, "slic", None, None),
    ("1 bag (28 g)", 1, "bag", None, 28.0),
    ("half a cup", 0.5, "cup", None, None),
    ("1 1/2 cups", 1.5, "cup", None, None),
    ("1½ cups", 1.5, "cup", None, None),
    ("two large eggs", 2, "egg", "larg", None),
    ("3 oz", 3, "oz", None, 85.0),
    ("12 oz can", 12, "floz", None, None),  # a can's ounces are fluid ounces
    ("8 fl oz", 8, "floz", None, None),
    ("2 tablespoons", 2, "tbsp", None, None),
    ("1 medium", 1, None, "medium", None),
    ("", 1, None, None, None),
])
def test_parse_portion(text, quantity, unit, size, stated):
    p = nutrition.parse_portion(text)
    assert (p.quantity, p.unit, p.size) == (quantity, unit, size)
    assert p.stated_grams == (pytest.approx(stated, abs=0.1) if stated else None)


@pytest.mark.parametrize("name, portion, grams", [
    ("Bread, gluten free", "2 slices", 56.0),            # 2 × "1 medium or regular slice", not the crust-off one
    ("Muffin, fruit", "1", 130.0),                        # a muffin is a medium one
    ("Muffin, fruit", "1 large", 180.0),
    ("Soft drink, cola", "1 can", 372.0),
    ("Soft drink, cola", "12 oz can", 372.0),
    ("Pasta with tomato-based sauce and meat, restaurant", "1 bowl", 375.0),  # a bowl is 1.5 cups
    ("Babyfood, snack, GERBER, GRADUATES, YOGURT MELTS", "1 bag", 28.0),
    ("Chicken breast, grilled without sauce, skin not eaten", "3 oz", 85.0),
    ("Pizza with pepperoni, from restaurant or fast food, NS as to type of crust", "1 slice", 124.0),  # piece, NFS
    ("Potato, french fries, restaurant", "1 medium order", 145.0),
    ("Rice, white, cooked, no added fat", "1 serving", 118.0),  # USDA's typical amount
    ("Soft drink, cola", "1 bag (28 g)", 28.0),          # no such USDA measure: the stated weight
])
def test_portion_grams_uses_usdas_household_measures(name, portion, grams):
    assert nutrition.portion_grams(_food(name), portion)[0] == pytest.approx(grams, abs=0.2)


def test_portion_grams_gives_up_on_a_unit_usda_does_not_list():
    assert nutrition.portion_grams(_food("Bread, gluten free"), "1 plate") is None
    assert nutrition.portion_grams(_food("Restaurant, Italian, spaghetti with meat sauce"), "1 bowl") is None


def test_negated_words_are_not_counted():
    assert nutrition.tokens("Chicken breast, grilled without sauce, skin not eaten") == ["chicken", "breast", "grill", "skin"]
    assert "fat" not in nutrition.tokens("Rice, white, cooked, no added fat")


@pytest.mark.parametrize("said, usda", [
    ("Swiss cheese", "Cheese, Swiss"),
    ("Gluten-free bread", "Bread, gluten free"),
    ("Banana", "Banana, raw"),
    ("Cheeseburger", "Cheeseburger, NFS"),
    ("Yogurt bites", "Babyfood, snack, GERBER, GRADUATES, YOGURT MELTS"),  # a curated common name
    ("Spaghetti with meat sauce", None),  # same words as "Spaghetti sauce with meat", different food
    ("Cream cheese", "Cream cheese, regular, plain"),  # not a baked potato listing it as a topping
    ("Blueberry muffin", None),
])
def test_named_match_only_takes_a_foods_own_name(said, usda):
    found = nutrition.named_match(said, nutrition.candidates(said))
    assert (found.food.name if found else None) == usda


def test_candidates_offer_the_dish_and_only_what_the_portion_can_weigh():
    names = [m.food.name for m in nutrition.candidates("Spaghetti with meat sauce", 8, "1 bowl")]
    assert "Pasta with tomato-based sauce and meat, restaurant" in names
    assert "Restaurant, Italian, spaghetti with meat sauce" not in names  # only "1 serving (554 g)"
    fries = nutrition.candidates("French fries", 8, "1 medium order")
    assert fries and all(nutrition.portion_grams(m.food, "1 medium order")[1].endswith("medium fast food order")
                         for m in fries)


def test_candidates_keep_versions_that_differ_in_carbs():
    names = [m.food.name for m in nutrition.candidates("French fries")]
    assert "Potato, french fries, NFS" in names and "Potato, french fries, restaurant" in names


def _two_step(estimate, choices):
    async def fake_invoke(prompt, site="", **kwargs):
        if site == "food_match":
            asked = re.findall(r'^Food: "([^"]+)"', prompt, flags=re.MULTILINE)
            return {"matches": [{"food": name, "usda": choices.get(name, "none")} for name in asked]}
        return estimate
    return fake_invoke


def _meal(*items, **extra):
    return {"title": "Meal", "items": [
        {"preset_id": None, "servings": None, "fiber": None, "sugars": None, "protein": None, "fat": None,
         "calories": None, **item} for item in items
    ], "eaten_at": None, "minutes_ago": None, "confidence": 0.6, **extra}


def test_estimate_keeps_the_models_numbers_when_no_usda_entry_fits(database, monkeypatch):
    estimate = _meal({"name": "Grandma's casserole", "portion": "1 cup", "carbs": 31},
                     {"name": "Spaghetti with meat sauce", "portion": "1 bowl", "carbs": 45})
    monkeypatch.setattr(food, "invoke_llm", _two_step(estimate, {"Spaghetti with meat sauce": "none"}))
    casserole, spaghetti = asyncio.run(food.estimate_meal({"text": "grandma's casserole and spaghetti"}))["meal"]["items"]
    assert casserole["source"] == "estimate" and casserole["carbs"] == 31 and casserole["fdc_id"] is None
    assert spaghetti["source"] == "estimate" and spaghetti["carbs"] == 45


def test_estimate_ignores_a_usda_entry_it_was_not_offered(database, monkeypatch):
    estimate = _meal({"name": "Spaghetti with meat sauce", "portion": "1 bowl", "carbs": 45})
    monkeypatch.setattr(food, "invoke_llm", _two_step(estimate, {"Spaghetti with meat sauce": "Lasagna, made up"}))
    item = asyncio.run(food.estimate_meal({"text": "spaghetti"}))["meal"]["items"][0]
    assert item["source"] == "estimate" and item["carbs"] == 45


def test_estimate_accepts_a_loosely_copied_entry(database, monkeypatch):
    estimate = _meal({"name": "Spaghetti with meat sauce", "portion": "1 bowl", "carbs": 45})
    copied = "  pasta with tomato-based sauce and meat, RESTAURANT (also: spaghetti, any shape pasta)."
    monkeypatch.setattr(food, "invoke_llm", _two_step(estimate, {"Spaghetti with meat sauce": copied}))
    item = asyncio.run(food.estimate_meal({"text": "spaghetti"}))["meal"]["items"][0]
    assert item["source"] == "usda" and item["grams"] == 375 and item["carbs"] == 66.5


def test_estimate_keeps_the_estimate_when_the_portion_cannot_be_weighed(database, monkeypatch):
    estimate = _meal({"name": "Gluten-free bread", "portion": "1 plate", "carbs": 20})
    monkeypatch.setattr(food, "invoke_llm", _two_step(estimate, {}))
    item = asyncio.run(food.estimate_meal({"text": "a plate of gluten free bread"}))["meal"]["items"][0]
    assert item["source"] == "estimate" and item["carbs"] == 20


def test_estimate_falls_back_to_word_matching_when_the_choice_call_fails(database, monkeypatch):
    estimate = _meal({"name": "Gluten-free bread", "portion": "2 slices", "carbs": 18},
                     {"name": "Blueberry muffin", "portion": "1", "carbs": 20},
                     {"name": "Mystery bar", "portion": "1", "carbs": 22})

    async def fake_invoke(prompt, site="", **kwargs):
        if site == "food_match":
            raise RuntimeError("model busy")
        return estimate

    monkeypatch.setattr(food, "invoke_llm", fake_invoke)
    bread, muffin, bar = asyncio.run(food.estimate_meal({"text": "toast, a muffin and a bar"}))["meal"]["items"]
    assert bread["source"] == "usda" and bread["carbs"] == 25.6        # named: no choice needed
    assert muffin["source"] == "usda" and muffin["fdc_name"] == "Muffin, fruit" and muffin["carbs"] == 68.9
    assert bar["source"] == "estimate" and bar["carbs"] == 22


# What the live local model (qwen3-vl-8b on vLLM) said for each phrase, recorded
# 2026-10-16: the foods and portions it heard with its own carb guess, and its
# reply when asked to pick USDA entries (None: every food was said by its USDA
# name, so it wasn't asked). The reference is what USDA's dietary-recall coding
# assigns the phrase: the FNDDS food at its household measure ("a bowl" taken as
# 1.5 cups), or the package label for a branded food (Gerber Yogurt Melts, a
# 1 oz bag). The model's own guesses miss 15 of these 23 by more than 15%.
VOICE_PHRASES = [
    ("one bag of yogurt bites", 20.1,
     [("Yogurt bites", "1 bag", 30)],
     None),
    ("chicken sandwich on gluten-free bread with Swiss cheese", 25.9,
     [("Gluten-free bread", "2 slices", 20), ("Chicken breast", "3 oz", 0), ("Swiss cheese", "1 slice", 0)],
     [{"food": "Chicken breast", "usda": "Chicken breast, grilled without sauce, skin not eaten"}]),
    ("blueberry muffin", 68.9,
     [("Blueberry muffin", "1", 30)],
     [{"food": "Blueberry muffin", "usda": "Muffin, fruit (also: blueberry; Hostess Banana Walnut Mini Muffins)"}]),
    ("bowl of spaghetti with meat sauce", 71.8,
     [("Spaghetti with meat sauce", "1 bowl", 45)],
     [{"food": "Spaghetti with meat sauce", "usda": "Pasta with tomato-based sauce and meat, restaurant "
                                                    "(also: spaghetti, any shape pasta; marinara, red sauce)"}]),
    ("a banana", 28.6,
     [("Banana", "1 medium", 27)],
     None),
    ("a cup of 2% milk", 12.0,
     [("2% milk", "1 cup", 12)],
     [{"food": "a cup of 2% milk", "usda": "Milk, reduced fat (2%)"}]),
    ("two scrambled eggs and a slice of white toast", 14.5,
     [("Scrambled eggs", "2 eggs", 0), ("White bread", "1 slice", 12)],
     None),
    ("a cup of white rice", 44.2,
     [("White rice", "1 cup", 45)],
     None),
    ("a slice of pepperoni pizza", 39.7,
     [("Pepperoni pizza", "1 slice", 12)],
     [{"food": "a slice of pepperoni pizza", "usda": "Pizza with pepperoni, from restaurant or fast food, "
                                                    "NS as to type of crust"}]),
    ("a plain bagel with cream cheese", 56.4,
     [("Bagel", "1", 49), ("Cream cheese", "2 tbsp", 1)],
     None),
    ("a medium order of french fries", 60.1,
     [("French fries", "1 medium order", 30)],
     [{"food": "French fries", "usda": "Potato, french fries, restaurant"}]),
    ("a glass of orange juice", 25.2,
     [("Orange juice", "1 cup", 24)],
     None),
    ("a cheeseburger", 30.9,
     [("Cheeseburger", "1", 25)],
     None),
    ("two chocolate chip cookies", 39.2,
     [("Chocolate chip cookie", "2", 36)],
     None),
    ("a cup of mashed potatoes", 44.0,
     [("Mashed potatoes", "1 cup", 27)],
     None),
    ("a can of Coke", 38.5,
     [("Coke", "1 can", 39)],
     [{"food": "Coke", "usda": "Soft drink, cola (also: Coke)"}]),
    ("a cup of grapes", 29.1,
     [("Grapes", "1 cup", 15)],
     None),
    ("two pancakes with two tablespoons of maple syrup", 62.1,
     [("Pancake", "2 pancakes", 36), ("Maple syrup", "2 tablespoons", 16)],
     None),
    ("a granola bar", 27.7,
     [("Granola bar", "1 bar", 20)],
     [{"food": "a granola bar", "usda": "Cereal or Granola bar, NFS "
                                        "(also: New Trail Granola Bars; Sunbelt Granola Bar, all flavors)"}]),
    ("a cup of oatmeal with a tablespoon of brown sugar", 40.3,
     [("Oatmeal", "1 cup", 27), ("Brown sugar", "1 tablespoon", 12)],
     None),
    ("an apple", 29.6,
     [("Apple", "1 medium", 25)],
     None),
    ("half a cup of vanilla ice cream", 15.9,
     [("Vanilla ice cream", "half a cup", 27)],
     None),
    ("turkey sandwich on whole wheat bread with mayo", 31.1,
     [("Whole wheat bread", "2 slices", 24), ("Turkey", "3 oz", 0), ("Mayonnaise", "1 tbsp", 1)],
     None),
]


@pytest.mark.parametrize("phrase, reference, said, replies", VOICE_PHRASES, ids=[p[0] for p in VOICE_PHRASES])
def test_spoken_meals_land_within_15_percent_of_the_usda_reference(database, monkeypatch, phrase, reference, said,
                                                                    replies):
    estimate = _meal(*({"name": name, "portion": portion, "carbs": carbs} for name, portion, carbs in said))

    async def recorded(prompt, site="", **kwargs):
        if site == "food_match":
            return {"matches": replies or []}
        return estimate

    monkeypatch.setattr(food, "invoke_llm", recorded)
    meal = asyncio.run(food.estimate_meal({"text": phrase}))["meal"]
    assert [i["source"] for i in meal["items"]] == ["usda"] * len(said)
    assert meal["totals"]["carbs"] == pytest.approx(reference, rel=0.15)


def test_food_lookups_stay_off_the_breeze_route():
    assert "food_estimate" not in breeze.BREEZE_ELIGIBLE_SITES
    assert "food_match" not in breeze.BREEZE_ELIGIBLE_SITES


# ── log ───────────────────────────────────────────────────────────────────


def test_log_meal_creates_one_carb_treatment_with_the_breakdown(database):
    bites = _preset(name="Yogurt Bites", carbs=21, use_count=2)
    items = [
        {"name": "Yogurt Bites", "portion": "1 bag (28 g)", "preset_id": bites["id"], "servings": 1, "carbs": 21, "protein": 3, "fiber": 0},
        {"name": "Banana", "portion": "1 medium", "preset_id": None, "carbs": 27, "protein": 1.3, "fiber": 3.1, "calories": 105},
    ]
    r = food.log_meal({"name": "Snack", "items": items, "timestamp": "2026-10-16T16:00:00Z",
                       "transcript": "a bag of yogurt bites and a banana", "entry_method": "voice"})
    t = r["treatment"]
    assert t["type"] == "carb" and t["source"] == "manual" and t["entry_method"] == "voice"
    assert t["amount"] == 48 and t["protein_g"] == 4.3 and t["fiber_g"] == 3.1 and t["calories"] == 105
    assert t["food_name"] == "Snack" and t["servings"] == 1 and len(t["items"]) == 2
    assert t["notes"] == "Snack: Yogurt Bites (1 bag (28 g)); Banana (1 medium)"
    assert t["transcript"] == "a bag of yogurt bites and a banana"
    assert [x["id"] for x in food.recent_logs()] == [t["id"]]
    assert food._get("FoodPreset", bites["id"])["use_count"] == 3
    assert len(get_repositories().treatments.query({"owner_email": food.OWNER_EMAIL, "type": "carb"}, "-timestamp", 10)) == 1


def test_log_meal_keeps_where_each_foods_numbers_came_from(database):
    items = [
        {"name": "Gluten-free bread", "portion": "2 slices", "carbs": 25.6, "source": "usda", "fdc_id": 2707797,
         "fdc_name": "Bread, gluten free", "grams": 56},
        {"name": "Swiss cheese", "portion": "1 slice", "carbs": 1, "source": "edited", "fdc_id": 2705735,
         "fdc_name": "Cheese, Swiss", "grams": 21},
        {"name": "Pickles", "carbs": 1, "source": "made-up", "fdc_id": "x", "fdc_name": "Nope", "grams": -3},
    ]
    logged = food.log_meal({"items": items})["treatment"]["items"]
    assert [(i["source"], i["fdc_id"], i["grams"]) for i in logged] == [
        ("usda", 2707797, 56), ("edited", 2705735, 21), (None, None, None)]
    assert logged[0]["fdc_name"] == "Bread, gluten free" and logged[2]["fdc_name"] is None


def test_log_meal_can_save_the_meal_as_a_preset(database):
    food.log_meal({"name": "Usual breakfast", "items": [{"name": "Oatmeal", "carbs": 27, "protein": 5}], "save_preset": True})
    preset = next(p for p in food.list_presets() if p["name"] == "Usual breakfast")
    assert preset["carbs"] == 27 and preset["protein"] == 5 and preset["serving_label"] == "1 meal" and preset["source"] == "voice"


def test_log_meal_drops_a_preset_reference_that_does_not_exist(database):
    r = food.log_meal({"items": [{"name": "Toast", "carbs": 15, "preset_id": "gone"}]})
    assert r["treatment"]["items"][0]["preset_id"] is None
    assert r["treatment"]["food_name"] == "Toast"  # no name -> the first food


@pytest.mark.parametrize("body, message", [
    ({"items": []}, "Add at least one food."),
    ({"items": [{"name": ""}]}, "Add at least one food."),
    ({"items": [{"name": "Mystery", "carbs": None}]}, "Every food needs a carb amount."),
    ({"items": [{"name": "Toast", "carbs": 15}], "timestamp": "yesterday-ish"}, "A valid time is required."),
])
def test_log_meal_validates(database, body, message):
    r = food.log_meal(body)
    assert r["_status"] == 400 and r["error"] == message


def test_logged_meals_can_be_deleted_like_any_food_log(database):
    t = food.log_meal({"items": [{"name": "Toast", "carbs": 15}]})["treatment"]
    assert asyncio.run(food.handle({"action": "delete_log", "id": t["id"]}))["ok"]
    assert food.recent_logs() == []


# Late afternoon, when "330" can only mean 3:30 p.m.
LATE = datetime(2026, 10, 16, 16, 35, tzinfo=ZoneInfo("America/New_York"))


def _at(day, hour, minute):
    return datetime(2026, 10, day, hour, minute, tzinfo=LATE.tzinfo)


@pytest.mark.parametrize("said, expected, rest", [
    ("a bag of chips at 330", _at(16, 15, 30), "a bag of chips"),
    ("a bag of chips at 3 30", _at(16, 15, 30), "a bag of chips"),
    ("a bag of chips at 3:30", _at(16, 15, 30), "a bag of chips"),
    ("a bag of chips at 3.30.", _at(16, 15, 30), "a bag of chips."),
    ("Yogurt with berries at 1.20 p.m.", _at(16, 13, 20), "Yogurt with berries"),
    ("rice at 12.15 and a pear", _at(16, 12, 15), "rice and a pear"),
    ("a muffin 10.05am", _at(16, 10, 5), "a muffin"),
    ("Toast, 8.45 a.m.", _at(16, 8, 45), "Toast"),
    ("pasta at 1430", _at(16, 14, 30), "pasta"),
    ("at 9 o'clock a bagel", _at(16, 9, 0), "a bagel"),
    ("two eggs at around 7 AM", _at(16, 7, 0), "two eggs"),
    ("oatmeal at 4:50", _at(16, 4, 50), "oatmeal"),  # 4:50 p.m. is still ahead -> this morning
    ("a burger at 5 p.m.", _at(15, 17, 0), "a burger"),  # later than now -> yesterday
    ("popcorn at 1030 p.m.", _at(15, 22, 30), "popcorn"),
    ("an apple at 4 45", _at(16, 16, 45), "an apple"),  # a few minutes ahead is still today
    # A bare hour that ends the sentence or clause.
    ("Chicken broccoli bowl with rice at 12.", _at(16, 12, 0), "Chicken broccoli bowl with rice."),
    ("Chicken with crackers at 11.", _at(16, 11, 0), "Chicken with crackers."),
    ("a sandwich at 9", _at(16, 9, 0), "a sandwich"),
    ("toast at 7, then coffee", _at(16, 7, 0), "toast, then coffee"),
])
def test_parse_spoken_time(said, expected, rest):
    assert food.parse_spoken_time(said, LATE) == (expected, rest)


def test_a_bare_spoken_time_is_the_most_recent_past_one():
    morning = datetime(2026, 10, 16, 9, 0, tzinfo=LATE.tzinfo)
    assert food.parse_spoken_time("cereal at 830", morning)[0] == _at(16, 8, 30)
    assert food.parse_spoken_time("pizza at 930", morning)[0] == _at(15, 21, 30)
    assert food.parse_spoken_time("a snack at 1205", morning)[0] == _at(16, 0, 5)
    assert food.parse_spoken_time("a snack at 1205 p.m.", morning)[0] == _at(15, 12, 5)


@pytest.mark.parametrize("said", [
    "a bag of chips",
    "burgers at 5 Guys",  # a bare hour mid-sentence is left to the model
    "cookies, about 2.",
    "a granola bar at 2 for one",
    "rice at 20.",
    "1.5 cups of rice",
    "about 150 grams of rice",
    "one bag at 140 calories",
    "2 slices of bread and 12 almonds",
    "a 330 ml can of soda",
    "at 3 30 g bags of pretzels",
    "I am hungry, had a banana",
    "coffee at 25:00",
    "a granola bar for $2.50",
])
def test_parse_spoken_time_leaves_other_numbers_alone(said):
    assert food.parse_spoken_time(said, LATE) == (None, said)


@pytest.mark.parametrize("said", [" " * 20000 + "!", " ," * 10000, "1 " * 10000, "at 1 " * 5000, "1" + " " * 20000 + "g", "chips at" + " " * 20000 + "330 ."])
def test_parse_spoken_time_stays_fast_on_long_runs(said):
    start = time.perf_counter()
    food.parse_spoken_time(said, LATE)
    assert time.perf_counter() - start < 1


def test_parse_spoken_time_tidies_the_words_left_behind():
    assert food.parse_spoken_time("chips   at 330 ,  and   a soda .", LATE) == (_at(16, 15, 30), "chips, and a soda.")


def test_estimate_reads_the_spoken_time_itself_and_hides_it_from_the_model(database, monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return LATE.astimezone(tz)

    prompts = []

    async def fake_invoke(prompt, **kwargs):
        prompts.append(prompt)
        return {**SANDWICH, "eaten_at": "13:00"}  # the model's misreading loses to the parse

    monkeypatch.setattr(food, "datetime", FixedDatetime)
    monkeypatch.setattr(food, "invoke_llm", fake_invoke)
    r = asyncio.run(food.estimate_meal({"text": "a bag of chips at 330", "tz": "America/New_York"}))
    assert 'What they said: "a bag of chips"' in prompts[0]
    assert all("330" not in p for p in prompts)  # neither the estimate nor the USDA match sees the time
    assert r["transcript"] == "a bag of chips at 330"
    assert r["timestamp"] == "2026-10-16T19:30:00.000Z"

    r = asyncio.run(food.estimate_meal({"text": "a bag of chips", "tz": "America/New_York"}))
    assert r["timestamp"] == "2026-10-16T17:00:00.000Z"  # no time said -> the model's eaten_at
