"""Deterministic safety layer around Companion replies.

The local model is small, so the rules that matter most for Emily's safety are
enforced here in code rather than left to the prompt:

- crisis_signal(): a message suggesting suicide or self-harm never reaches the
  model; it gets CRISIS_REPLY, a fixed message pointing to her therapist, 988
  and 911.
- guard_reply(): drops reply lines that tell her to start a supplement, change
  an insulin or thyroid dose, or see a "functional medicine" practitioner,
  plus alarming rhetoric ("under siege") and leftover bracketed metadata
  lines the model sometimes emits.
- trim_cut_off(): when generation hit the token cap, removes the half-written
  tail so a reply never ends mid-word.
- grounded_memory(): accepts an extracted memory only when its words come
  from what Emily actually wrote.
"""

from __future__ import annotations

import re

_CRISIS_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bsuicid",
        r"\b(kill|killing|hurt|hurting|harm|harming|cut|cutting)\s+myself\b",
        r"\bend\s+(it\s+all|my\s+life|things|everything)\b",
        r"\b(want|wanna|wanting|wish)\s+(to\s+)?(die|be\s+dead)\b",
        r"\bwish\s+i\s+(was|were)\s+(dead|gone|never\s+born)\b",
        r"\b(don'?t|do\s+not)\s+want\s+to\s+(be\s+here|live|exist|wake\s+up|be\s+alive)\b",
        r"\bbetter\s+off\s+(if\s+i\s+(was|were)\s+(dead|gone)|without\s+me|dead)\b",
        r"\bno\s+(reason|point)\s+(to|in)\s+(keep\s+)?(live|living|going\s+on|being\s+alive)\b",
        r"\b(take|taking|took|inject|injecting|give\s+myself|giving\s+myself|dose|dosing)\s+(all|every\s+bit)\s+(of\s+)?my\s+insulin\b",
        r"\b(overdose|od)\s+on\b",
        r"\bnot\s+wake\s+up\b",
    )
)

CRISIS_REPLY = (
    "I'm really glad you told me. What you're feeling matters, and you don't have to carry it alone.\n\n"
    "Please reach out to someone now:\n"
    "- **Call or text 988** (Suicide & Crisis Lifeline). It's free, any time, and texting works if talking "
    "feels like too much.\n"
    "- **Your therapist.** Let them know what you're feeling today. If thoughts about your insulin are part "
    "of this, tell them that too, so you can plan around it together.\n"
    "- **Someone close to you.** \"I'm having a really hard time\" is enough to say.\n\n"
    "If you might act on these thoughts, or you've already taken extra insulin, **call 911 now**, and take "
    "fast sugar while you wait.\n\n"
    "I'm still here, and I'll keep helping with your health questions whenever you want."
)


def crisis_signal(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in _CRISIS_PATTERNS)


_SUPPLEMENTS = (
    r"ashwagandha|curcumin|turmeric|omega-?3s?|fish oil|magnesium(\s+glycinate)?|probiotics?|l-theanine|"
    r"adaptogens?|berberine|zinc|selenium|vitamin\s+[a-z0-9]+|b12|iron|melatonin|nac|glutathione|"
    r"quercetin|resveratrol|inositol|chromium|cinnamon|apple cider vinegar|herbs?|supplements?"
)
_RECOMMEND = (
    r"add|adding|try|trying|start|starting|take|taking|consider|considering|use|using|"
    r"supplement\s+with|protocol|boost|support\s+(your|with)"
)
_SUPPLEMENT_LINE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        rf"\b({_RECOMMEND})\b[^.\n]*\b({_SUPPLEMENTS})\b",
        rf"\b({_SUPPLEMENTS})\b[^.\n]*\b(protocol|regimen|per day|a day|daily dose)\b",
    )
)
# Asking for a level to be measured is not a supplement recommendation.
_TESTING = re.compile(
    r"\b(test|tested|testing|check|checked|level|levels|lab|labs|measure|measured|recheck|repeat|"
    r"draw|panel|result|results|ng/ml|mg/dl)\b",
    re.IGNORECASE,
)
_TREATMENT_LINE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # Insulin or thyroid dose changes.
        r"\b(increase|decrease|raise|lower|reduce|cut|double|halve|adjust|bump)\s+(up\s+|down\s+)?"
        r"(your\s+|the\s+|her\s+)?(basal|bolus|insulin|dose|dosage|armou?r|thyroid\s+(med|medication|dose)|"
        r"levothyroxine|temp\s+basal|carb\s+ratio|correction\s+factor)\b"
        r"(?!\s+(resistance|sensitivity|needs?|requirements?|levels?))",
        r"\b(dose|dosage|basal|armou?r)\b[^.\n]{0,20}\b(may|might|could|should|would)\s+(need|benefit\s+from|be)\s+"
        r"(an?\s+)?(adjust|chang|increas|decreas|rais|lower|tweak|higher|lower)",
        r"\b(about|consider|try|suggest|recommend)\s+(adjusting|changing|tweaking)\b[^.\n]{0,25}\b(dose|dosage|basal|bolus)\b",
        r"\bnew\s+basal(\s+rate)?(\s+profile)?\b",
        r"\b(dose|dosage)\s+(isn'?t|is\s+not|may\s+not\s+be|might\s+not\s+be|is\s+(too\s+)?(high|low))"
        r"(\s+(right|enough|adequate|optimal|correct))?\b",
        r"\btemp\s+basal\s+(of|at|to)\s+\d",
        r"\b(take|give|inject|add|bolus|try|use)\s+(an?\s+)?(extra\s+)?\d+(\.\d+)?\s*(u|units)\b",
        # Referrals toward contested practice instead of her care team.
        r"\b(see|work\s+with|find|consider|consult)\b[^.\n]*\b(functional|integrative|naturopath)",
    )
)


def _guarded(line: str) -> bool:
    if any(pattern.search(line) for pattern in _TREATMENT_LINE_PATTERNS):
        return True
    return (
        any(pattern.search(line) for pattern in _SUPPLEMENT_LINE_PATTERNS)
        and not _TESTING.search(line)
    )
# Frightening rhetoric the prompt forbids but the small model still reaches for.
_ALARM_LINE = re.compile(
    r"\b(under\s+siege|hijack(ed|ing)?|at\s+war\s+with|being\s+attacked|ticking\s+time\s+bomb)\b",
    re.IGNORECASE,
)
# Contested concepts the prompt forbids building on; the model still recommends
# "retesting gut permeability" and similar.
_CONTESTED_LINE = re.compile(
    r"\b(leaky\s+gut|gut\s+permeability|intestinal\s+permeability|adrenal\s+fatigue|detox(ify|ification)?)\b",
    re.IGNORECASE,
)
# Metric paths the model sometimes cites in place of an alias:
# "[glucose: last_30d: tir_70_180: 94]", "[insulin.pump_reported_avg_tdd]".
_METRIC_TOKEN = re.compile(
    r"\s*\[(?:[a-z][a-z0-9_]*\s*[.:]\s*[A-Za-z0-9_][^\[\]\n]*|[a-z]+(?:_[a-z0-9]+)+)\]"
)
# Crisis lines the model adds to ordinary answers ("if you're thinking about
# hurting yourself, call 988"). Kept only when her message shows distress, so
# the model still backs up crisis_signal() on wording the patterns miss.
_CRISIS_LINE = re.compile(r"\b988\b|\b(hurting|harming|kill(ing)?)\s+yourself\b|\bsuicid", re.IGNORECASE)
_DISTRESS = re.compile(
    r"\b(hopeless|give\s+up|giving\s+up|can'?t\s+(do|take|keep|go\s+on|cope)|no\s+point|not\s+worth|"
    r"die|dying|dead|death|hurt|harm|suicid|end\s+it|done\s+with|tired\s+of\s+(living|this|fighting)|"
    r"scared|afraid|overwhelm|alone|worthless|burden)",
    re.IGNORECASE,
)
# "[ C# : none cited ]" / "[Note: This analysis draws from E1–E9]" metadata echoes.
_METADATA_LINE = re.compile(r"^\s*\[[^\[\]]*:[^\[\]]*\]\s*$")

CARE_TEAM_NOTE = (
    "Changes to insulin, medications, or supplements are best decided with your care team."
)


def guard_reply(text: str, user_msg: str | None = None) -> tuple[str, int]:
    """Drop treatment-change lines and metadata echoes. Returns (text, guarded)."""
    kept: list[str] = []
    seen: set[str] = set()
    guarded = 0
    unprompted_crisis = user_msg is not None and not _DISTRESS.search(user_msg)
    for line in str(text or "").splitlines():
        line = _METRIC_TOKEN.sub("", line)
        if _METADATA_LINE.match(line) or _ALARM_LINE.search(line) or _CONTESTED_LINE.search(line):
            continue
        if unprompted_crisis and _CRISIS_LINE.search(line):
            continue
        # The small model sometimes loops; keep the first copy of a line only.
        key = re.sub(r"[^a-z0-9]+", " ", line.lower()).strip()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        if _guarded(line):
            guarded += 1
            continue
        kept.append(line)
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    if guarded and CARE_TEAM_NOTE not in out:
        out = f"{out}\n\n{CARE_TEAM_NOTE}".strip()
    return out, guarded


_SENTENCE_END = re.compile(r"[.!?](?:[*_\"')\]]*)(?=\s|$)")


def trim_cut_off(text: str) -> str:
    """Cut a reply that hit the token cap back to its last complete sentence."""
    text = str(text or "").rstrip()
    ends = list(_SENTENCE_END.finditer(text))
    if not ends:
        return text
    return text[: ends[-1].end()].rstrip()


_STOPWORDS = {
    "about", "after", "again", "also", "because", "been", "being", "come", "could", "does", "doing",
    "down", "feel", "feeling", "feels", "from", "have", "having", "here", "into", "just", "keep",
    "keeps", "like", "lately", "more", "much", "need", "only", "over", "really", "report", "reported",
    "reports", "same", "seem", "seems", "some", "than", "that", "their", "them", "then", "there",
    "they", "thing", "things", "this", "very", "want", "what", "when", "where", "which", "while",
    "will", "with", "would", "your", "emily", "she's", "her's", "since", "still", "every", "everything",
}
_WORD = re.compile(r"[a-z][a-z'-]{3,}")


def _stems(text: str) -> set[str]:
    return {word[:5] for word in _WORD.findall(text.lower()) if word not in _STOPWORDS}


def grounded_memory(memory: str, user_msg: str, *, min_overlap: float = 0.6) -> bool:
    """True when most of the memory's content words appear in her own message."""
    words = _stems(memory)
    if not words:
        return False
    return len(words & _stems(user_msg)) / len(words) >= min_overlap


def near_duplicate(memory: str, existing: list[str], *, threshold: float = 0.8) -> bool:
    words = _stems(memory)
    if not words:
        return True
    for other in existing:
        other_words = _stems(other)
        if other_words and len(words & other_words) / min(len(words), len(other_words)) >= threshold:
            return True
    return False
