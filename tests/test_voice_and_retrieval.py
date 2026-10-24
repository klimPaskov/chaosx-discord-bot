"""Spoken replies, the recogniser's guards, and retrieval query building.

Every one of these exists because of a live failure in the voice channel, so they are pinned here:
markdown read aloud, a domain prompt hallucinating on short audio, "Chaos Redux" heard as "case you're
ducks", and stopwords crowding out the actual event file in retrieval ranking.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chaosx_bot.knowledge import _query_terms  # noqa: E402
from chaosx_bot.testing_poll import candidate_label  # noqa: E402
from chaosx_bot.voice_audio import _correct_domain_terms  # noqa: E402
from chaosx_bot.voice_chat import VoiceChatSession, looks_like_prompt_echo, speech_safe  # noqa: E402


@pytest.mark.parametrize(
    "raw, forbidden",
    [
        ("## Testing poll\n- **One vote** per user\n- See [the docs](https://example.com/x)", ["#", "*", "https"]),
        ("### Zombie Apocalypse\n1. Step one\n2. Step two", ["#", "1.", "2."]),
        ("> quoted text\n| a | b |", [">", "|"]),
        ("Use `chaosx_super_event.3` here", ["`"]),
    ],
)
def test_speech_safe_strips_markdown(raw, forbidden):
    spoken = speech_safe(raw)
    for token in forbidden:
        assert token not in spoken, f"{token!r} survived into the spoken text: {spoken!r}"
    assert "  " not in spoken


def test_speech_safe_replaces_bare_urls_with_words():
    assert "a link" in speech_safe("See https://example.com/some/path for details")


def test_speech_safe_keeps_the_words():
    spoken = speech_safe("## Testing poll\n- **One vote** per user")
    assert "Testing poll" in spoken and "One vote per user" in spoken


def test_speech_safe_drops_emoji_but_not_newlines():
    spoken = speech_safe("Chaos 🔴 tier\nnext line")
    assert "🔴" not in spoken
    # the newline must become a space, never be deleted (that glued words together once)
    assert "tier next line" in spoken


@pytest.mark.parametrize(
    "heard, expected",
    [
        ("how many super events are in case you're ducks?", "Chaos Redux"),
        ("what is the supervent", "super event"),
        ("chaos reduce supervents", "Chaos Redux"),
        ("hello Chaos Redux", "Chaos Redux"),
        ("what is the crumb beneath", "crown beneath"),
        ("tell me about the black plates", "black plague"),
        ("who is hoops mccain", "Hoops McCann"),
        ("what is the atomic zombie league", "Anti-Zombie League"),
    ],
)
def test_domain_corrections_repair_real_mishearings(heard, expected):
    assert expected in _correct_domain_terms(heard)


def test_domain_corrections_do_not_touch_ordinary_words():
    # the map is deliberately built from strings that cannot occur in a real question
    for clean in (
        "how many events are in the mod",
        "when is the next playtest",
        "what does the tier system do",
    ):
        assert _correct_domain_terms(clean) == clean


def test_domain_corrections_leave_clean_speech_alone():
    clean = "when is the next community playtest"
    assert _correct_domain_terms(clean) == clean


@pytest.mark.parametrize("text", ["Chaos Redux.", "super events hoi4 mod", "chaos", "  Super Events  "])
def test_prompt_echo_is_recognised(text):
    assert looks_like_prompt_echo(text) is True


@pytest.mark.parametrize(
    "text",
    ["hi", "hey there", "how many super events are in chaos redux", "", "what is the zombie outbreak"],
)
def test_real_speech_is_never_mistaken_for_a_prompt_echo(text):
    # a discarded transcript means the bot goes silent, so this must never fire on speech
    assert looks_like_prompt_echo(text) is False


def test_query_terms_drop_stopwords_but_keep_the_subject():
    terms = _query_terms("what is the zombie outbreak super event")
    assert "zombie" in terms and "outbreak" in terms
    assert not {"what", "is", "the"} & set(terms)


def test_query_terms_keep_order_and_ignore_punctuation():
    assert _query_terms("Black Plague, super-event?")[:2] == ["black", "plague"]


def _session_stub(max_chars: int) -> SimpleNamespace:
    return SimpleNamespace(settings=SimpleNamespace(voice_max_answer_chars=max_chars))


def test_trim_for_voice_respects_the_cap():
    stub = _session_stub(120)
    long_text = "First sentence is here. " + "Second sentence that is much longer. " * 6
    trimmed = VoiceChatSession._trim_for_voice(stub, long_text)
    assert len(trimmed) <= 121
    assert trimmed.endswith((".", "!", "?", "…"))


def test_trim_for_voice_leaves_short_answers_intact():
    stub = _session_stub(320)
    assert VoiceChatSession._trim_for_voice(stub, "Short and sweet.") == "Short and sweet."


def test_candidate_label_leads_with_the_catalog_id():
    # testers quote the ID in /playtest report, so it must survive onto the ballot
    label = candidate_label("event", "1", "Communist Insurgency")
    assert "Communist Insurgency" in label
    assert "1" in label
