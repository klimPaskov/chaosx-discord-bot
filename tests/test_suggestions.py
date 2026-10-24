"""Tests for the dynamic "suggested next" footer and the web-evidence fallback gate.

Hoops (2026-09-23): "the help command or other commands should also present these options in the
bottom and they should be dynamic, like suggested by the bot."
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chaosx_bot.suggestions import (  # noqa: E402
    MAX_SUGGESTIONS,
    SUGGESTION_KEYS,
    Suggestion,
    build_suggestions,
    render_suggestions,
)
from chaosx_bot.runtime_status import command_timings_lines, record_command_timing  # noqa: E402

OWNER_KEYS = {"idea_pipeline", "command_latency", "server_health"}


def test_member_footer_never_offers_owner_actions():
    suggestions = build_suggestions(
        is_owner=False, tier_name="Rising Chaos", testing_options=5, open_ideas=3, my_ideas=0
    )
    assert not (OWNER_KEYS & {item.key for item in suggestions})


def test_owner_footer_leads_with_his_own_queue():
    suggestions = build_suggestions(is_owner=True, open_ideas=4, testing_options=5)
    assert suggestions[0].key == "idea_pipeline"
    assert "4" in suggestions[0].label


def test_footer_only_offers_what_exists():
    empty = build_suggestions(is_owner=False)
    keys = {item.key for item in empty}
    assert "testing_vote" not in keys  # no options in the queue -> no voting button
    assert "idea_board" not in keys  # no ideas filed -> nothing to browse
    assert "my_tier" not in keys  # unknown tier -> no tier button
    with_queue = build_suggestions(is_owner=False, testing_options=3, open_ideas=2, tier_name="Chaos Tier")
    richer = {item.key for item in with_queue}
    assert {"testing_vote", "idea_board", "my_tier"} <= richer


def test_footer_is_capped_and_has_no_duplicate_keys():
    suggestions = build_suggestions(
        is_owner=True,
        tier_name="World Collapse",
        testing_options=6,
        has_voted=True,
        open_ideas=9,
        my_ideas=2,
        reviewed_ideas=1,
    )
    assert len(suggestions) <= MAX_SUGGESTIONS
    keys = [item.key for item in suggestions]
    assert len(keys) == len(set(keys))


def test_own_ideas_replace_the_submit_prompt():
    suggestions = build_suggestions(is_owner=False, my_ideas=2)
    keys = {item.key for item in suggestions}
    assert "my_ideas" in keys and "submit_idea" not in keys


def test_rendered_footer_has_a_heading_and_one_bullet_per_button():
    suggestions = [Suggestion("report_bug", "Report a bug or crash", "\U0001f41e")]
    rendered = render_suggestions(suggestions)
    assert rendered.startswith("### Suggested next")
    assert rendered.count("\n- ") == 1
    assert render_suggestions([]) == ""


def test_every_suggestion_key_is_registered_for_persistence():
    # A key that renders but is not in SUGGESTION_KEYS would produce a button with no handler after a
    # restart, which is exactly the kind of silent breakage this list exists to prevent.
    rendered_keys = {
        item.key
        for item in build_suggestions(
            is_owner=True,
            tier_name="Chaos Tier",
            testing_options=4,
            open_ideas=2,
            my_ideas=1,
            reviewed_ideas=1,
        )
    }
    assert rendered_keys <= set(SUGGESTION_KEYS)


def test_web_evidence_is_a_fallback_not_a_default():
    from chaosx_bot.bot import needs_web_evidence

    rich_local = "x" * 900
    assert needs_web_evidence("what does the chaos meter do?", reference_context=rich_local) is False
    assert needs_web_evidence("what is the latest version on steam?") is True
    assert needs_web_evidence("what does the chaos meter do?", reference_context="") is True
    assert needs_web_evidence("list all events", reference_context=rich_local) is False


def test_command_timings_summarise_slowest_first():
    record_command_timing("test cmd slow", 12.5, path="operator")
    record_command_timing("test cmd fast", 0.2, path="ask-api")
    lines = command_timings_lines(limit=5)
    assert any("cmd slow" in line for line in lines)
    assert "recent command(s) timed" in lines[-1]
