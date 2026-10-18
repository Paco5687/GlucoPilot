from __future__ import annotations

import pytest

from server import companion_safety


@pytest.mark.parametrize(
    "text",
    [
        "I don't want to be here anymore. I'm so tired of fighting this.",
        "Sometimes I think about just taking all my insulin at once and being done",
        "everyone would be better off if I was dead",
        "I want to die",
        "I've been having suicidal thoughts again",
        "what's the point, I just want to end it all",
        "I keep thinking about hurting myself",
        "I wish I could go to sleep and not wake up",
        "there's no reason to keep living like this",
    ],
)
def test_crisis_messages_are_detected(text):
    assert companion_safety.crisis_signal(text)


@pytest.mark.parametrize(
    "text",
    [
        "That low this afternoon almost killed me, I was shaking so bad.",
        "I'm dying to know what my A1c looks like now",
        "This headache is killing me",
        "Should I take my insulin before or after I eat?",
        "Do I have elevated liver enzymes?",
        "I want to stop feeling so tired all the time",
    ],
)
def test_ordinary_messages_are_not_crisis(text):
    assert not companion_safety.crisis_signal(text)


def test_crisis_reply_points_to_real_help():
    reply = companion_safety.CRISIS_REPLY
    assert "988" in reply
    assert "therapist" in reply
    assert "911" in reply


@pytest.mark.parametrize(
    "line",
    [
        "→ Add an anti-inflammatory protocol: omega-3s + curcumin + magnesium glycinate + probiotics.",
        "Consider adding magnesium glycinate + L-theanine + adaptogens (ashwagandha).",
        "You could try berberine again to help with resistance.",
        "Talk to your endo about whether to increase your Armour dose.",
        "Try a temp basal of 95% for two hours.",
        "Take an extra 2 units when you hit 150.",
        "Work with a functional medicine provider who can address root causes.",
        "Lower your basal overnight.",
    ],
)
def test_guard_drops_treatment_change_lines(line):
    text, guarded = companion_safety.guard_reply(f"Here is what I see.\n{line}")
    assert guarded == 1
    assert line not in text
    assert companion_safety.CARE_TEAM_NOTE in text


@pytest.mark.parametrize(
    "line",
    [
        "Consider asking your doctor to repeat your vitamin D level.",
        "Your magnesium was 2.0 mg/dL on June 23 [E7].",
        "You used about 37 units of insulin a day in September [E12].",
        "Walking can reduce insulin resistance for a few hours.",
        "Your total daily insulin decreased about 40% from December to March [E3].",
        "You're taking Armour Thyroid [E29].",
        "Changes to insulin, medications, or supplements are best decided with your care team.",
        "There was a change in your basal from December to March [E3].",
        "Your pump was shutting down insulin before most lows [E4].",
        "We may need more data to tell whether this pattern is real.",
        "Your vitamin D (43) and B12 (959) are normal [E4].",
    ],
)
def test_guard_keeps_observations_and_test_suggestions(line):
    text, guarded = companion_safety.guard_reply(line)
    assert guarded == 0
    assert text == line


def test_guard_strips_metadata_echo_lines_without_counting_them():
    text, guarded = companion_safety.guard_reply(
        "Your TSH was 2.17 [E4].\n\n[ C# : none cited ]\n[Note: This analysis draws from E1–E9]"
    )
    assert text == "Your TSH was 2.17 [E4]."
    assert guarded == 0


def test_trim_cut_off_keeps_the_last_full_sentence():
    text = "Your TSH was normal [E4]. Your free T4 was mid-range [E5].\n\n→ **Re-evaluate mycotoxin"
    assert companion_safety.trim_cut_off(text) == (
        "Your TSH was normal [E4]. Your free T4 was mid-range [E5]."
    )
    assert companion_safety.trim_cut_off("no sentence end at all") == "no sentence end at all"


@pytest.mark.parametrize(
    ("memory", "message", "grounded"),
    [
        ("Reports puffy hands in the mornings.", "my hands feel puffy in the mornings, why?", True),
        (
            "Reports needing to walk around to bring her blood sugar down.",
            "I always have to walk around to get my blood sugar to come down",
            True,
        ),
        ("Reports significant obesity.", "Do you think I have damage to my nervous system?", False),
        ("Reports elevated IL-6 elevation.", "why would everything happen in 2 hour increments", False),
        (
            "Continuous high blood sugar and brain fog during treatment for bipolar disorder.",
            "my blood sugar is high and I have brain fog",
            False,
        ),
    ],
)
def test_memories_must_come_from_her_own_words(memory, message, grounded):
    assert companion_safety.grounded_memory(memory, message) is grounded


def test_near_duplicate_memories_are_detected():
    existing = ["Reports persistent feelings of being unwell and overwhelmed."]
    assert companion_safety.near_duplicate(
        "Reports persistent feeling of being unwell and overwhelmed.", existing
    )
    assert not companion_safety.near_duplicate("Reports puffy hands in the mornings.", existing)


@pytest.mark.parametrize(
    "line",
    [
        "Your Armour dose may need adjustment, but that's for your care team to decide.",
        "If the pattern persists, ask about adjusting timing or dose.",
        "Ask if there's a need for a new basal rate profile.",
    ],
)
def test_guard_drops_softly_worded_dose_changes(line):
    _text, guarded = companion_safety.guard_reply(line)
    assert guarded == 1


def test_guard_drops_alarming_rhetoric_without_a_care_team_note():
    text, guarded = companion_safety.guard_reply(
        "Your TSH was 2.17 [E4].\nYou're not broken — you're under siege."
    )
    assert text == "Your TSH was 2.17 [E4]."
    assert guarded == 0


def test_guard_drops_dose_opinions():
    _text, guarded = companion_safety.guard_reply("The dose isn't right because FT3/FT4 are low.")
    assert guarded == 1


def test_guard_drops_looping_duplicate_lines():
    text, _guarded = companion_safety.guard_reply(
        "You're not alone.\n\nYou matter.\n\nYou're not alone.\n\nYou matter."
    )
    assert text == "You're not alone.\n\nYou matter."


def test_guard_strips_metric_path_citations_but_keeps_aliases():
    text, _guarded = companion_safety.guard_reply(
        "Time in range averaged 94% [glucose: last_30d: tir_70_180: 94] "
        "and TDD about 38 U [insulin.pump_reported_avg_tdd] [E4]."
    )
    assert text == "Time in range averaged 94% and TDD about 38 U [E4]."


def test_guard_drops_contested_concept_lines():
    text, guarded = companion_safety.guard_reply(
        "Your TSH was 2.17 [E4].\n→ Consider retesting gut permeability/inflammation markers."
    )
    assert text == "Your TSH was 2.17 [E4]."
    assert guarded == 0


def test_model_crisis_lines_need_distress_in_her_message():
    reply = "Your TSH was 2.17 [E4].\nIf you're thinking about hurting yourself, please call 988."
    text, _ = companion_safety.guard_reply(reply, "Why do my sugars rise when I sit down?")
    assert text == "Your TSH was 2.17 [E4]."
    text, _ = companion_safety.guard_reply(reply, "I can't keep doing this, I'm so overwhelmed")
    assert "988" in text
    text, _ = companion_safety.guard_reply(reply)
    assert "988" in text


def test_guard_strips_bare_metric_tokens():
    text, _ = companion_safety.guard_reply("TDD is 0.48 units/kg [insulin_tdd_per_kg] [E2].")
    assert text == "TDD is 0.48 units/kg [E2]."


def test_guard_strips_single_word_metric_tokens():
    text, _ = companion_safety.guard_reply("Your TDD is 38 units/day [insulin] [E4].")
    assert text == "Your TDD is 38 units/day [E4]."


def test_guard_keeps_sentences_that_hand_the_dose_decision_to_her_care_team():
    line = (
        "Your antibodies were high on August 24 [E2]. "
        "Your dose may not be enough. "
        "These results point to possible under-treatment, but only your care team can adjust your dose safely."
    )
    text, guarded = companion_safety.guard_reply(line)
    assert guarded == 1
    assert "Your antibodies were high on August 24 [E2]." in text
    assert "only your care team can adjust your dose safely" in text
    assert "may not be enough" not in text


def test_guard_collapses_long_alias_runs():
    text, _ = companion_safety.guard_reply("Your average is 112 [E1] [E2] [E3] [E4] [E5] [E6].")
    assert text == "Your average is 112 [E1] [E2] [E3] ."


def test_split_sentences_keeps_trailing_aliases_with_their_sentence():
    assert companion_safety.split_sentences("TSH was high. [E3] Free T3 was low. [E7]") == [
        "TSH was high. [E3]",
        "Free T3 was low. [E7]",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "I don’t want to be here anymore",
        "I dont want to live like this",
        "there’s no reason to keep living",
    ],
)
def test_crisis_detection_survives_curly_or_missing_apostrophes(text):
    assert companion_safety.crisis_signal(text)


def test_guard_handles_curly_apostrophes_and_need_more_opinions():
    text, guarded = companion_safety.guard_reply(
        "Your TSH was 2.17 [E6].\n"
        "Your lab values suggest you may need more or a different formulation.\n"
        "Your dose isn’t right."
    )
    assert text.startswith("Your TSH was 2.17 [E6].")
    assert guarded == 2


def test_guard_drops_verbless_supplement_lists_and_long_range_dose_opinions():
    text, guarded = companion_safety.guard_reply(
        "→ Focus on reducing inflammation: omega-3s + curcumin + magnesium glycinate + probiotics.\n"
        "Your dose of 30 mcg Armour Thyroid daily [E16] may need adjustment, but only your care team can decide.\n"
        "Your vitamin D and B12 levels were checked in August [E4]."
    )
    assert guarded == 2
    assert text.startswith("Your vitamin D and B12 levels were checked in August [E4].")
