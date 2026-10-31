"""Answer shape rules: concise style, no provenance, honest capability claims."""

from __future__ import annotations

from chaosx_bot.formatting import (
    GROUNDING_RULE,
    RELEVANCE_RULE,
    STRUCTURE_RULE,
    trim_answer_filler,
)
from chaosx_bot.hermes_bridge import AUTO_SCAN_ANSWER_BOUNDARY, PUBLIC_ASK_BOUNDARY


def test_structure_rule_asks_for_short_conversational_answers():
    folded = STRUCTURE_RULE.casefold()
    assert "one or two short paragraphs" in folded
    assert "match the length" in folded
    # The old rule asked for headings and bullets on anything with more than two points.
    assert "more than two points" not in folded
    assert "no headings unless" in folded


def test_no_intro_or_signoff():
    folded = STRUCTURE_RULE.casefold()
    assert "no opening label" in folded
    assert "closing summary" in folded


def test_capability_claims_are_never_denied_from_absence():
    folded = GROUNDING_RULE.casefold()
    assert "never state that a project tool, workflow or feature does not exist" in folded
    assert "not sure" in folded
    assert "documented" in folded and "experimental" in folded


def test_public_boundaries_carry_the_new_rules():
    for boundary in (PUBLIC_ASK_BOUNDARY, AUTO_SCAN_ANSWER_BOUNDARY):
        assert STRUCTURE_RULE in boundary
        assert RELEVANCE_RULE in boundary
        assert GROUNDING_RULE in boundary


def test_public_boundary_makes_links_conditional():
    folded = PUBLIC_ASK_BOUNDARY.casefold()
    assert "a link already given in this conversation" in folded
    assert "explanations of how a tool or feature works carry no link" in folded


def test_trim_answer_filler():
    assert trim_answer_filler("Short answer: yes, it does.") == "Yes, it does."
    assert trim_answer_filler("Great question! It fires on day 3.") == "It fires on day 3."
    assert trim_answer_filler("It fires on day 3.\n\nLet me know if you want the steps.") == "It fires on day 3."
    assert trim_answer_filler("Hope this helps.") == "Hope this helps."  # never empties an answer
    assert trim_answer_filler("The meter fills from events.") == "The meter fills from events."
