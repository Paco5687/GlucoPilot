"""Food logging: barcode normalization, label extraction, presets, and logs."""

import asyncio

import pytest

from server import db, food
from server.migrations import run_migrations


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "data" / "app.sqlite3"
    path.parent.mkdir()
    run_migrations(path)
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


NUTELLA = {
    "product_name": "Nutella", "brands": "Ferrero", "serving_size": "1 tbsp (15 g)", "serving_quantity": 15,
    "nutriments": {"carbohydrates_100g": 57.5, "sugars_100g": 56.3, "proteins_100g": 6.3, "fat_100g": 30.9, "energy-kcal_100g": 539},
}


def test_off_product_scales_per_100g_to_the_stated_serving():
    p = food.normalize_off_product(NUTELLA)
    assert p["name"] == "Nutella" and p["brand"] == "Ferrero"
    assert p["serving_label"] == "1 tbsp (15 g)" and p["serving_grams"] == 15
    assert p["carbs"] == 8.6 and p["sugars"] == 8.4 and p["calories"] == pytest.approx(80.85, abs=0.1)
    assert p["fiber"] is None  # not reported -> never invented


def test_off_product_prefers_direct_per_serving_values():
    product = {**NUTELLA, "nutriments": {**NUTELLA["nutriments"], "carbohydrates_serving": 9.0}}
    assert food.normalize_off_product(product)["carbs"] == 9.0


def test_off_product_without_serving_falls_back_to_100g():
    product = {"product_name": "Rice", "nutriments": {"carbohydrates_100g": 28.0}}
    p = food.normalize_off_product(product)
    assert p["serving_label"] == "100 g" and p["carbs"] == 28.0


def test_off_product_without_carbs_is_rejected():
    assert food.normalize_off_product({"product_name": "Water", "nutriments": {}}) is None


def test_barcode_lookup_respects_the_setting(database, monkeypatch):
    async def _run():
        monkeypatch.setattr(db, "config_value", lambda name, default="": "false" if name == "food_barcode_lookup" else default)
        result = await food.lookup_barcode("3017624010701")
        assert result["_status"] == 403
    asyncio.run(_run())


def test_label_scan_maps_model_output_and_rejects_no_carbs(monkeypatch):
    async def _run():
        async def fake_invoke(prompt, response_json_schema=None, max_tokens=0, images=None, site=""):
            assert site == "nutrition_label" and images == ["image/jpeg|abc"]
            return {"name": "Oat bar", "brand": None, "serving_label": "1 bar (40 g)", "calories": 190,
                    "carbs": 27, "fiber": 3, "sugars": 9, "protein": 4, "fat": 7, "confidence": 0.9}
        monkeypatch.setattr(food, "invoke_llm", fake_invoke)
        result = await food.scan_label("image/jpeg|abc")
        assert result["ok"] and result["label"]["carbs"] == 27 and result["label"]["serving_label"] == "1 bar (40 g)"

        async def no_carbs(*args, **kwargs):
            return {"serving_label": None, "calories": None, "carbs": None, "fiber": None, "sugars": None, "protein": None, "fat": None, "confidence": 0.2}
        monkeypatch.setattr(food, "invoke_llm", no_carbs)
        assert (await food.scan_label("image/jpeg|abc"))["_status"] == 422
    asyncio.run(_run())


def test_preset_log_creates_a_carb_treatment_with_nutrition_and_bumps_use(database):
    async def _run():
        saved = await food.handle({"action": "save_preset", "name": "Oat bar", "serving_label": "1 bar",
                                   "carbs": 27, "fiber": 3, "protein": 4, "fat": 7, "calories": 190, "source": "label"})
        preset = saved["preset"]
        logged = await food.handle({"action": "log", "preset_id": preset["id"], "servings": 1.5,
                                    "timestamp": "2026-09-23T12:00:00.000Z"})
        t = logged["treatment"]
        assert t["type"] == "carb" and t["event_type"] == "Carbs" and t["source"] == "manual"
        assert t["amount"] == 40.5 and t["protein_g"] == 6.0 and t["fiber_g"] == 4.5 and t["calories"] == 285.0
        assert t["food_name"] == "Oat bar" and t["servings"] == 1.5 and t["preset_id"] == preset["id"]
        assert "Oat bar × 1.5 1 bar" == t["notes"]

        listing = await food.handle({"action": "presets"})
        assert listing["presets"][0]["use_count"] == 1
        assert listing["recent"][0]["id"] == t["id"]

        # Only manual food logs are deletable here; pump-reported carbs are not.
        assert (await food.handle({"action": "delete_log", "id": t["id"]}))["ok"]
        assert (await food.handle({"action": "presets"}))["recent"] == []
    asyncio.run(_run())


def test_log_without_carbs_or_with_bad_time_is_rejected(database):
    async def _run():
        assert (await food.handle({"action": "log", "name": "Mystery"}))["_status"] == 400
        assert (await food.handle({"action": "log", "name": "X", "carbs": 10, "timestamp": "nope"}))["_status"] == 400
    asyncio.run(_run())

