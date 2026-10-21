"""Voice food logging: on-box transcription, the per-food estimate, preset
reuse, spoken times, and logging a described meal as one carb Treatment."""

import asyncio
import base64
import io
import math
import struct
import time
import wave
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from server import db, food, speech
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


def test_estimate_from_text_returns_items_and_totals(database, monkeypatch):
    seen = {}

    async def fake_invoke(prompt, response_json_schema=None, max_tokens=0, images=None, site=""):
        seen.update(prompt=prompt, site=site, schema=response_json_schema, images=images)
        return SANDWICH

    monkeypatch.setattr(food, "invoke_llm", fake_invoke)
    r = asyncio.run(food.handle({"action": "estimate_meal", "text": "chicken sandwich on gluten free bread with swiss cheese"}))
    assert r["ok"] and r["transcript"] == "chicken sandwich on gluten free bread with swiss cheese"
    assert seen["site"] == "food_estimate" and seen["schema"] is food.MEAL_SCHEMA and not seen["images"]
    assert "chicken sandwich on gluten free bread" in seen["prompt"] and "Their saved foods" not in seen["prompt"]
    meal = r["meal"]
    assert meal["title"] == "Chicken sandwich on GF bread" and len(meal["items"]) == 3
    assert meal["totals"]["carbs"] == 34.4 and meal["totals"]["protein"] == 38 and meal["totals"]["fiber"] == 4
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
    assert item["preset_id"] == bites["id"] and item["servings"] == 2
    assert item["name"] == "Yogurt Bites" and item["portion"] == "2 × 1 bag (28 g)"
    assert item["carbs"] == 42 and item["protein"] == 6 and item["calories"] == 220
    assert item["sugars"] is None  # the preset has none -> the model's guess is not kept


def test_estimate_ignores_a_preset_id_it_was_not_offered(database, monkeypatch):
    async def fake_invoke(prompt, **kwargs):
        return {**SANDWICH, "items": [{**SANDWICH["items"][0], "preset_id": "made-up", "servings": 3}]}

    monkeypatch.setattr(food, "invoke_llm", fake_invoke)
    item = asyncio.run(food.estimate_meal({"text": "toast"}))["meal"]["items"][0]
    assert item["preset_id"] is None and item["servings"] is None and item["carbs"] == 34


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
    "a sandwich at 9",  # a bare hour is left to the model
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

    seen = {}

    async def fake_invoke(prompt, **kwargs):
        seen["prompt"] = prompt
        return {**SANDWICH, "eaten_at": "13:00"}  # the model's misreading loses to the parse

    monkeypatch.setattr(food, "datetime", FixedDatetime)
    monkeypatch.setattr(food, "invoke_llm", fake_invoke)
    r = asyncio.run(food.estimate_meal({"text": "a bag of chips at 330", "tz": "America/New_York"}))
    assert 'What they said: "a bag of chips"' in seen["prompt"] and "330" not in seen["prompt"]
    assert r["transcript"] == "a bag of chips at 330"
    assert r["timestamp"] == "2026-10-16T19:30:00.000Z"

    r = asyncio.run(food.estimate_meal({"text": "a bag of chips", "tz": "America/New_York"}))
    assert r["timestamp"] == "2026-10-16T17:00:00.000Z"  # no time said -> the model's eaten_at
