"""On-box nutrition table for the voice food log.

The local model is good at hearing a meal as separate foods with amounts and
poor at remembering what those foods contain, so the numbers come from here
whenever a food and its portion can be pinned down: a compact subset of USDA
FoodData Central (FNDDS survey foods plus packaged, fast-food and restaurant
entries from SR Legacy; public domain), bundled with the app and searched
in-process. Food words never leave the machine for a lookup.

``candidates`` finds the closest foods by weighted word overlap. A food said by
its own USDA name is taken as is (``named_match``); otherwise the caller lets
the model pick among them, since word overlap can't tell "spaghetti with meat
sauce" from "Spaghetti sauce with meat". ``portion_grams`` turns the spoken
amount into grams with USDA's own household measures for that food, and
``nutrition`` scales the per-100 g values.

``scripts/build_usda_foods.py`` regenerates ``usda_foods.json.gz``.
"""

from __future__ import annotations

import gzip
import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from itertools import permutations
from pathlib import Path
from typing import Any

TABLE_PATH = Path(__file__).with_name("usda_foods.json.gz")
MACROS = ("carbs", "fiber", "sugars", "protein", "fat", "calories")

# Share of the query's (weighted) words a food must carry to be offered as a
# candidate at all, and to be used without the model confirming it.
CANDIDATE_COVERAGE = 0.6
MIN_COVERAGE = 0.8
QUALIFIER_WEIGHT = 0.25   # "prepared", "home recipe", "100%" ...
EXTRA_WEIGHT = 0.5        # per unit of description the query didn't ask for
SR_PENALTY = 1.5          # FNDDS (foods as eaten) wins near-ties over SR Legacy


def _stem(word: str) -> str:
    """Fold plurals, -ed, and y/ie/e endings so "cookies" meets "cookie",
    "fries" meets "fry" and "toasted" meets "toast". Applied to both sides, so
    the stems need not be words."""
    if len(word) > 4 and word.endswith("ies"):
        word = word[:-3] + "i"
    elif len(word) > 4 and word.endswith(("ches", "shes", "sses", "xes", "oes")):
        word = word[:-2]
    elif len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    if len(word) > 4 and word.endswith("ied"):
        word = word[:-3] + "i"
    elif len(word) > 5 and word.endswith("ed") and not word.endswith("eed"):
        word = word[:-2]
    if len(word) > 3 and word.endswith("ie"):
        word = word[:-1]
    elif len(word) > 3 and word.endswith("y"):
        word = word[:-1] + "i"
    elif len(word) > 3 and word.endswith("e"):
        word = word[:-1]
    return word


def _stems(words: str) -> frozenset[str]:
    return frozenset(_stem(w) for w in words.split())


_STOPWORDS = frozenset(
    "a an and as at by for from in into of on or the to with w ns nfs specified type kind any "
    "some my their his her".split()
)
# Words that qualify a food without naming it. They count for little either
# way, so "Pasta with tomato-based sauce and meat, home recipe" still reaches a
# plain "spaghetti with meat sauce".
_QUALIFIERS = _stems(
    "prepared commercially commercial regular plain home recipe homemade made cooked fresh raw added no salt "
    "include includes based ready eat heat style other than all variety brand restaurant fast food store bought "
    "bottled packaged enriched unenriched"
)
_SYNONYMS = {"gf": "gluten free", "oj": "orange juice", "pb": "peanut butter", "bbq": "barbecue",
             "soda": "soft drink"}
# Spoken and USDA spellings of a measure, folded to one word before stemming.
_MEASURE_WORDS = {
    "grams": "g", "gram": "g", "gm": "g", "ounces": "oz", "ounce": "oz", "pounds": "lb", "pound": "lb", "lbs": "lb",
    "kilograms": "kg", "tablespoons": "tbsp", "tablespoon": "tbsp", "tbs": "tbsp", "teaspoons": "tsp",
    "teaspoon": "tsp", "milliliters": "ml", "millilitres": "ml", "liters": "liter", "litres": "liter",
}

_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "half": 0.5, "quarter": 0.25, "couple": 2, "few": 3,
}
_UNICODE_FRACTIONS = {"½": ".5", "¼": ".25", "¾": ".75", "⅓": ".333", "⅔": ".667"}

# Measures that convert without the food's own portion list. Volumes are in
# cups and become grams through the food's USDA cup (or tbsp / fl oz) weight.
# (These keys are already their own stems.)
_WEIGHT_GRAMS = {"g": 1.0, "kg": 1000.0, "oz": 28.35, "lb": 453.6}
_VOLUME_CUPS = {"cup": 1.0, "tbsp": 1 / 16, "tsp": 1 / 48, "floz": 1 / 8, "ml": 1 / 236.6, "liter": 4.227,
                "pint": 2.0, "quart": 4.0,
                # Everyday vessels, at their usual size.
                "glass": 1.0, "mug": 1.0, "bowl": 1.5}
_CONTAINER_UNITS = _stems("bag package packet pack pouch box carton tub")
_DRINK_VESSELS = _stems("can bottle glass cup carton mug drink")
_SLICE_UNITS = _stems("slice piece")
_COUNT_UNITS = _stems("each piece item whole serving unit portion one")
_SERVING_UNITS = _stems("serving portion")
_SIZES = _stems("small medium large regular extra jumbo mini miniature big thin thick")
# Sizes as USDA measures put them ("1 baby potato", "1 new potato"), unless the
# food is named by them (baby carrots).
_MEASURE_SIZES = _stems("baby new")
_DEFAULT_SIZES = _stems("medium regular")
# Portion descriptions that are never what someone means by a plain count.
_ODD_PORTIONS = _stems("crust yields raw melted cubic linear surface guideline calorie snack bite miniature mini "
                       "kid child dipping cracker cooked dry")
_GENERIC_PORTION = re.compile(r"\bNFS\b|\bNS as to size\b|any size", re.IGNORECASE)
_SINGLE_SERVING = re.compile(r"\bsingle[\s-]+serving", re.IGNORECASE)  # a bag's size, not a serving


def _fold_measures(text: str) -> str:
    text = re.sub(r"\bfl(?:uid)?\.?\s*(?:oz|ounces?)\b", " floz ", text, flags=re.IGNORECASE)
    return re.sub(r"[A-Za-z]+", lambda m: _MEASURE_WORDS.get(m.group(0).lower(), m.group(0)), text)


_NEGATIONS = frozenset({"without", "no", "not"})


def tokens(text: Any) -> list[str]:
    """Stemmed meaningful words. What follows "without" / "no" / "not" up to
    the next comma is absent, so it isn't counted: "Chicken breast, grilled
    without sauce" carries no sauce, "Rice, white, cooked, no added fat" no fat."""
    words = []
    for segment in re.split(r"[,;()]", str(text or "").lower()):
        for word in re.findall(r"[a-z0-9]+", segment):
            if word in _NEGATIONS:
                break
            for part in _SYNONYMS.get(word, word).split():
                if part not in _STOPWORDS:
                    words.append(_stem(part))
    return words


@dataclass(frozen=True)
class Food:
    fdc_id: int
    dataset: str
    name: str
    per_100g: tuple[float | None, ...]
    portions: tuple[tuple[str, float], ...]
    words: frozenset[str]
    alias_words: frozenset[str]
    aliases: tuple[tuple[str, frozenset[str]], ...]  # USDA's additional descriptions, with their words
    names: tuple[tuple[str, ...], ...]  # curated common names ("yogurt bites"), as word sequences


class _Table:
    def __init__(self, foods: list[Food]):
        self.foods = foods
        self.by_id = {f.fdc_id: f for f in foods}
        self.postings: dict[str, list[Food]] = {}
        for food in foods:
            for word in food.words | food.alias_words:
                self.postings.setdefault(word, []).append(food)
        n = len(foods)
        self.idf = {w: math.log(1 + n / len(fs)) for w, fs in self.postings.items()}
        self.unknown_idf = math.log(1 + n)

    def weight(self, word: str, asked: bool = False) -> float:
        """How telling a word is. A number ("2" in "2% milk") counts fully when
        asked for and barely as a description's extra ("100%")."""
        qualifier = word in _QUALIFIERS or (word.isdigit() and not asked)
        return self.idf.get(word, self.unknown_idf) * (QUALIFIER_WEIGHT if qualifier else 1.0)


@lru_cache(maxsize=1)
def table() -> _Table:
    with gzip.open(TABLE_PATH, "rt", encoding="utf-8") as f:
        raw = json.load(f)
    foods = []
    for fdc_id, dataset, name, aliases, per100, portions, names in raw["foods"]:
        known = tuple(tuple(tokens(n)) for n in names)
        words = frozenset(tokens(name))
        others = tuple((a.strip(), frozenset(tokens(a))) for a in [*aliases.split(";"), *names] if a.strip())
        foods.append(Food(fdc_id, dataset, name, tuple(per100), tuple((d, g) for d, g in portions),
                          words, frozenset(w for _, ws in others for w in ws) - words, others, known))
    return _Table(foods)


def is_food_word(word: str) -> bool:
    """Whether the word appears in some USDA food's name or descriptions — an
    everyday food word rather than a brand or product name."""
    stems = tokens(word)
    return bool(stems) and all(s in table().postings for s in stems)


@dataclass(frozen=True)
class Match:
    food: Food
    coverage: float
    score: float
    also: tuple[str, ...] = ()  # the food's other names that share a word with the query


def _scored(query: str) -> list[Match]:
    """Every food carrying enough of the query, best first: the most of the
    query covered for the least description it didn't ask for, so "blueberry
    muffin" ranks a plain fruit muffin above a low-fat or toaster one."""
    t = table()
    wanted = list(dict.fromkeys(tokens(query)))
    total = sum(t.weight(w, asked=True) for w in wanted)
    if total <= 0:
        return []
    asked = frozenset(wanted)
    pool: dict[int, Food] = {}
    for word in wanted:
        for food in t.postings.get(word, ()):
            pool[food.fdc_id] = food
    matches = []
    for food in pool.values():
        if any(frozenset(name) == asked for name in food.names):
            # Said exactly as one of the food's common names ("yogurt bites").
            hit, extra = total, 0.0
        else:
            # USDA's additional descriptions count as fully as the name
            # ("spaghetti" on "Pasta with tomato-based sauce and meat").
            hit = sum(t.weight(w, asked=True) for w in wanted if w in food.words or w in food.alias_words)
            extra = sum(t.weight(w) for w in food.words if w not in asked)
        coverage = hit / total
        if coverage < CANDIDATE_COVERAGE:
            continue
        score = hit - EXTRA_WEIGHT * extra - (SR_PENALTY if food.dataset != "fndds" else 0.0)
        also = tuple(text for text, words in food.aliases if words & asked and not words <= food.words)[:2]
        matches.append(Match(food, round(coverage, 3), round(score, 3), also))
    matches.sort(key=lambda m: (-m.score, len(m.food.name)))
    return matches


def _core(food: Food) -> frozenset[str]:
    return frozenset(w for w in food.words if w not in _QUALIFIERS and not w.isdigit())


def candidates(query: str, limit: int = 8, portion: Any = None) -> list[Match]:
    """A short, varied list for the model to choose from: the best-scoring
    foods interleaved with the ones covering the most words, one per dish
    (see ``_same_dish``). Word overlap alone can't see that "spaghetti with meat
    sauce" is a pasta dish rather than "Spaghetti sauce with meat". With a
    portion, only foods whose USDA measures can express it are offered — an
    entry listing just "1 serving" can't say what "1 bowl" weighs."""
    scored = _scored(query)
    if portion is not None:
        spoken = parse_portion(portion)
        scored = [m for m in scored if _grams(m.food, spoken) is not None]
    by_cover = sorted(scored, key=lambda m: (-round(m.coverage, 1), -m.score))
    picked: dict[int, Match] = {}
    for pair in zip(scored, by_cover):
        for m in pair:
            if len(picked) >= limit or m.food.fdc_id in picked:
                continue
            if not any(_same_dish(m.food, p.food) for p in picked.values()):
                picked[m.food.fdc_id] = m
    return sorted(picked.values(), key=lambda m: (-m.score, len(m.food.name)))


def _same_dish(a: Food, b: Food) -> bool:
    """Versions of one dish that differ only in qualifiers and barely in carbs
    (restaurant vs home recipe), offered once. Fast-food fries carry nearly
    twice the carbs per 100 g of oven fries, so those stay apart."""
    if _core(a) != _core(b):
        return False
    ca, cb = a.per_100g[0] or 0.0, b.per_100g[0] or 0.0
    return abs(ca - cb) <= 0.15 * max(ca, cb, 1.0)


def _said_as_named(query: list[str], food: Food) -> bool:
    """Whether the words are this food's own name: USDA's description with its
    comma-separated parts in any order ("Swiss cheese" is "Cheese, Swiss") and
    bare qualifiers dropped ("Banana, raw", "Cheeseburger, NFS"), or one of its
    curated common names ("yogurt bites"). Word order inside a part counts,
    so "spaghetti with meat sauce" is not "Spaghetti sauce with meat"."""
    if tuple(query) in food.names:
        return True
    parts = [p for p in (tokens(part) for part in re.split(r"[,()]", food.name))
             if p and not all(w in _QUALIFIERS or w.isdigit() for w in p)]
    return 0 < len(parts) <= 4 and any(sum(order, []) == query for order in permutations(parts))


def named_match(query: str, matches: list[Match]) -> Match | None:
    """The best candidate whose own name is exactly what was said, if any —
    no judgement call is needed for it."""
    said = tokens(query)
    return next((m for m in matches if _said_as_named(said, m.food)), None)


def best_match(query: str, portion: Any = None) -> Match | None:
    """The top candidate when it alone carries the query — used when the model
    can't be asked to choose."""
    found = candidates(query, 1, portion)
    return found[0] if found and found[0].coverage >= MIN_COVERAGE else None


# ── portions ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Portion:
    quantity: float
    unit: str | None            # stemmed: "slic", "cup", "bag", "floz", "g", or None
    size: str | None            # stemmed: "small", "medium", "larg" ...
    stated_grams: float | None  # an explicit weight, e.g. "(28 g)" or "3 oz"


def _number(text: str) -> float | None:
    text = text.strip().lower()
    for glyph, decimal in _UNICODE_FRACTIONS.items():
        text = re.sub(rf"(\d)\s*{glyph}", lambda m: str(int(m.group(1)) + float(decimal)), text)
        text = text.replace(glyph, "0" + decimal)
    mixed = re.fullmatch(r"(\d+)[\s-]+(\d+)/(\d+)", text)
    if mixed:
        return int(mixed.group(1)) + int(mixed.group(2)) / max(int(mixed.group(3)), 1)
    frac = re.fullmatch(r"(\d+)/(\d+)", text)
    if frac:
        return int(frac.group(1)) / max(int(frac.group(2)), 1)
    try:
        return float(text)
    except ValueError:
        return _NUMBER_WORDS.get(text)


_AMOUNT = r"(?:\d+[\s-]+)?\d+/\d+|\d*\.?\d+\s*[½¼¾⅓⅔]?|[½¼¾⅓⅔]"
_LEADING_AMOUNT = re.compile(rf"^\s*({_AMOUNT}|[a-z]+)(?=\s|$)\s*(?:of\s+)?(?:an?\s+)?", re.IGNORECASE)
_STATED_WEIGHT = re.compile(r"(\d*\.?\d+)\s*(g|grams?|gm|oz|ounces?|lbs?|pounds?|kg)\b", re.IGNORECASE)


def parse_portion(text: Any) -> Portion:
    """ "2 slices" -> 2 × slice; "1 bag (28 g)" -> 1 × bag stating 28 g;
    "half a cup" -> 0.5 × cup; "3 oz" -> 3 oz; "1 large" -> 1, size large."""
    raw = _fold_measures(" ".join(str(text or "").split()))
    stated = None
    weight = _STATED_WEIGHT.search(raw)
    if weight:
        unit = _MEASURE_WORDS.get(weight.group(2).lower(), weight.group(2).lower())
        stated = round(float(weight.group(1)) * _WEIGHT_GRAMS.get(_stem(unit), 1.0), 1)
    head = re.sub(r"\(.*?\)", " ", raw).strip()
    quantity = 1.0
    match = _LEADING_AMOUNT.match(head)
    if match and _number(match.group(1)) is not None:
        quantity = _number(match.group(1)) or 1.0
        head = head[match.end():]
    words = tokens(head)
    size = next((w for w in words if w in _SIZES), None)
    rest = [w for w in words if w not in _SIZES]
    unit = rest[0] if rest else None
    if unit == "oz" and set(rest[1:]) & _DRINK_VESSELS:
        unit, stated = "floz", None  # "a 12 oz can" is fluid ounces
    elif unit in _WEIGHT_GRAMS:
        stated = round(quantity * _WEIGHT_GRAMS[unit], 1)
    return Portion(max(quantity, 0.0), unit, size, stated)


def _portion_parts(desc: str) -> tuple[float, list[str]]:
    """A USDA portion description -> (amount, stemmed words)."""
    head = _fold_measures(re.sub(r"\(.*?\)", " ", desc))
    amount = 1.0
    match = re.match(rf"\s*({_AMOUNT})\s*", head)
    if match:
        amount = _number(match.group(1)) or 1.0
        head = head[match.end():]
    return amount, tokens(head)


def _is_measure(word: str) -> bool:
    return word in _WEIGHT_GRAMS or word in _VOLUME_CUPS or word in ("cubic", "linear", "surface")


def _cup_grams(food: Food) -> float | None:
    """Grams in one cup, from the plainest volume measure USDA lists."""
    best = None
    for desc, grams in food.portions:
        amount, words = _portion_parts(desc)
        if not words or words[0] not in ("cup", "tbsp", "tsp", "floz"):
            continue
        cups = amount * _VOLUME_CUPS[words[0]]
        odd = len([w for w in words[1:] if w not in _QUALIFIERS])
        rank = (0 if words[0] == "cup" else 1, odd)
        if best is None or rank < best[0]:
            best = (rank, grams / cups)
    return best[1] if best else None


def _says_serving(desc: str) -> bool:
    """A USDA measure that is a serving itself, not a single-serving bag."""
    return bool(set(_portion_parts(desc)[1]) & _SERVING_UNITS) and not _SINGLE_SERVING.search(desc)


def _typical(food: Food) -> float | None:
    """USDA's "Quantity not specified": the amount people typically report."""
    return next((g for d, g in food.portions if d.lower() == "quantity not specified"), None)


def _count_portion(food: Food, portion: Portion) -> tuple[str, float] | None:
    """The one-item USDA portion meant by "2 slices", "1 large", "a muffin" or
    "1 bag": the one naming that unit, at the stated size, else the generic
    (NFS) or medium/regular one, avoiding odd cuts (crust not eaten,
    snack-size, raw yields). Returns (USDA description, grams for one)."""
    unit = portion.unit
    if unit in _CONTAINER_UNITS:
        family = _CONTAINER_UNITS
    elif unit in _SLICE_UNITS:
        family = _SLICE_UNITS
    else:
        family = {unit} if unit else set()
    # "1 muffin", "1 serving", or no unit at all: one of the food itself.
    as_count = unit is None or unit in _COUNT_UNITS or unit in food.words or unit in food.alias_words
    # An odd word isn't odd when it was said: "10 crackers", "1 mini muffin".
    said = {unit, portion.size}
    best = None
    for desc, grams in food.portions:
        if desc.lower() == "quantity not specified":
            continue
        amount, words = _portion_parts(desc)
        if not words or _is_measure(words[0]):
            continue
        wordset = set(words)
        named = bool(wordset & family)
        sizes = wordset & _SIZES | (wordset & _MEASURE_SIZES) - food.words - said
        generic = bool(_GENERIC_PORTION.search(desc))
        # A bag of crackers is not one cracker: when the food itself is being
        # counted, a container is used only if nothing else fits, or if it is
        # the size asked for ("1 small" chips is the small bag).
        last_resort = (as_count and not named and bool(wordset & _CONTAINER_UNITS)
                       and not (portion.size and portion.size in sizes))
        if named and not as_count:
            score = 4.0
        elif as_count and (named or sizes or generic or wordset & food.words):
            score = 1.0 if named or wordset & food.words else 0.0
        else:
            continue
        if portion.size:
            score += 3 if portion.size in sizes else (-2 if sizes else 0)
        else:
            score += 2.5 if generic else (2 if sizes & _DEFAULT_SIZES else (-1 if sizes else 0))
        score -= 2 * len((wordset & _ODD_PORTIONS) - said)
        rank = (last_resort, -score, len(words))
        if score > 0 and (best is None or rank < best[0]):
            best = (rank, desc, grams / amount)
    return (best[1], best[2]) if best else None


def portion_grams(food: Food, text: Any) -> tuple[float, str] | None:
    """Grams eaten for a spoken portion, from USDA's own household measures
    for this food: (grams, the measure used). None when the portion can't be
    pinned down — a unit USDA doesn't list for the food and no stated weight."""
    return _grams(food, parse_portion(text))


def _grams(food: Food, portion: Portion) -> tuple[float, str] | None:
    qty = portion.quantity or 1.0
    unit = portion.unit
    if unit in _WEIGHT_GRAMS:
        return round(qty * _WEIGHT_GRAMS[unit], 1), f"{qty:g} {unit}"
    if unit in _VOLUME_CUPS:
        per_cup = _cup_grams(food)
        if per_cup:
            cups = qty * _VOLUME_CUPS[unit]
            return round(cups * per_cup, 1), f"{cups:g} cup"
    else:
        found = _count_portion(food, portion)
        if found and unit in _SERVING_UNITS and _typical(food) and not _says_serving(found[0]):
            # "A serving" of crackers is the amount people usually eat, not
            # one cracker; USDA's own "1 serving" measure still wins.
            found = None
        if found:
            return round(qty * found[1], 1), f"{qty:g} × {found[0]}"
        if unit is None or unit in _COUNT_UNITS or unit in food.words:
            typical = _typical(food)
            if typical:
                return round(qty * typical, 1), f"{qty:g} × typical serving"
    if portion.stated_grams:
        return portion.stated_grams, "stated weight"
    return None


def nutrition(food: Food, grams: float) -> dict[str, float | None]:
    return {m: (round(v * grams / 100, 1) if v is not None else None) for m, v in zip(MACROS, food.per_100g)}


def ground(food: Food, portion: Any) -> dict[str, Any] | None:
    """USDA-backed nutrition for a chosen food and spoken portion, or None when
    the amount can't be pinned down."""
    sized = portion_grams(food, portion)
    if sized is None or sized[0] <= 0:
        return None
    grams, basis = sized
    return {"fdc_id": food.fdc_id, "fdc_name": food.name, "grams": grams, "basis": basis, **nutrition(food, grams)}
