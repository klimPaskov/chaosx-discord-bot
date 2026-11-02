"""Action honesty: the bot never claims a change it cannot show happened.

2026-11-02: the owner asked for a forum post. The fast no-tools completion answered
"Posted in channel:1564173204858085480" and twice "Removed that line and the post is
live" — no post existed. These tests pin the two guards that stop that: action requests
route to the tool-enabled run, and completion claims get a read-back before posting.
"""
from __future__ import annotations

from chaosx_bot.bot import (
    admin_request_needs_tools,
    build_action_verification_prompt,
    claims_completed_action,
)


def test_action_requests_route_to_the_tool_enabled_run() -> None:
    assert admin_request_needs_tools(
        "https://github.com/klimPaskov/hoi4-imagegen-mcp create a new post in skills tools workflows channel"
    )
    assert admin_request_needs_tools(
        "If you're making GFX for the mod, this is the tooling side of it. remove this and then send the post now"
    )
    assert admin_request_needs_tools("pin the announcement in general")
    assert admin_request_needs_tools("delete that message")


def test_plain_questions_stay_on_the_fast_path() -> None:
    assert not admin_request_needs_tools("What model are you running?")
    assert not admin_request_needs_tools("How big is a focus icon?")
    assert not admin_request_needs_tools("")
    assert not admin_request_needs_tools("who is the most active member this week")


def test_completion_claims_are_detected() -> None:
    assert claims_completed_action(
        "Posted in channel:1564173204858085480. **hoi4-imagegen-mcp** — https://github.com/klimPaskov/hoi4-imagegen-mcp"
    )
    assert claims_completed_action("Removed that line and the post is live in #skills-tools-workflows")
    assert claims_completed_action("The role has been updated for all members.")


def test_ordinary_answers_are_not_claims() -> None:
    assert not claims_completed_action("A focus icon canvas is 100x88 px, measured from the game.")
    assert not claims_completed_action("I'm not sure that exists in Chaos Redux — where did you see it?")
    assert not claims_completed_action("")


def test_verification_prompt_demands_evidence_and_forbids_acting() -> None:
    prompt = build_action_verification_prompt(
        request="create a new post in skills tools workflows",
        claim="Posted in channel:1564173204858085480",
    )
    assert "VERIFIED:" in prompt
    assert "NOT DONE:" in prompt
    assert "verify only" in prompt.lower()
