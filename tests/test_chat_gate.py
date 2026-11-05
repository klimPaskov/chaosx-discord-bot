"""Casual-reply gates: address, repeat, in-flight, budget, troll cooldown (Hoops, 2026-11-05)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from chaosx_bot import chat_gate
from chaosx_bot.chat_gate import (
    ChatGate,
    InFlight,
    RepeatWindow,
    ReplyBudget,
    TrollTracker,
    addresses_bot,
    is_jab_at_bot,
    looks_like_insult,
    normalize,
)

NOW = datetime(2026, 11, 5, 21, 0, tzinfo=timezone.utc)


def test_normalize_strips_emoji_mentions_and_punctuation():
    assert normalize("Kill all clankers <:pepe_sip:123456789012345678> <@123>!!") == "kill all clankers"
    assert normalize("  KILL   ALL clankers  ") == "kill all clankers"


def test_address_gate_requires_an_actual_address():
    assert addresses_bot(content="hi", mentioned=True, replies_to_bot=False, name_addressed=False)
    assert addresses_bot(content="chaosx hi", mentioned=False, replies_to_bot=False, name_addressed=True)
    assert addresses_bot(content="hi", mentioned=False, replies_to_bot=True, name_addressed=False)
    assert addresses_bot(content="hi again", mentioned=False, replies_to_bot=False, name_addressed=False, in_chain=True)
    # A human answering a human outside a banter channel is not addressed to the bot.
    assert not addresses_bot(content="<@1125> hi", mentioned=False, replies_to_bot=False, name_addressed=False)


def test_banter_channel_chat_counts_as_addressed():
    """#bot-spam exists for talking to the bot: plain chat gets an answer there."""
    assert addresses_bot(
        content="Kill all clankers", mentioned=False, replies_to_bot=False, name_addressed=False,
        banter_channel=True,
    )
    # But two humans talking are still left alone.
    assert not addresses_bot(
        content="<@1125> hi", mentioned=False, replies_to_bot=False, name_addressed=False,
        banter_channel=True, mentions_other=True,
    )
    assert not addresses_bot(
        content="yeah agreed", mentioned=False, replies_to_bot=False, name_addressed=False,
        banter_channel=True, replies_to_other=True,
    )


def test_repeat_window_answers_one_of_many_identical_lines():
    window = RepeatWindow(window_seconds=180)
    assert not window.is_repeat(channel_id=1, author_id=2, text="Kill all clankers", now=NOW)
    window.note(channel_id=1, author_id=2, text="Kill all clankers", now=NOW)
    for seconds in (1, 2, 30, 120):
        assert window.is_repeat(
            channel_id=1, author_id=2, text="Kill all clankers", now=NOW + timedelta(seconds=seconds)
        )
    # The concatenated paste is the same line.
    assert window.is_repeat(
        channel_id=1, author_id=2, text="Kill all clankersKill all clankers", now=NOW + timedelta(seconds=5)
    )
    # After the window a fresh answer is allowed.
    assert not window.is_repeat(
        channel_id=1, author_id=2, text="Kill all clankers", now=NOW + timedelta(seconds=181)
    )
    # A different member is unaffected.
    assert not window.is_repeat(channel_id=1, author_id=9, text="Kill all clankers", now=NOW)


def test_repeat_window_ignores_empty_text():
    window = RepeatWindow()
    window.note(channel_id=1, author_id=2, text="<:pepe_sip:123456789012345678>", now=NOW)
    assert not window.is_repeat(channel_id=1, author_id=2, text="<:pepe_stare:123456789012345679>", now=NOW)


def test_inflight_allows_one_model_call_per_identical_line():
    lock = InFlight()
    assert lock.acquire(channel_id=1, text="Kill all clankers")
    assert not lock.acquire(channel_id=1, text="kill all clankers!!")
    lock.release(channel_id=1, text="Kill all clankers")
    assert lock.acquire(channel_id=1, text="Kill all clankers")


def test_budget_enforces_the_channel_gap():
    budget = ReplyBudget(gap_seconds=25, one_in=1)
    assert budget.check(channel_id=1, author_id=2, now=NOW).allowed
    budget.note_reply(channel_id=1, author_id=2, now=NOW)
    assert not budget.check(channel_id=1, author_id=2, now=NOW + timedelta(seconds=10)).allowed
    assert budget.check(channel_id=1, author_id=9, now=NOW + timedelta(seconds=26)).allowed
    # A different channel keeps its own gap.
    assert budget.check(channel_id=2, author_id=2, now=NOW + timedelta(seconds=10)).allowed


def test_budget_needs_n_new_messages_before_replying_to_the_same_member_again():
    budget = ReplyBudget(gap_seconds=0, one_in=3)
    budget.note_reply(channel_id=1, author_id=2, now=NOW)
    # First message after a reply: still inside the one-in-three rule.
    budget.note_member_message(channel_id=1, author_id=2)
    assert not budget.check(channel_id=1, author_id=2, now=NOW + timedelta(seconds=10)).allowed
    budget.note_member_message(channel_id=1, author_id=2)
    assert not budget.check(channel_id=1, author_id=2, now=NOW + timedelta(seconds=20)).allowed
    budget.note_member_message(channel_id=1, author_id=2)
    assert budget.check(channel_id=1, author_id=2, now=NOW + timedelta(seconds=30)).allowed


def test_budget_never_gates_a_member_the_bot_has_not_answered():
    budget = ReplyBudget(gap_seconds=25, one_in=3)
    budget.note_member_message(channel_id=1, author_id=5)
    assert budget.check(channel_id=1, author_id=5, now=NOW).allowed


def test_troll_tracker_disengages_on_the_same_repeated_line():
    tracker = TrollTracker(repeat_threshold=3, cooldown_seconds=900)
    verdicts = [
        tracker.observe(author_id=7, channel_id=1, text="Kill all clankers", now=NOW + timedelta(seconds=i))
        for i in range(3)
    ]
    assert not verdicts[0] and not verdicts[1]
    assert verdicts[2].disengage and verdicts[2].reason == "repeating the same line"
    assert tracker.muted(7, now=NOW + timedelta(seconds=10))
    assert not tracker.muted(7, now=NOW + timedelta(seconds=1000))


def test_troll_tracker_disengages_on_a_single_channel_flood():
    tracker = TrollTracker(flood_messages=6, flood_window_seconds=60)
    for i in range(5):
        assert not tracker.observe(author_id=8, channel_id=1, text=f"line {i}", now=NOW + timedelta(seconds=i))
    verdict = tracker.observe(author_id=8, channel_id=1, text="line 5", now=NOW + timedelta(seconds=6))
    assert verdict.disengage and verdict.reason == "flooding the channel"


def test_troll_tracker_disengages_on_insult_only_messages():
    tracker = TrollTracker(jab_threshold=3)
    for i, text in enumerate(["stupid clanker", "chaosx is dumb", "get a job"]):
        verdict = tracker.observe(author_id=9, channel_id=1, text=text, now=NOW + timedelta(seconds=i * 20))
    assert verdict.disengage and verdict.reason == "only posting insults"
    assert is_jab_at_bot("stupid clanker")
    assert looks_like_insult("kill all clankers")
    assert not looks_like_insult("how do I install the mod?")


def test_troll_tracker_ignores_ordinary_conversation():
    tracker = TrollTracker()
    for i, text in enumerate(["hi", "how does zombie outbreak work", "thanks", "ok", "nice one"]):
        assert not tracker.observe(
            author_id=4, channel_id=1, text=text, now=NOW + timedelta(seconds=i * 30)
        )
    assert not tracker.muted(4, now=NOW)


def test_chat_gate_full_order_reports_the_first_blocking_gate():
    gate = ChatGate(reply_gap_seconds=25, one_in=1, troll_cooldown_seconds=600)
    gate.observe(channel_id=1, author_id=2, text="Kill all clankers", now=NOW)
    assert gate.casual_allowed(channel_id=1, author_id=2, text="Kill all clankers", now=NOW) == (True, "")
    gate.note_answered(channel_id=1, author_id=2, text="Kill all clankers", now=NOW)
    allowed, reason = gate.casual_allowed(
        channel_id=1, author_id=2, text="Kill all clankers", now=NOW + timedelta(seconds=5)
    )
    assert not allowed and reason == "repeat of a message already answered"


def test_chat_gate_can_be_disabled():
    gate = ChatGate(enabled=False, reply_gap_seconds=999)
    assert gate.casual_allowed(channel_id=1, author_id=2, text="x", now=NOW) == (True, "")


def test_observed_troll_blocks_every_casual_reply():
    gate = ChatGate(troll_cooldown_seconds=600)
    for i in range(3):
        gate.observe(author_id=3, channel_id=1, text="kill all clankers", now=NOW + timedelta(seconds=i))
    allowed, reason = gate.casual_allowed(
        channel_id=1, author_id=3, text="something else entirely", now=NOW + timedelta(seconds=10)
    )
    assert not allowed and reason == "member in troll cooldown"
