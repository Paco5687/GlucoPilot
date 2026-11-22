"""Suggest lab evidence for a hypothesis.

Two short questions go to the local model, at temperature 0 so a hypothesis
gets the same answer each time. First: is this usually checked with lab tests,
and how is it usually diagnosed? Second, only when the answer is yes: which lab
tests, which way each usually reads if the hypothesis is true, and whether a
listed condition or medication pushes the test that way by itself. The model
never sees lab values. Code matches the named tests to the tests on file by
analyte (the key the Companion's same-draw matching uses); every value, unit,
date and flag in a suggestion comes from LabResult rows, read the way the
Visit Report reads them. Named tests with no match become "not on file" items.

Nothing is written here: the owner reviews the suggestions and saves the ones
they keep as an ordinary evidence revision.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from . import llm
from .auth import require_admin
from .conditions import get_conditions
from .config import OWNER_EMAIL
from .hypotheses import SqliteHypothesisRepository
from .lab_audit import _HIGH_FLAGS, _LOW_FLAGS, _DIRECTIONLESS_ABNORMAL, effective_flag
from .lab_audit import summary_eligible as lab_summary_eligible
from .lab_names import analyte_key, canonical_lab_name
from .meds import get_medications
from .report import _dedupe_lab_points, _lab_date
from .repositories import get_repositories

log = logging.getLogger("glucopilot.hypothesis_labs")

router = APIRouter()

METHOD = "hypothesis-labs/1.2.0"
# Fixed, PHI-free call-site id. It is not on the Breeze allowlist, so the call
# always stays on the local model.
SITE = "hypothesis_labs"
MAX_TESTS = 12
MAX_NOT_ON_FILE = 6
MAX_ALIASES = 4
EARLIER_SHOWN = 3
DIRECTIONS = ("high", "low", "positive", "negative", "any")
# Reported grades that mean "inside the range" even though they are not the
# word "normal" (functional-medicine panels). Other grades stay undecided.
_NORMAL_FLAGS = {"normal", "n", "negative", "within_range", "optimal", "within_luteal_range"}

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "lab_tests_used": {"type": "boolean"},
        "usually_checked_by": {"type": "string"},
    },
    "required": ["lab_tests_used", "usually_checked_by"],
}

TESTS_SCHEMA = {
    "type": "object",
    "properties": {
        "tests": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "test": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "if_true": {"type": "string", "enum": list(DIRECTIONS)},
                    "other_cause": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["test", "aliases", "if_true", "other_cause", "why"],
            },
        },
    },
    "required": ["tests"],
}


def _catalog() -> list[dict[str, Any]]:
    """Every test on file, one entry per canonical name, draws oldest first."""
    rows = get_repositories().labs.query({"owner_email": OWNER_EMAIL}, "collected_date", 5000)
    by_test: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("value") is None or not row.get("test_name") or not lab_summary_eligible(row):
            continue
        by_test.setdefault(canonical_lab_name(row["test_name"]), []).append(
            {**row, "_date": _lab_date(row.get("collected_date"))}
        )
    catalog = []
    for name in sorted(by_test, key=str.lower):
        points, _ = _dedupe_lab_points(by_test[name])
        points.sort(key=lambda p: (p["_date"] is not None, p["_date"] or date.min, str(p.get("collected_date") or "")))
        catalog.append({"name": name, "points": points})
    return catalog


def _context_line(label: str, values: list[str]) -> str:
    return f"\n{label}: {', '.join(values)}" if values else ""


def _header(title: str, description: str, conditions: list[str], medications: list[str]) -> str:
    notes = f"\nNotes: {description}" if description else ""
    return (
        "You help a patient gather lab evidence about a health hypothesis for their clinician. "
        "You are not diagnosing.\n\n"
        f"Hypothesis: {title}{notes}"
        f"{_context_line('Confirmed conditions', conditions)}"
        f"{_context_line('Current medications and supplements', medications)}\n\n"
    )


def _triage_prompt(header: str) -> str:
    return header + (
        "Is this hypothesis usually checked with laboratory tests (blood, urine, saliva, stool or swab "
        "cultures)? Set \"lab_tests_used\" to true if lab results are a usual part of confirming or "
        "ruling it out, false if it is diagnosed mainly by history, examination, imaging or other "
        "procedures. In \"usually_checked_by\", say in one short phrase how it is usually diagnosed."
    )


def _tests_prompt(header: str) -> str:
    return header + (
        "List the laboratory tests (blood, urine, saliva, stool or swab cultures; not imaging) a "
        f"clinician usually orders or reviews to check this hypothesis, most useful first, at most "
        f"{MAX_TESTS}. List single tests, not ratios or panels, and only real tests that clinical labs "
        "offer. For each give:\n"
        "- \"test\": its common name\n"
        f"- \"aliases\": up to {MAX_ALIASES} other names or abbreviations labs print for it\n"
        "- \"if_true\": how the result usually reads if the hypothesis is true: \"high\", \"low\", "
        "\"positive\" (an antibody or marker present), \"negative\", or \"any\" when it has no usual direction\n"
        "- \"other_cause\": a confirmed condition or current medication listed above that is well known "
        "to push this test the same way by itself (for example, thyroid hormone medication lowers TSH); "
        "\"\" if none does\n"
        "- \"why\": one short sentence"
    )


def _flag_kind(flag: str) -> str | None:
    if flag in _HIGH_FLAGS:
        return "high"
    if flag in _LOW_FLAGS:
        return "low"
    if flag in _DIRECTIONLESS_ABNORMAL or flag == "positive":
        return "abnormal"
    if flag in _NORMAL_FLAGS:
        return "normal"
    return None


def suggested_role(if_true: str, flag: str) -> str | None:
    """Supporting or opposing from the expected direction and the result's
    flag; None when the flag or direction cannot decide it."""
    kind = _flag_kind(flag)
    if kind is None or if_true == "any":
        return None
    if if_true in ("high", "low"):
        if kind == if_true:
            return "supporting"
        return "opposing" if kind == "normal" or kind in ("high", "low") else None
    if if_true == "positive":
        return "opposing" if kind == "normal" else "supporting"
    if if_true == "negative":
        return "supporting" if kind == "normal" else "opposing"
    return None


def _value_text(point: dict[str, Any]) -> str:
    original = str(point.get("original_value") or "").strip()
    if original and original[0] in "<>":
        return original  # a censored result keeps its "<" or ">"
    value = point.get("value")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def _range_text(point: dict[str, Any]) -> str:
    low, high = point.get("reference_low"), point.get("reference_high")
    if low is not None and high is not None and not (low == 0 and high == 0):
        return f"{low:g}–{high:g}" if isinstance(low, (int, float)) and isinstance(high, (int, float)) else f"{low}–{high}"
    text = str(point.get("original_reference_range") or "").strip()
    # Multi-population ranges ("> or = 20 Years 0.40-4.50; Pregnancy ...")
    # keep the first, adult, segment.
    return text.removeprefix("Reference Range:").split(";")[0].strip()[:80]


def _when(point: dict[str, Any]) -> str:
    return point["_date"].isoformat() if point["_date"] else str(point.get("collected_date") or "undated")


def _lab_suggestion(
    name: str,
    points: list[dict[str, Any]],
    if_true: str,
    other_cause: str,
    why: str,
    existing_ids: set[str],
) -> dict[str, Any]:
    latest = points[-1]
    flag = effective_flag(latest)[0]
    unit = str(latest.get("unit") or "").strip()
    value = f"{_value_text(latest)} {unit}".strip()
    reference = _range_text(latest)
    earlier = [
        {"value": f"{_value_text(p)} {str(p.get('unit') or '').strip()}".strip(), "date": _when(p)}
        for p in reversed(points[-1 - EARLIER_SHOWN:-1])
    ]
    detail = ", ".join(part for part in (flag or "no flag", f"reference {reference}" if reference else "") if part)
    summary = f"{name} {value} on {_when(latest)} ({detail})."
    if earlier:
        summary += " Earlier: " + "; ".join(f"{e['value']} on {e['date']}" for e in earlier) + "."
    direction = "any direction" if if_true == "any" else if_true
    summary += f" Usually {direction} if this is true ({METHOD}): {why}"
    if other_cause:
        summary += f" Also affected by: {other_cause}."
    record_id = latest.get("record_id")
    link = (
        {"kind": "lab_report", "label": "lab report", "href": f"/api/records/file/{record_id}"}
        if record_id
        else {"kind": "normalized_entity", "href": f"/api/evidence/sources/LabResult/{latest['id']}"}
    )
    return {
        "test": name,
        "value": value,
        "date": _when(latest),
        "flag": flag,
        "reference": reference,
        "earlier": earlier,
        "draws": len(points),
        "if_true": if_true,
        "other_cause": other_cause,
        "why": why,
        # A condition or medication that could produce the result by itself
        # leaves the direction for the owner to judge.
        "suggested_role": None if other_cause else suggested_role(if_true, flag),
        "already_added": latest["id"] in existing_ids,
        "evidence": {
            "source_kind": "entity",
            "source_type": "LabResult",
            "source_id": latest["id"],
            "source_version": str(latest.get("updated_date") or latest.get("extraction_observation_id") or ""),
            "summary": summary[:2000],
            "weight": 1,
            "source_link": link,
        },
    }


_PARENTHETICAL = re.compile(r"\(([^)]*)\)")


def _names_for(test: str, aliases: list[str]) -> list[str]:
    """The test's own name first, then its aliases, each also without and as
    its parenthetical ("Thyroid stimulating immunoglobulin (TSI)")."""
    names: list[str] = []
    for name in [test, *aliases]:
        names.append(name)
        names.append(_PARENTHETICAL.sub(" ", name))
        names.extend(_PARENTHETICAL.findall(name))
    return [name for name in (" ".join(n.split()) for n in names) if name]


# Assay variants of one analyte: "hs CRP" is a CRP, "TPO Ab, quantitative" a
# TPO antibody. A test matches a name on file that adds only these words.
_VARIANT_WORDS = frozenset({
    "hs", "high", "sensitivity", "sensitive", "ultrasensitive", "quant", "quantitative", "direct",
})


def _compact(key: str) -> str:
    return key.replace(" ", "")


def _match(test: str, aliases: list[str], by_key: dict[str, list[dict[str, Any]]]) -> str | None:
    """The analyte key on file a named test refers to, or None. Exact keys
    first, then keys equal without spaces ("DHEAS" and "DHEA-S"), then keys on
    file that differ only by assay-variant words ("CRP" and "hs CRP")."""
    names = _names_for(test, aliases)
    keys = [key for key in (analyte_key(name) for name in names) if key]
    for key in keys:
        if key in by_key:
            return key
    compact = {_compact(key): key for key in by_key}
    for key in keys:
        if _compact(key) in compact:
            return compact[_compact(key)]
    for key in keys:
        wanted = set(key.split())
        for candidate in by_key:
            extra = set(candidate.split()) - wanted
            if extra and extra <= _VARIANT_WORDS and wanted <= set(candidate.split()):
                return candidate
    return None


# A "not on file" item must be a single lab test: imaging and panels (whose
# parts may well be on file) are left out.
_NOT_A_SINGLE_LAB = re.compile(
    r"\b(ultrasound|sonogram|mri|ct|scan|x-?ray|imaging|uptake|panel|profile|tilt)\b", re.IGNORECASE
)


# Words too general to tie an "other cause" to a listed condition or medication.
_GENERIC_WORDS = frozenset({
    "type", "disease", "syndrome", "disorder", "deficiency", "drops", "capsules",
    "tablet", "tablets", "mg", "mcg", "iu", "daily", "and", "the", "of", "with",
})


def _context_words(names: list[str]) -> set[str]:
    words: set[str] = set()
    for name in names:
        words |= {w for w in re.findall(r"[a-z]+", name.lower()) if len(w) > 2 and w not in _GENERIC_WORDS}
    return words


def _mentions(text: str, context_words: set[str]) -> bool:
    """Whether text names a listed condition or medication; "Hashimoto's"
    and "Hashimotos" share a stem."""
    for word in re.findall(r"[a-z]+", text.lower()):
        if len(word) < 4:
            continue
        if any(word.startswith(c[:max(4, len(c) - 2)]) or c.startswith(word[:max(4, len(word) - 2)])
               for c in context_words):
            return True
    return False


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


async def suggest(hypothesis_id: str, *, skip_triage: bool = False) -> dict[str, Any]:
    """Lab suggestions for one hypothesis. skip_triage asks for tests even when
    the model would call the hypothesis one that is diagnosed without labs."""
    hypothesis = SqliteHypothesisRepository().get(hypothesis_id)
    if not hypothesis:
        raise HTTPException(status_code=404, detail="hypothesis not found")
    catalog = _catalog()
    by_key: dict[str, list[dict[str, Any]]] = {}
    for entry in catalog:
        by_key.setdefault(analyte_key(entry["name"]), []).append(entry)
    conditions = [str(c["name"]) for c in get_conditions() if c.get("status") == "active" and c.get("name")]
    medications = [
        " ".join(part for part in (str(m["name"]), str(m.get("dose") or "")) if part)
        for m in get_medications()
        if m.get("status") == "active" and m.get("name")
    ]
    header = _header(hypothesis["title"], hypothesis.get("description") or "", conditions, medications)
    triage = await llm.invoke_llm(_triage_prompt(header), TRIAGE_SCHEMA, max_tokens=200, site=SITE, temperature=0)
    if not isinstance(triage, dict):
        raise HTTPException(status_code=502, detail="The model returned an unreadable answer.")
    checked_by = _clean(triage.get("usually_checked_by"), 200)
    if not skip_triage and str(triage.get("lab_tests_used")).strip().lower() in ("false", "no", "0"):
        return {
            "method": METHOD,
            "lab_tests_used": False,
            "usually_checked_by": checked_by,
            "labs": [],
            "not_on_file": [],
            "tests_on_file": len(catalog),
        }
    raw = await llm.invoke_llm(_tests_prompt(header), TESTS_SCHEMA, max_tokens=1500, site=SITE, temperature=0)
    if not isinstance(raw, dict):
        raise HTTPException(status_code=502, detail="The model returned an unreadable answer.")
    context_words = _context_words(conditions + medications)

    evidence = hypothesis.get("evidence") or []
    existing_ids = {item.get("source_id") for item in evidence if item.get("source_id")}
    existing_text = " ".join(str(item.get("summary") or "") for item in evidence).lower()
    labs: list[dict[str, Any]] = []
    not_on_file: list[dict[str, Any]] = []
    used_keys: set[str] = set()
    missing_names: set[str] = set()
    for item in (raw.get("tests") or [])[: MAX_TESTS * 2]:
        if not isinstance(item, dict):
            continue
        test = _clean(item.get("test"), 120)
        if not test:
            continue
        aliases = [_clean(a, 120) for a in (item.get("aliases") or []) if isinstance(a, str)][:MAX_ALIASES]
        if_true = _clean(item.get("if_true"), 20).lower()
        if if_true not in DIRECTIONS:
            if_true = "any"
        other_cause = _clean(item.get("other_cause"), 160).strip(" .")
        if not _mentions(other_cause, context_words):
            other_cause = ""  # only a condition or medication listed in the prompt counts
        why = _clean(item.get("why"), 300)
        key = _match(test, aliases, by_key)
        if key:
            if key in used_keys or len(labs) >= MAX_TESTS:
                continue
            used_keys.add(key)
            entries = by_key[key]
            # One analyte can sit under two printed names ("Free T3" and
            # "Triiodothyronine (T3), Free"); its draws are read together.
            points, _ = _dedupe_lab_points([p for entry in entries for p in entry["points"]])
            points.sort(key=lambda p: (p["_date"] is not None, p["_date"] or date.min, str(p.get("collected_date") or "")))
            name = max(entries, key=lambda entry: len(entry["points"]))["name"]
            labs.append(_lab_suggestion(name, points, if_true, other_cause, why, existing_ids))
        elif (
            test.lower() not in missing_names
            and len(not_on_file) < MAX_NOT_ON_FILE
            and not _NOT_A_SINGLE_LAB.search(test)
        ):
            missing_names.add(test.lower())
            summary = f"No {test} result in the app's lab results. {why}".strip()
            not_on_file.append({
                "test": test,
                "why": why,
                "already_added": f"no {test.lower()} result" in existing_text,
                "evidence": {
                    "role": "missing",
                    "source_kind": "missing",
                    "source_id": None,
                    "source_type": "",
                    "source_version": "",
                    "summary": summary[:2000],
                    "weight": 1,
                    "source_link": {},
                },
            })

    log.info("hypothesis lab suggestions: %d labs, %d not on file", len(labs), len(not_on_file))
    return {
        "method": METHOD,
        "lab_tests_used": True,
        "usually_checked_by": checked_by,
        "labs": labs,
        "not_on_file": not_on_file,
        "tests_on_file": len(catalog),
    }


@router.post("/api/hypotheses/{hypothesis_id}/lab-suggestions", dependencies=[Depends(require_admin)])
async def lab_suggestions(hypothesis_id: str, anyway: bool = False):
    return await suggest(hypothesis_id, skip_triage=anyway)
