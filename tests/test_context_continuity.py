"""Continuity + memory-discipline regression tests.

Two live bugs (Hoops 2026-09-19):
1. A short name-addressed message ("Idk ask chaosX") was answered as "Not much of a
   question to work with there" instead of being resolved against the conversation
   it was replying into.
2. An owner ask ("is the server dead") was answered with an unrelated stored
   moderation note, because stored records were appended AFTER the request — the
   prompt ended on the memory instead of the question.
"""

from chaosx_bot.bot import addressed_message_context, looks_like_fragment
from chaosx_bot.channel_context import REPLY_CONTEXT_LABEL, format_reply_context
from chaosx_bot.hermes_bridge import (
    AUTO_SCAN_BANTER_BOUNDARY,
    PUBLIC_ASK_BOUNDARY,
    build_owner_prompt,
    build_public_prompt,
)

MEMORY = (
    "## Previous /admin ask context\n"
    "### Turn 1 — 2026-09-06 status=ok hash=310d1ff1fd4f\n"
    "Owner asked: what is this image?\n"
    "ChaosX answered: nothing came through with the request.\n"
    "### Turn 2 — 2026-09-06 status=ok hash=abc123\n"
    "Owner asked: who was warned?\n"
    "ChaosX answered: TheSussyOne was actioned under Rule 13 (genocide-spam)."
)


def test_owner_prompt_keeps_stored_records_out_of_the_request_slot():
    prompt = build_owner_prompt(
        owner_request="is the server dead",
        guild_name="Chaos Redux",
        channel_name="automation",
        memory_context=MEMORY,
    )
    assert prompt.rstrip().endswith("is the server dead")
    assert prompt.index("BACKGROUND ONLY") < prompt.index("Owner request:")
    assert prompt.index("TheSussyOne was actioned under Rule 13") < prompt.index("Owner request:")


def test_owner_memory_block_tells_the_model_to_ignore_unrelated_records():
    prompt = build_owner_prompt(
        owner_request="is the server dead",
        guild_name="Chaos Redux",
        channel_name="automation",
        memory_context=MEMORY,
    )
    assert "never to supply an answer" in prompt
    assert "ignore the background completely" in prompt


def test_owner_prompt_without_memory_has_no_background_block():
    prompt = build_owner_prompt(
        owner_request="what changed in the last commit?",
        guild_name="Chaos Redux",
        channel_name="automation",
    )
    assert "BACKGROUND ONLY" not in prompt
    assert prompt.rstrip().endswith("what changed in the last commit?")


def test_public_boundary_requires_resolving_what_the_message_refers_to():
    assert "Read the message you are answering in its conversation" in PUBLIC_ASK_BOUNDARY
    assert 'said "ask ChaosX"' in PUBLIC_ASK_BOUNDARY
    assert "instead of saying there is nothing to work with" in PUBLIC_ASK_BOUNDARY


def test_banter_boundary_follows_the_ongoing_conversation():
    assert "Follow the ongoing conversation" in AUTO_SCAN_BANTER_BOUNDARY
    assert "do not pivot to stored summaries or records" in AUTO_SCAN_BANTER_BOUNDARY


def test_public_prompt_places_addressed_context_before_the_question():
    addressed = addressed_message_context(
        raw_content="Idk ask chaosX",
        request="Idk ask",
        reply_context="- TheSussyOne: why though",
        has_conversation=True,
    )
    prompt = build_public_prompt(
        user_request="Idk ask",
        guild_name="Chaos Redux",
        channel_name="vibe-coding-general",
        conversation_context="Recent conversation:\n- TheSussyOne: @Siegfried u dead?\n- Siegfried: Yeah",
        addressed_context=addressed,
    )
    assert 'The Discord message you were addressed with: "Idk ask chaosX"' in prompt
    assert "- TheSussyOne: why though" in prompt
    assert "carries no question of its own" in prompt
    assert prompt.index("## The message you are answering") < prompt.index("Community user question:")
    assert prompt.rstrip().endswith("Idk ask")


def test_addressed_context_is_empty_for_a_normal_self_contained_question():
    assert (
        addressed_message_context(
            raw_content="how does the zombie outbreak event work?",
            request="how does the zombie outbreak event work?",
        )
        == ""
    )


def test_addressed_context_omits_fragment_hint_without_conversation():
    block = addressed_message_context(
        raw_content="Idk ask chaosX",
        request="Idk ask",
        has_conversation=False,
    )
    assert "carries no question of its own" not in block
    assert 'The Discord message you were addressed with: "Idk ask chaosX"' in block


def test_looks_like_fragment():
    assert looks_like_fragment("Idk ask")
    assert looks_like_fragment("why though")
    assert looks_like_fragment("")
    assert not looks_like_fragment("how does the zombie outbreak event work?")
    assert not looks_like_fragment(
        "explain how the chaos level drives nuclear escalation across the whole map"
    )


def test_format_reply_context_renders_oldest_first_and_skips_empty():
    block = format_reply_context(
        [
            {"author_name": "TheSussyOne", "content": "why though"},
            {"author_name": "Siegfried", "content": "Idk ask chaosX"},
        ]
    )
    assert block.startswith(REPLY_CONTEXT_LABEL)
    assert "- TheSussyOne: why though" in block
    assert block.index("TheSussyOne") < block.index("Siegfried")
    assert format_reply_context([]) == ""
    assert format_reply_context([{"author_name": "x", "content": "   "}]) == ""
