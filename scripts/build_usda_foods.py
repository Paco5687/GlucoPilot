"""Build server/usda_foods.json.gz, the on-box nutrition table the voice food
log grounds its estimates in.

Source: USDA FoodData Central (public domain, CC0 1.0), downloaded once from
https://fdc.nal.usda.gov/download-datasets/ and unzipped:

  - FNDDS 2021-2023 survey foods, CSV release 2024-10-31
    (FoodData_Central_survey_food_csv_2024-10-31.zip): foods as eaten, with
    household portions and a "Quantity not specified" typical amount.
  - SR Legacy, CSV release 2018-04 (FoodData_Central_sr_legacy_food_csv_2018-04.zip):
    only the categories that add packaged, branded, restaurant, and fast foods
    the survey set lacks.

Usage:
  python3 scripts/build_usda_foods.py <fndds_csv_dir> <sr_legacy_csv_dir>

Each food becomes [fdc_id, dataset, description, aliases, per_100g, portions,
names]: aliases are USDA's additional descriptions ("blueberry" on "Muffin,
fruit", but also toppings and examples), per_100g is [carbs, fiber, sugars,
protein, fat, calories] (grams, kcal; null when USDA has no value), portions is
[[description, grams], ...], and names are the curated common names below.
"""

from __future__ import annotations

import csv
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "server" / "usda_foods.json.gz"

# Nutrients in the table's macro order. SR Legacy's food_nutrient rows use FDC
# nutrient ids (1005), FNDDS's use the older nutrient numbers (205); either
# matches. Carbohydrate falls back from "by difference" to "by summation".
NUTRIENTS = {"carbs": ("1005", "205", "1050"), "fiber": ("1079", "291"), "sugars": ("2000", "269"),
             "protein": ("1003", "203"), "fat": ("1004", "204"), "calories": ("1008", "208")}
WANTED = {nid for ids in NUTRIENTS.values() for nid in ids}

# SR Legacy categories kept: the ones carrying brand-name, packaged, fast-food
# and restaurant items. Raw cuts, spices and ingredients are already covered
# as-eaten by FNDDS and would only add noise to matching.
SR_CATEGORIES = {
    "3",   # Baby Foods
    "8",   # Breakfast Cereals
    "18",  # Baked Products
    "19",  # Sweets
    "21",  # Fast Foods
    "22",  # Meals, Entrees, and Side Dishes
    "23",  # Snacks
    "25",  # Restaurant Foods
}

# A few common names and package sizes USDA doesn't record, so everyday
# phrasing lands on the right entry. Keyed by FDC id; a name said exactly
# selects the food, portions are appended to USDA's.
CURATED: dict[int, dict] = {
    173520: {  # Babyfood, snack, GERBER, GRADUATES, YOGURT MELTS
        "names": ["yogurt bites", "yogurt melts", "freeze-dried yogurt snack"],
        "portions": [["1 bag (1 oz)", 28.0]],
    },
}


def _rows(path: Path):
    with path.open(newline="", encoding="utf-8") as f:
        yield from csv.DictReader(f)


def _nutrients(folder: Path, ids: set[str]) -> dict[str, list[float | None]]:
    raw: dict[str, dict[str, float]] = defaultdict(dict)
    for r in _rows(folder / "food_nutrient.csv"):
        if r["fdc_id"] in ids and r["nutrient_id"] in WANTED and r["amount"] != "":
            raw[r["fdc_id"]][r["nutrient_id"]] = float(r["amount"])
    out = {}
    for fdc_id, values in raw.items():
        per100 = []
        for nids in NUTRIENTS.values():
            found = next((values[n] for n in nids if n in values), None)
            per100.append(round(found, 2) if found is not None else None)
        if per100[0] is not None:
            out[fdc_id] = per100
    return out


def _portion(desc: str, grams: str, portions: list) -> None:
    desc = " ".join(desc.split())
    if not desc or not grams:
        return
    weight = float(grams)
    if weight > 0 and all(p[0] != desc for p in portions):
        portions.append([desc, round(weight, 1)])


def fndds(folder: Path) -> list[list]:
    foods = {r["fdc_id"]: r["description"] for r in _rows(folder / "food.csv") if r["data_type"] == "survey_fndds_food"}
    per100 = _nutrients(folder, set(foods))
    aliases: dict[str, list[str]] = defaultdict(list)
    for r in _rows(folder / "food_attribute.csv"):
        if r["food_attribute_type_id"] == "1001" and r["fdc_id"] in foods and r["value"].strip():
            aliases[r["fdc_id"]].append(r["value"].strip())
    portions: dict[str, list] = defaultdict(list)
    for r in sorted(_rows(folder / "food_portion.csv"), key=lambda r: (r["fdc_id"], int(r["seq_num"] or 0))):
        if r["fdc_id"] in foods:
            _portion(r["portion_description"], r["gram_weight"], portions[r["fdc_id"]])
    return [[int(i), "fndds", foods[i], "; ".join(aliases[i]), per100[i], portions[i], []]
            for i in foods if i in per100]


def sr_legacy(folder: Path) -> list[list]:
    foods = {r["fdc_id"]: r["description"] for r in _rows(folder / "food.csv")
             if r["data_type"] == "sr_legacy_food" and r["food_category_id"] in SR_CATEGORIES}
    per100 = _nutrients(folder, set(foods))
    units = {r["id"]: r["name"] for r in _rows(folder / "measure_unit.csv")}
    portions: dict[str, list] = defaultdict(list)
    for r in sorted(_rows(folder / "food_portion.csv"), key=lambda r: (r["fdc_id"], int(r["seq_num"] or 0))):
        if r["fdc_id"] not in foods:
            continue
        unit = units.get(r["measure_unit_id"], "")
        unit = "" if unit in ("undetermined", "") else unit
        amount = r["amount"]
        amount = f"{float(amount):g}" if amount else "1"
        desc = r["portion_description"] or " ".join(p for p in (amount, unit, r["modifier"]) if p)
        _portion(desc, r["gram_weight"], portions[r["fdc_id"]])
    return [[int(i), "sr", foods[i], "", per100[i], portions[i], []] for i in foods if i in per100]


def main(fndds_dir: str, sr_dir: str) -> None:
    foods = fndds(Path(fndds_dir)) + sr_legacy(Path(sr_dir))
    by_id = {f[0]: f for f in foods}
    for fdc_id, extra in CURATED.items():
        food = by_id[fdc_id]
        food[5].extend(extra.get("portions", []))
        food[6].extend(extra.get("names", []))
    table = {
        "source": "USDA FoodData Central: FNDDS 2021-2023 survey foods (2024-10-31) and SR Legacy (2018-04). "
                  "Public domain (CC0 1.0).",
        "macros": list(NUTRIENTS),
        "foods": sorted(foods, key=lambda f: f[0]),
    }
    data = json.dumps(table, separators=(",", ":"), ensure_ascii=False).encode()
    with OUT.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0) as gz:
        gz.write(data)
    print(f"{len(foods)} foods, {len(data) / 1e6:.1f} MB json, {OUT.stat().st_size / 1e6:.2f} MB gz -> {OUT}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
