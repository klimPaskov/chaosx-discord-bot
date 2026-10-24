"""A plain project question without a mention must reach an answer, plurals included.

Hoops, 2026-10-24: "Okay, what are the mcps used for?" got no reply — the term list holds "mcp" and the
matcher required an exact space-delimited hit, so "mcps" carried no domain signal.
"""

from chaosx_bot.auto_scan import (
    classify_auto_answer,
    classify_message,
    _contains_normalized_phrase,
    normalize_scan_text,
)
from chaosx_bot.config import load_settings


class FakeKnowledge:
    def public_ask_context(self, query: str) -> str:
        return "HOI4 Agent Tools and HOI4 Live Tools are the two MCP servers."


def _classify(content: str):
    settings = load_settings()
    return classify_auto_answer(content, knowledge=FakeKnowledge(), settings=settings)


def test_plural_term_matches_its_singular():
    text = normalize_scan_text("what are the mcps used for?")
    assert _contains_normalized_phrase(text, "mcp")
    assert _contains_normalized_phrase(normalize_scan_text("any mods coming?"), "mod")
    assert _contains_normalized_phrase(normalize_scan_text("show me events"), "event")


def test_es_suffix_does_not_leak_generic_words():
    assert not _contains_normalized_phrase(normalize_scan_text("what modes are there?"), "mod")


def test_plain_question_about_the_mcps_is_answered():
    decision = _classify("Okay, what are the mcps used for?")
    assert decision.acted, decision.reason
    assert decision.source == "project_context"


def test_casual_question_without_domain_signal_stays_quiet():
    decision = classify_message("what time is it for you?", knowledge=FakeKnowledge(), settings=load_settings())
    assert not decision.acted
