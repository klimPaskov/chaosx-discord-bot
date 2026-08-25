from __future__ import annotations

import asyncio
from pathlib import Path

from chaosx_bot.conversation_memory import (
    KEEP_AFTER_COMPACT,
    MAX_MESSAGES_PER_CHANNEL,
    USER_PROFILE_COMPACT_THRESHOLD,
    backfill_capture,
    capture_message,
    compact_if_due,
    compact_user_profile_if_due,
    conversation_context_for,
    known_authors_for,
    user_history_for,
    user_profile_for,
)

GUILD = 1001
CHANNEL = 2001
ALLOWED = 1001


def _iso(day: int) -> str:
    return f"2026-08-{day:02d}T10:00:00+00:00"


async def _capture(db: Path, *, n: int = 1, channel: int = CHANNEL, content: str | None = None, author: str = "Hoops") -> None:
    for i in range(n):
        await capture_message(
            db,
            guild_id=GUILD,
            channel_id=channel,
            author_id=3000 + i,
            author_name=author,
            content=content or f"message {i}",
            created_at=_iso(i + 1),
            is_bot_self=False,
            allowed_guild_id=ALLOWED,
        )


def test_user_history_for_returns_public_messages_newest_first(tmp_path: Path) -> None:
    db = tmp_path / "mem.db"

    async def run() -> None:
        await _capture(db, n=3, author="zin")
        # An admin-scope (owner task) message must NOT leak into public history.
        await capture_message(
            db,
            guild_id=GUILD,
            channel_id=CHANNEL,
            author_id=3000,
            author_name="zin",
            content="admin task: rebuild index",
            created_at=_iso(9),
            is_bot_self=False,
            allowed_guild_id=ALLOWED,
            visibility="admin",
        )
        block = await user_history_for(db, author_id=3000, scope="public")
        assert "This user's recent messages (captured history):" in block
        assert "message 0" in block
        assert "message 2" not in block  # message 2 belongs to author 3002
        assert "admin task" not in block
        # exclude_message_id drops the current message from the block.
        other = await user_history_for(db, author_id=3002, scope="public")
        assert "message 2" in other
        assert "admin task" not in other

    asyncio.run(run())


def test_known_authors_for_maps_latest_display_names(tmp_path: Path) -> None:
    db = tmp_path / "mem.db"

    async def run() -> None:
        await _capture(db, n=2, author="Holly")
        # A different user in another channel is included too.
        await capture_message(
            db,
            guild_id=GUILD,
            channel_id=CHANNEL + 1,
            author_id=4001,
            author_name="Maverick",
            content="hi",
            created_at=_iso(1),
            is_bot_self=False,
            allowed_guild_id=ALLOWED,
        )
        # Admin-scope rows must not leak into the public directory.
        await capture_message(
            db,
            guild_id=GUILD,
            channel_id=CHANNEL,
            author_id=5000,
            author_name="zin",
            content="admin task",
            created_at=_iso(2),
            is_bot_self=False,
            allowed_guild_id=ALLOWED,
            visibility="admin",
        )
        mapping = await known_authors_for(db, scope="public")
        assert 4001 in mapping and mapping[4001] == "Maverick"
        assert 5000 not in mapping  # admin rows stay out of the public directory

    asyncio.run(run())


def test_user_profile_compacts_and_personalizes(tmp_path: Path) -> None:
    db = tmp_path / "mem.db"

    async def run() -> None:
        # Below threshold: no profile built yet.
        await _capture(db, n=3, author="Holly")
        assert await compact_user_profile_if_due(db, 3000, summarize=_fake_profile_summarize) is False
        assert await user_profile_for(db, 3000) == ""

        # Cross the threshold (default 25) with real messages.
        for i in range(USER_PROFILE_COMPACT_THRESHOLD):
            await capture_message(
                db,
                guild_id=GUILD,
                channel_id=CHANNEL,
                author_id=3000,
                author_name="Holly",
                content=f"suggestion {i}: more {('railway guns' if i % 2 else 'penguin states')}",
                created_at=_iso(10 + i),
                is_bot_self=False,
                allowed_guild_id=ALLOWED,
            )
        assert await compact_user_profile_if_due(db, 3000, summarize=_fake_profile_summarize) is True
        block = await user_profile_for(db, 3000)
        assert "User profile for Holly" in block
        assert "penguin states" in block or "railway guns" in block

    asyncio.run(run())


async def _fake_profile_summarize(prompt: str) -> str:
    # Pulls the concrete facts out of the transcript for the assertion.
    if "penguin states" in prompt:
        return "Prefers penguin states; suggested more of them."
    return "Suggested railway guns."


def test_capture_skips_non_guild_slash_other_bots(tmp_path: Path) -> None:
    db = tmp_path / "mem.db"

    async def run() -> None:
        await capture_message(db, guild_id=None, channel_id=1, author_id=1, author_name="x", content="hi", created_at=_iso(1), is_bot_self=False, allowed_guild_id=ALLOWED)
        await capture_message(db, guild_id=GUILD, channel_id=2, author_id=1, author_name="x", content="hi", created_at=_iso(1), is_bot_self=False, allowed_guild_id=ALLOWED)
        await capture_message(db, guild_id=9999, channel_id=3, author_id=1, author_name="x", content="hi", created_at=_iso(1), is_bot_self=False, allowed_guild_id=ALLOWED)
        await capture_message(db, guild_id=GUILD, channel_id=4, author_id=1, author_name="x", content="/event 7", created_at=_iso(1), is_bot_self=False, allowed_guild_id=ALLOWED)
        await capture_message(db, guild_id=GUILD, channel_id=5, author_id=1, author_name="OtherBot", content="hi", created_at=_iso(1), is_bot_self=True, allowed_guild_id=ALLOWED)
        ctx = await conversation_context_for(db, CHANNEL)
    asyncio.run(run())
    ctx = asyncio.run(conversation_context_for(db, CHANNEL))
    assert "message" not in ctx  # nothing from CHANNEL was captured


def test_visibility_partitions_public_and_admin_history(tmp_path: Path) -> None:
    """Public context never shows admin-task messages; admin sees everything."""
    db = tmp_path / "mem.db"

    async def run() -> None:
        await capture_message(
            db,
            guild_id=GUILD,
            channel_id=CHANNEL,
            author_id=4001,
            author_name="Regular",
            content="What is the Fury event?",
            created_at=_iso(1),
            is_bot_self=False,
            allowed_guild_id=ALLOWED,
            message_id=111,
            visibility="public",
        )
        await capture_message(
            db,
            guild_id=GUILD,
            channel_id=CHANNEL,
            author_id=3001,
            author_name="Hoops",
            content="admin: delete those messages",
            created_at=_iso(2),
            is_bot_self=False,
            allowed_guild_id=ALLOWED,
            message_id=222,
            visibility="admin",
        )
        await capture_message(
            db,
            guild_id=GUILD,
            channel_id=CHANNEL,
            author_id=7777,
            author_name="ChaosX",
            content="Done, removed 3 messages.",
            created_at=_iso(3),
            is_bot_self=True,
            allowed_guild_id=ALLOWED,
            message_id=333,
            visibility="admin",
        )
        public_ctx = await conversation_context_for(db, CHANNEL, scope="public")
        admin_ctx = await conversation_context_for(db, CHANNEL, scope="admin")
        assert "Fury event" in public_ctx
        assert "delete those messages" not in public_ctx
        assert "removed 3 messages" not in public_ctx
        assert "Fury event" in admin_ctx
        assert "delete those messages" in admin_ctx
        assert "removed 3 messages" in admin_ctx

    asyncio.run(run())


def test_mark_messages_admin_moves_rows_out_of_public_scope(tmp_path: Path) -> None:
    db = tmp_path / "mem.db"

    async def run() -> None:
        await capture_message(
            db,
            guild_id=GUILD,
            channel_id=CHANNEL,
            author_id=4002,
            author_name="Hoops",
            content="@ChaosX check the deleted messages",
            created_at=_iso(1),
            is_bot_self=False,
            allowed_guild_id=ALLOWED,
            message_id=444,
            visibility="public",  # captured as public, upgraded later like the admin path does
        )
        from chaosx_bot.conversation_memory import mark_messages_admin
        await mark_messages_admin(db, [444])
        public_ctx = await conversation_context_for(db, CHANNEL, scope="public")
        admin_ctx = await conversation_context_for(db, CHANNEL, scope="admin")
        assert "deleted messages" not in public_ctx
        assert "deleted messages" in admin_ctx

    asyncio.run(run())


def test_context_includes_summary_and_window_excludes_trigger(tmp_path: Path) -> None:
    db = tmp_path / "mem.db"
    asyncio.run(_capture(db, n=3))

    async def run() -> None:
        # Messages get ids 1,2,3; excluding id 2 drops the "message 1" row.
        ctx = await conversation_context_for(db, CHANNEL, exclude_message_id=2)
        assert "message 0" in ctx and "message 2" in ctx
        assert "message 1" not in ctx
        assert "Hoops" in ctx

        # A stored summary is included as the compacted memory block.
        from chaosx_bot.conversation_memory import _SCHEMA
        import aiosqlite
        async with aiosqlite.connect(db) as conn:
            await conn.execute(
                "INSERT INTO conversation_summaries (channel_id, summary, last_message_id, updated_at) VALUES (?, ?, ?, ?)",
                (CHANNEL, "Hoops is testing conversation memory.", 3, _iso(4)),
            )
            await conn.commit()
        ctx2 = await conversation_context_for(db, CHANNEL)
        # Internal-infrastructure phrasing is redacted even inside stored context.
        assert "Channel memory" in ctx2 and "testing prior context" in ctx2
        assert "conversation memory" not in ctx2
    asyncio.run(run())


def test_capture_is_capped_per_channel(tmp_path: Path) -> None:
    db = tmp_path / "mem.db"
    asyncio.run(_capture(db, n=MAX_MESSAGES_PER_CHANNEL + 10))

    async def run() -> None:
        ctx = await conversation_context_for(db, CHANNEL)
        lines = [l for l in ctx.splitlines() if l.startswith("- ")]
        assert len(lines) == 14  # window, not the full cap
        import aiosqlite
        async with aiosqlite.connect(db) as conn:
            row = await (await conn.execute("SELECT COUNT(*) FROM conversation_messages WHERE channel_id = ?", (CHANNEL,))).fetchone()
        count = int(row[0]) if row is not None else 0
        assert count == MAX_MESSAGES_PER_CHANNEL
    asyncio.run(run())


def test_compaction_runs_at_threshold_and_prunes(tmp_path: Path) -> None:
    db = tmp_path / "mem.db"
    asyncio.run(_capture(db, n=30))  # threshold is 30

    calls: list[str] = []
    async def summarize(prompt: str) -> str:
        calls.append(prompt)
        return "Compacted: 30 messages discussed event 7 and the zombie outbreak."
    ran = asyncio.run(compact_if_due(db, CHANNEL, summarize=summarize))
    assert ran is True
    assert len(calls) == 1
    assert "Compress this Discord conversation" in calls[0]

    async def run() -> None:
        import aiosqlite
        async with aiosqlite.connect(db) as conn:
            count_row = await (await conn.execute("SELECT COUNT(*) FROM conversation_messages WHERE channel_id = ?", (CHANNEL,))).fetchone()
            summary_row = await (await conn.execute("SELECT summary FROM conversation_summaries WHERE channel_id = ?", (CHANNEL,))).fetchone()
        count = int(count_row[0]) if count_row is not None else 0
        summary = str(summary_row[0]) if summary_row is not None else ""
        ctx = await conversation_context_for(db, CHANNEL)
        assert count == KEEP_AFTER_COMPACT
        assert "Compacted: 30 messages" in summary
        assert "Channel memory" in ctx
        # Below the new threshold again -> no second compaction.
        ran2 = await compact_if_due(db, CHANNEL, summarize=summarize)
        assert ran2 is False
    asyncio.run(run())


def test_prompt_builders_include_conversation_context(tmp_path: Path) -> None:
    from chaosx_bot.hermes_bridge import (
        build_auto_scan_answer_prompt,
        build_auto_scan_banter_prompt,
        build_public_prompt,
    )

    answer = build_auto_scan_answer_prompt(
        user_message="hi chaos bot",
        guild_name="Chaos Redux",
        channel_name="general",
        reference_context="event 7",
        gate_reason="bot-topic conversation",
        conversation_context="Recent conversation:\n- Hoops: hello",
    )
    assert "Recent channel conversation" in answer and "Hoops: hello" in answer

    banter = build_auto_scan_banter_prompt(
        user_message="chaosx?",
        guild_name="Chaos Redux",
        channel_name="general",
        gate_reason="bot-topic conversation",
        conversation_context="Recent conversation:\n- Hoops: hi",
    )
    assert "Hoops: hi" in banter

    public = build_public_prompt(
        user_request="how does event 7 work?",
        guild_name="Chaos Redux",
        channel_name="general",
        conversation_context="Recent conversation:\n- Hoops: earlier we talked about zombies",
    )
    assert "earlier we talked about zombies" in public

    empty = build_auto_scan_banter_prompt(
        user_message="chaosx?",
        guild_name="Chaos Redux",
        channel_name="general",
        gate_reason="bot-topic conversation",
    )
    assert "Recent channel conversation" not in empty


def test_backfill_capture_dedupes_and_force_profiles(tmp_path: Path) -> None:
    db = tmp_path / "backfill.db"

    async def run() -> None:
        # Same message id captured twice -> stored once.
        for _ in range(2):
            await backfill_capture(
                db,
                guild_id=GUILD,
                channel_id=CHANNEL,
                author_id=3001,
                author_name="Mira",
                content="I think fury should be rebalanced.",
                created_at=_iso(1),
                message_id=900001,
                allowed_guild_id=ALLOWED,
            )
        history = await user_history_for(db, 3001)
        assert history.count("rebalanced") == 1

        # Below the normal threshold, force=True still builds a profile.
        assert await compact_user_profile_if_due(db, 3001, summarize=_fake_profile_summarize) is False
        assert await compact_user_profile_if_due(db, 3001, summarize=_fake_profile_summarize, force=True) is True
        block = await user_profile_for(db, 3001)
        assert "Mira" in block

        # Zero message id rows are skipped entirely (no real Discord id).
        await backfill_capture(
            db,
            guild_id=GUILD,
            channel_id=CHANNEL,
            author_id=3002,
            author_name="Skip",
            content="should not appear",
            created_at=_iso(2),
            message_id=0,
            allowed_guild_id=ALLOWED,
        )
        assert await user_history_for(db, 3002) == ""

    asyncio.run(run())


def test_old_schema_db_migrates_on_capture(tmp_path: Path) -> None:
    """DBs created before the visibility/message_id columns must still capture.

    Regression for the live failure where the schema script's index on
    ``visibility`` raised ``no such column`` on legacy DBs, so every
    capture silently dropped and user profiles were never built.
    """
    import sqlite3

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE conversation_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            author_id INTEGER NOT NULL,
            author_name TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            message_id INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_conv_messages_channel ON conversation_messages(channel_id, id);
        """
    )
    conn.commit()
    conn.close()

    async def run() -> None:
        await backfill_capture(
            db,
            guild_id=GUILD,
            channel_id=CHANNEL,
            author_id=3003,
            author_name="Legacy",
            content="captured despite old schema",
            created_at=_iso(3),
            message_id=900002,
            allowed_guild_id=ALLOWED,
        )
        assert "captured despite old schema" in await user_history_for(db, 3003)
        assert await compact_user_profile_if_due(db, 3003, summarize=_fake_profile_summarize, force=True) is True

    asyncio.run(run())
