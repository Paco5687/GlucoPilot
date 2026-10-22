"""How lab tests are named across labs and documents.

One table of synonyms serves every reader of LabResult: the Visit Report and
Clinician Brief list a test once under its canonical name, and the Companion's
same-draw matching (``lab_dedupe``) keys on the same table, so the three never
disagree about which results are the same test.
"""

from __future__ import annotations

import re
from typing import Any

# Common synonyms for the same assay, keyed by a punctuation-free lowercase
# form of the reported name. Names not listed keep their reported form.
_LAB_SYNONYMS = {
    "Hemoglobin A1c": (
        "hba1c", "hb a1c", "a1c", "hemoglobin a1c", "haemoglobin a1c", "hgb a1c",
        "hemoglobin a1c hba1c", "glycated hemoglobin", "glycosylated hemoglobin",
        "glycohemoglobin", "hba1c ngsp",
    ),
    "TSH": ("tsh", "thyroid stimulating hormone", "thyrotropin", "tsh 3rd generation", "tsh sensitive"),
    "Free T4": ("free t4", "t4 free", "ft4", "free thyroxine", "thyroxine free", "t4 free direct"),
    "Free T3": ("free t3", "t3 free", "ft3", "free triiodothyronine", "triiodothyronine free"),
    "TPO antibodies": (
        "tpo", "tpo ab", "tpo abs", "tpo antibody", "tpo antibodies", "anti tpo", "anti tpo ab",
        "antitpo", "thyroid peroxidase", "thyroid peroxidase ab", "thyroid peroxidase abs",
        "thyroid peroxidase antibody", "thyroid peroxidase antibodies", "thyroid peroxidase tpo ab",
        "thyroid peroxidase tpo abs", "thyroid peroxidase tpo antibodies", "thyroperoxidase ab",
    ),
    "Thyroglobulin antibodies": (
        "tgab", "tg ab", "thyroglobulin ab", "thyroglobulin abs", "thyroglobulin antibody",
        "thyroglobulin antibodies", "anti thyroglobulin", "anti thyroglobulin ab", "antithyroglobulin ab",
        "thyroglobulin antibody tgab",
    ),
    "Reverse T3": (
        "reverse t3", "t3 reverse", "rt3", "reverse t3 rt3", "reverse t3 serum", "t3 reverse lc ms ms",
        "reverse t3 lc ms ms",
    ),
    "Cortisol (AM)": (
        "cortisol am", "am cortisol", "cortisol am specimen", "cortisol morning", "morning cortisol",
        "cortisol total am", "cortisol 8 am", "cortisol 8am",
    ),
    "Prolactin": ("prolactin", "prl", "prolactin serum"),
    "FSH": ("fsh", "follicle stimulating hormone"),
    "LH": ("lh", "luteinizing hormone"),
    "Estradiol": ("estradiol", "e2", "estradiol e2"),
    "ACTH": ("acth", "adrenocorticotropic hormone", "corticotropin"),
    "IGF-1": ("igf 1", "igf1", "insulin like growth factor 1", "insulin like growth factor i", "somatomedin c"),
    "C-peptide": ("c peptide", "cpeptide"),
    "Glucose": ("glucose", "glucose serum", "glucose plasma", "glucose fasting", "fasting glucose"),
    "Creatinine": ("creatinine", "creatinine serum"),
    "eGFR": ("egfr", "estimated gfr", "gfr estimated", "egfr ckd epi", "egfr if non africn am"),
    "Urine albumin/creatinine ratio": (
        "uacr", "albumin creatinine ratio", "microalbumin creatinine ratio", "albumin creatinine ratio urine",
        "microalbumin creatinine ratio urine", "urine albumin creatinine ratio",
    ),
    "LDL cholesterol": ("ldl", "ldl c", "ldl cholesterol", "ldl chol calc", "ldl cholesterol calc", "ldl calculated"),
    "HDL cholesterol": ("hdl", "hdl c", "hdl cholesterol"),
    "Total cholesterol": ("cholesterol", "cholesterol total", "total cholesterol"),
    "Triglycerides": ("triglycerides", "triglyceride", "trig"),
    "Vitamin D, 25-OH": (
        "vitamin d", "vitamin d 25 hydroxy", "vitamin d 25 oh", "25 oh vitamin d", "25 hydroxyvitamin d",
        "vitamin d 25 oh total", "25 hydroxy vitamin d",
    ),
    "Ferritin": ("ferritin", "ferritin serum"),
    "Hemoglobin": ("hemoglobin", "hgb", "hb", "haemoglobin", "hemoglobin female", "hemoglobin male"),
    "WBC": ("wbc", "wbcs", "total wbc", "total wbcs", "white blood cells", "white blood cell count", "wbc count"),
    "Sodium": ("sodium", "na", "sodium serum"),
    "Potassium": ("potassium", "k", "potassium serum"),
}
_LAB_CANONICAL = {alias: canonical for canonical, aliases in _LAB_SYNONYMS.items() for alias in aliases}

def _lab_key(name: Any) -> str:
    text = str(name or "").lower().replace(".", "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def canonical_lab_name(name: Any) -> str:
    return _LAB_CANONICAL.get(_lab_key(name), " ".join(str(name or "").split()))


# Names the same analyte under the spellings two labs actually used.
_PHRASES = (
    (re.compile(r"hemoglobin\s*a1c|hba1c|glycated\s+hemoglobin|glycohemoglobin"), " a1c "),
    # "T4,Free(Direct)" names the free T4 method, not direct bilirubin.
    (re.compile(r"free\s*\(\s*direct\s*\)"), " free "),
    (re.compile(r"triiodothyronine"), " t3 "),
    (re.compile(r"thyroxine"), " t4 "),
    (re.compile(r"alk(?:aline)?\.?\s*phos(?:phatase)?"), " alp "),
    (re.compile(r"c[-\s]?reactive\s+protein"), " crp "),
    (re.compile(r"25[-\s]?(?:oh|hydroxy)(?:vitamin)?"), " 25oh "),
    (re.compile(r"\beos(?:inophils)?\b"), " eosinophils "),
    (re.compile(r"\bbasos?\b|\bbasophils\b"), " basophils "),
    (re.compile(r"\blymphs?\b|\blymphocytes\b"), " lymphocytes "),
    (re.compile(r"\bt\.?\s*chol\b"), " cholesterol "),
    (re.compile(r"\bchol\b"), " cholesterol "),
    (re.compile(r"\bantibod(?:y|ies)\b|\babs?\b"), " ab "),
    (re.compile(r"%"), " percent "),
)
_QUALIFIERS = frozenset({
    "serum", "plasma", "total", "female", "male", "calc", "nih", "lc", "ms",
    "level", "levels", "test", "result", "the", "of",
})


def analyte_key(name: Any) -> str:
    """A matching key for one analyte across labs' spellings: the table's
    canonical name when the name is a listed synonym, then reduced to its
    distinguishing words, so "TSH" and "Thyroid stimulating hormone", or
    "HbA1c" and "Hemoglobin A1c, Total", key alike while "Free T3" and
    "Free T4" stay apart. For matching only; display uses canonical_lab_name."""
    text = canonical_lab_name(name).lower()
    for pattern, replacement in _PHRASES:
        text = pattern.sub(replacement, text)
    tokens = {token for token in re.findall(r"[a-z0-9]+", text) if token not in _QUALIFIERS}
    return " ".join(sorted(tokens))
