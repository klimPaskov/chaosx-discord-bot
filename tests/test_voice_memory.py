"""Voice memory: the same store as text chat, in both directions.

Covers the two long-standing bugs this work exposed — memory blocks that kept the oldest messages and
trimmed the newest away — plus the voice recorder, the session transcript and frontmatter stripping.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from chaosx_bot.conversation_memory import (
    capture_message,
    conversation_context_for,
    record_voice_exchange,
    server_memory_for,
    user_history_for,
)
from chaosx_bot.indexer import strip_frontmatter
from chaosx_bot.voice_chat import VoiceChatSession

STAMP = "2026-09-27T12:00:00+00:00"


def run(coro):
    return asyncio.run(coro)


def _capture(db_path: Path, *, channel_id: int, index: int, author_id: int = 42) -> None:
    return capture_message(
        db_path,
        guild_id=1,
        channel_id=channel_id,
        author_id=author_id,
        author_name="Hoops",
        content=f"message number {index}",
        created_at=STAMP,
        is_bot_self=False,
        allowed_guild_id=1,
        message_id=1000 + index,
    )


def test_user_history_keeps_the_newest_messages(tmp_path: Path) -> None:
    """A busy member's memory must show what they just said, not their oldest lines."""
    db = tmp_path / "memory.db"

    async def scenario() -> str:
        for index in range(40):
            await _capture(db, channel_id=5, index=index)
        return await user_history_for(db, 42, scope="public")

    block = run(scenario())
    assert "message number 39" in block
    assert "message number 0" not in block


def test_channel_memory_keeps_the_newest_window(tmp_path: Path) -> None:
    """Channel continuity is about the recent conversation, so the newest lines survive the trim."""
    db = tmp_path / "memory.db"

    async def scenario() -> str:
        for index in range(30):
            await _capture(db, channel_id=7, index=index)
        return await conversation_context_for(db, 7, scope="public")

    block = run(scenario())
    assert "message number 29" in block
    assert "message number 0" not in block


def test_spoken_exchange_is_archived_and_readable(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"

    async def scenario() -> tuple[str, str]:
        await record_voice_exchange(
            db,
            guild_id=1,
            channel_id=9,
            user_id=42,
            user_name="Hoops",
            question="what are the zombie super events",
            answer="Zombie Apocalypse comes first.",
            allowed_guild_id=1,
            bot_user_id=77,
            created_at=STAMP,
        )
        return (
            await conversation_context_for(db, 9, scope="public"),
            await user_history_for(db, 42, scope="public"),
        )

    channel, speaker = run(scenario())
    assert "[voice]" in channel and "zombie super events" in channel
    assert "zombie super events" in speaker


def test_spoken_exchange_can_be_filed_in_a_shared_channel(tmp_path: Path) -> None:
    """With a shared memory channel configured, a text ask there sees what was said out loud."""
    db = tmp_path / "memory.db"

    async def scenario() -> str:
        await record_voice_exchange(
            db,
            guild_id=1,
            channel_id=9,
            user_id=42,
            user_name="Hoops",
            question="what is the black plague event",
            answer="The Rat King's coronation.",
            allowed_guild_id=1,
            bot_user_id=77,
            created_at=STAMP,
            memory_channel_id=1000,
        )
        return await conversation_context_for(db, 1000, scope="public")

    assert "black plague event" in run(scenario())


def test_frontmatter_is_not_indexed() -> None:
    raw = "---\ntitle: Videos\ntags: [tester, tutorial]\n---\n# Body\nInstalling Chaos Redux is a clone away.\n"
    cleaned = strip_frontmatter(raw)
    assert "tags:" not in cleaned
    assert "Installing Chaos Redux" in cleaned


def test_session_transcript_is_bounded_and_ordered() -> None:
    session = VoiceChatSession.__new__(VoiceChatSession)
    session._history = []
    assert session.history_block() == ""
    for index in range(10):
        session._history.append((f"question {index}", f"answer {index}"))
    block = session.history_block()
    assert "question 9" in block and "question 0" not in block
    assert len(block) <= 900


def test_session_archives_through_the_recorder() -> None:
    session = VoiceChatSession.__new__(VoiceChatSession)
    session._history = []
    calls: list[tuple[int, str, str]] = []

    async def recorder(user_id: int, question: str, answer: str) -> None:
        calls.append((user_id, question, answer))

    session._memory_recorder = recorder
    run(session.remember("what is the zombie outbreak", "A super event.", user_id=5))
    assert calls == [(5, "what is the zombie outbreak", "A super event.")]

    async def broken(user_id: int, question: str, answer: str) -> None:
        raise RuntimeError("database unavailable")

    session._memory_recorder = broken
    run(session.remember("still answered", "yes", user_id=5))
    assert session._history[-1][0] == "still answered"


def test_server_memory_covers_other_channels(tmp_path: Path) -> None:
    """Both voice and text should know what was said elsewhere in the server."""
    db = tmp_path / "memory.db"

    async def scenario() -> str:
        await _capture(db, channel_id=7, index=1)
        await _capture(db, channel_id=8, index=2)
        return await server_memory_for(db, channel_names={8: "general"}, skip_channel_ids={7})

    block = run(scenario())
    assert "general" in block
    assert "message number 2" in block
    assert "message number 1" not in block  # the channel supplied in full is not repeated


def test_server_memory_never_includes_admin_rows(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"

    async def scenario() -> str:
        await capture_message(
            db, guild_id=1, channel_id=8, author_id=42, author_name="Hoops",
            content="owner task secret", created_at=STAMP, is_bot_self=False,
            allowed_guild_id=1, message_id=777, visibility="admin",
        )
        return await server_memory_for(db, channel_names={8: "general"})

    assert "owner task secret" not in run(scenario())


def test_server_memory_is_empty_on_an_empty_archive(tmp_path: Path) -> None:
    assert run(server_memory_for(tmp_path / "empty.db")) == ""
