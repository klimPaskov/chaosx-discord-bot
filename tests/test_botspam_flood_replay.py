"""Replay of the 2026-11-03 #bot-spam flood through the new gates.

That evening the channel carried 231 human messages and 231 ChaosX replies; 169 of the messages were
"Kill all clankers" (two distinct texts) and every one of them got an answer, 56 inside one minute.
This test drives the same shape through the gate and asserts the reply count, so a regression cannot
quietly bring the 1:1 ratio back.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from chaosx_bot.chat_gate import ChatGate

NOW = datetime(2026, 11, 3, 21, 12, tzinfo=timezone.utc)
BOT_SPAM = 1396551514469699765
SIEGFRIED = 1156181307504730152
SUSSY = 1125795621782159432


def _replies_for(gate: ChatGate, *, channel_id: int, author_id: int, text: str, when: datetime) -> bool:
    allowed, _reason = gate.casual_allowed(
        channel_id=channel_id, author_id=author_id, text=text, now=when
    )
    if allowed:
        gate.note_answered(channel_id=channel_id, author_id=author_id, text=text, now=when)
    return allowed


def test_the_169_message_flood_gets_one_reply_not_169():
    gate = ChatGate()
    replies = 0
    for i in range(169):
        when = NOW + timedelta(seconds=i * 2)
        gate.observe(channel_id=BOT_SPAM, author_id=SIEGFRIED, text="Kill all clankers", now=when)
        if _replies_for(gate, channel_id=BOT_SPAM, author_id=SIEGFRIED, text="Kill all clankers", when=when):
            replies += 1
    assert replies == 1


def test_a_second_member_speaking_normally_still_gets_an_answer():
    gate = ChatGate()
    for i in range(5):
        when = NOW + timedelta(seconds=i)
        gate.observe(channel_id=BOT_SPAM, author_id=SIEGFRIED, text="Kill all clankers", now=when)
        _replies_for(gate, channel_id=BOT_SPAM, author_id=SIEGFRIED, text="Kill all clankers", when=when)
    when = NOW + timedelta(seconds=30)
    gate.observe(channel_id=BOT_SPAM, author_id=SUSSY, text="what do the votes look like currently", now=when)
    # The channel gap still holds (the bot's last reply was 30s ago, gap is 25s), so it may answer.
    assert _replies_for(gate, channel_id=BOT_SPAM, author_id=SUSSY, text="what do the votes look like currently", when=when)


def test_no_more_than_one_reply_per_minute_inside_a_flood():
    gate = ChatGate()
    replies = 0
    for i in range(120):  # two minutes of one message every second, alternating text
        when = NOW + timedelta(seconds=i)
        text = "Kill all clankers" if i % 2 else "Kill all clankersKill all clankers"
        gate.observe(channel_id=BOT_SPAM, author_id=SIEGFRIED, text=text, now=when)
        if _replies_for(gate, channel_id=BOT_SPAM, author_id=SIEGFRIED, text=text, when=when):
            replies += 1
    assert replies <= 2
