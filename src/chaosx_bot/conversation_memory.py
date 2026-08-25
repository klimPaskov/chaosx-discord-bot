"""Lightweight per-channel conversation memory for ChaosX.

Captures guild messages as they arrive, serves a rolling recent-message
window plus a compacted running summary for prompt continuity, and
periodically compacts long conversations (via the public Hermes model) into
an important-facts summary. Tables are created lazily so this module works
with any existing ChaosX database without migrations.

Everything stays dynamic: no manual cache clears, no restarts required —
new messages appear in the window immediately, and compaction runs in the
background when a channel crosses the threshold.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiosqlite

from .hermes_bridge import redact_internal_infrastructure, run_hermes

# Recent raw messages included in the prompt context.
WINDOW_SIZE = 14
# Hard cap of stored raw messages per channel.
MAX_MESSAGES_PER_CHANNEL = 200
# New messages since the last compaction before a compaction is due.
COMPACT_THRESHOLD = 30
# Raw messages kept after compaction so the most recent beats survive.
KEEP_AFTER_COMPACT = 8
# Max chars of the running summary (input and output).
SUMMARY_MAX_CHARS = 1200
# Max chars of a single stored message excerpt.
MESSAGE_EXCERPT_CHARS = 300
# Max number of a user's recent messages in the per-user history block.
USER_HISTORY_LIMIT = 12
# Total chars cap for the per-user history block.
USER_HISTORY_MAX_CHARS = 1600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    author_name TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    message_id INTEGER NOT NULL DEFAULT 0,
    visibility TEXT NOT NULL DEFAULT 'public'
);
CREATE INDEX IF NOT EXISTS idx_conv_messages_channel ON conversation_messages(channel_id, id);
CREATE INDEX IF NOT EXISTS idx_conv_messages_visibility ON conversation_messages(channel_id, visibility, id);
CREATE TABLE IF NOT EXISTS conversation_summaries (
    channel_id INTEGER NOT NULL,
    scope TEXT NOT NULL DEFAULT 'public',
    summary TEXT NOT NULL,
    last_message_id INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel_id, scope)
);
"""

_COMPACT_PROMPT = (
    "Compress this Discord conversation into a short running memory summary "
    "(maximum 150 words). Keep: decisions, concrete facts about the Chaos Redux "
    "project/mod, user preferences, requests, and named entities. Drop: greetings, "
    "banter, repetition, and trivia. If an existing summary is provided, merge it in "
    "and keep only what still matters. Output ONLY the new summary text."
)


async def _migrate_schema(db: aiosqlite.Connection) -> None:
    """Add visibility/message_id columns and scope-aware summaries in place.

    Existing rows default to 'public' visibility and legacy summaries are
    preserved as the admin scope (they were compacted from all messages).
    """
    cols = {row[1] for row in await (await db.execute("PRAGMA table_info(conversation_messages)")).fetchall()}
    if "visibility" not in cols:
        await db.execute("ALTER TABLE conversation_messages ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public'")
    if "message_id" not in cols:
        await db.execute("ALTER TABLE conversation_messages ADD COLUMN message_id INTEGER NOT NULL DEFAULT 0")
    summary_cols = {row[1] for row in await (await db.execute("PRAGMA table_info(conversation_summaries)")).fetchall()}
    if "scope" not in summary_cols:
        await db.execute("ALTER TABLE conversation_summaries RENAME TO conversation_summaries_legacy")
        await db.executescript(_SCHEMA)
        await db.execute(
            "INSERT INTO conversation_summaries (channel_id, scope, summary, last_message_id, updated_at) "
            "SELECT channel_id, 'admin', summary, last_message_id, updated_at FROM conversation_summaries_legacy"
        )
        await db.execute("DROP TABLE conversation_summaries_legacy")


async def _ensure_schema(db_path: Path) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await _migrate_schema(db)
        await db.commit()


def _excerpt(content: str, limit: int = MESSAGE_EXCERPT_CHARS) -> str:
    text = (content or "").strip().replace("\n", " ")
    return text[:limit]


async def capture_message(
    db_path: Path,
    *,
    guild_id: int | None,
    channel_id: int,
    author_id: int,
    author_name: str,
    content: str,
    created_at: str,
    is_bot_self: bool,
    allowed_guild_id: int | None,
    message_id: int = 0,
    visibility: str = "public",
) -> None:
    """Store one guild message for conversation context.

    Skips DMs, other bots, slash commands, empty content, and any guild the
    bot is not allowed in. The bot's own replies are captured (they are part
    of the conversation). ``visibility`` separates public channel history
    from owner/admin task history: public asks read only 'public' rows,
    while the admin path reads everything.
    """
    if guild_id is None or (allowed_guild_id and guild_id != allowed_guild_id):
        return
    if not is_bot_self and author_id in (0,):
        return
    text = (content or "").strip()
    if not text or text.startswith("/"):
        return
    try:
        await _ensure_schema(db_path)
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO conversation_messages (channel_id, author_id, author_name, content, created_at, message_id, visibility) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (channel_id, author_id, author_name[:64], text, created_at, message_id, visibility),
            )
            await db.execute(
                "DELETE FROM conversation_messages WHERE channel_id = ? AND id NOT IN ("
                "SELECT id FROM conversation_messages WHERE channel_id = ? ORDER BY id DESC LIMIT ?"
                ")",
                (channel_id, channel_id, MAX_MESSAGES_PER_CHANNEL),
            )
            await db.commit()
    except Exception:
        # Memory capture must never break message handling.
        return


async def mark_messages_admin(db_path: Path, message_ids: list[int]) -> None:
    """Upgrade captured rows to admin visibility so they stay out of public history."""
    ids = [int(mid) for mid in message_ids if mid]
    if not ids:
        return
    try:
        await _ensure_schema(db_path)
        async with aiosqlite.connect(db_path) as db:
            for mid in ids:
                await db.execute(
                    "UPDATE conversation_messages SET visibility = 'admin' WHERE message_id = ?",
                    (mid,),
                )
            await db.commit()
    except Exception:
        return


async def conversation_context_for(
    db_path: Path,
    channel_id: int,
    *,
    exclude_message_id: int | None = None,
    window: int = WINDOW_SIZE,
    scope: str = "public",
) -> str:
    """Build the continuity block: compacted summary + recent raw messages.

    ``scope`` selects the memory partition: 'public' reads only public
    history (no owner/admin task messages), 'admin' reads everything.
    """
    visibility_filter = "" if scope == "admin" else "AND visibility = 'public'"
    try:
        await _ensure_schema(db_path)
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            summary_row = await (
                await db.execute(
                    "SELECT summary FROM conversation_summaries WHERE channel_id = ? AND scope = ?",
                    (channel_id, scope),
                )
            ).fetchone()
            rows = await (
                await db.execute(
                    f"SELECT id, author_name, content FROM conversation_messages "
                    f"WHERE channel_id = ? {visibility_filter} ORDER BY id DESC LIMIT ?",
                    (channel_id, window + 1),
                )
            ).fetchall()
    except Exception:
        return ""

    parts: list[str] = []
    if summary_row is not None and str(summary_row["summary"]).strip():
        parts.append(
            "Channel memory (compacted summary):\n"
            + redact_internal_infrastructure(str(summary_row["summary"]).strip())
        )
    kept = [row for row in reversed(rows) if row["id"] != exclude_message_id]
    if kept:
        lines = [
            f"- {row['author_name']}: {redact_internal_infrastructure(_excerpt(row['content']))}"
            for row in kept[:window]
        ]
        parts.append("Recent conversation:\n" + "\n".join(lines))
    return "\n\n".join(parts)


async def user_history_for(
    db_path: Path,
    author_id: int,
    *,
    exclude_message_id: int | None = None,
    limit: int = USER_HISTORY_LIMIT,
    scope: str = "public",
) -> str:
    """Build a per-user history block: the author's recent captured messages.

    Public scope reads only public history (no owner/admin task messages).
    Returns a prompt-ready reference block ('' when nothing is available).
    """
    visibility_filter = "" if scope == "admin" else "AND visibility = 'public'"
    try:
        await _ensure_schema(db_path)
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    f"SELECT id, author_name, content FROM conversation_messages "
                    f"WHERE author_id = ? {visibility_filter} ORDER BY id DESC LIMIT ?",
                    (author_id, limit + 1),
                )
            ).fetchall()
    except Exception:
        return ""
    kept = [row for row in list(rows)[::-1] if row["id"] != exclude_message_id]
    if not kept:
        return ""
    lines: list[str] = []
    total = 0
    for row in kept[:limit]:
        excerpt = redact_internal_infrastructure(_excerpt(row["content"]))
        line = f"- {row['author_name']}: {excerpt}"
        total += len(line) + 1
        if total > USER_HISTORY_MAX_CHARS:
            break
        lines.append(line)
    return "This user's recent messages (captured history):\n" + "\n".join(lines)


async def known_authors_for(db_path: Path, *, limit: int = 60, scope: str = "public") -> dict[int, str]:
    """Map user IDs to their latest display names from captured messages.

    Public scope reads only public history. Used to build the user directory
    so the bot can name users (never ping them) when asked who said something.
    """
    visibility_filter = "" if scope == "admin" else "AND visibility = 'public'"
    mapping: dict[int, str] = {}
    try:
        await _ensure_schema(db_path)
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    f"SELECT author_id, author_name, MAX(id) AS last_id FROM conversation_messages "
                    f"WHERE author_id != 0 {visibility_filter} GROUP BY author_id "
                    f"ORDER BY last_id DESC LIMIT ?",
                    (limit,),
                )
            ).fetchall()
        for row in rows:
            name = str(row["author_name"] or "").strip()
            if name:
                mapping[int(row["author_id"])] = name
    except Exception:
        return {}
    return mapping


async def compact_if_due(
    db_path: Path,
    channel_id: int,
    *,
    summarize: Callable[[str], Awaitable[str]],
    threshold: int = COMPACT_THRESHOLD,
    keep_after_compact: int = KEEP_AFTER_COMPACT,
    scope: str = "public",
) -> bool:
    """Compress a channel's conversation once enough new messages arrived.

    ``summarize`` receives the full compact prompt and must return the new
    summary text (or "" on failure). Returns True when compaction ran.
    ``scope`` compacts the matching memory partition ('public' or 'admin').
    """
    visibility_filter = "" if scope == "admin" else "AND visibility = 'public'"
    try:
        await _ensure_schema(db_path)
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            summary_row = await (
                await db.execute(
                    "SELECT summary, last_message_id FROM conversation_summaries WHERE channel_id = ? AND scope = ?",
                    (channel_id, scope),
                )
            ).fetchone()
            since = summary_row["last_message_id"] if summary_row else 0
            count_row = await (
                await db.execute(
                    f"SELECT COUNT(*) AS n FROM conversation_messages WHERE channel_id = ? AND id > ? {visibility_filter}",
                    (channel_id, since),
                )
            ).fetchone()
            if count_row["n"] < threshold:
                return False
            rows = await (
                await db.execute(
                    f"SELECT author_name, content FROM conversation_messages "
                    f"WHERE channel_id = ? AND id > ? {visibility_filter} ORDER BY id ASC LIMIT ?",
                    (channel_id, since, 60),
                )
            ).fetchall()
            last_row = await (
                await db.execute(
                    "SELECT MAX(id) AS m FROM conversation_messages WHERE channel_id = ?",
                    (channel_id,),
                )
            ).fetchone()
            last_id = int(last_row["m"] or 0) if last_row is not None else 0
            old_summary = str(summary_row["summary"]) if summary_row is not None else ""
    except Exception:
        return False
    if not rows:
        return False

    transcript = "\n".join(f"- {r['author_name']}: {_excerpt(r['content'])}" for r in rows)
    existing = f"\nExisting summary:\n{old_summary[:SUMMARY_MAX_CHARS]}" if old_summary else ""
    prompt = f"{_COMPACT_PROMPT}{existing}\n\nConversation transcript:\n{transcript[:4000]}"
    try:
        new_summary = (await summarize(prompt)).strip()
    except Exception:
        return False
    if not new_summary:
        return False

    now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO conversation_summaries (channel_id, scope, summary, last_message_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(channel_id, scope) DO UPDATE SET summary = excluded.summary, "
                "last_message_id = excluded.last_message_id, updated_at = excluded.updated_at",
                (channel_id, scope, new_summary[:SUMMARY_MAX_CHARS], last_id, now),
            )
            if scope == "admin":
                await db.execute(
                    "DELETE FROM conversation_messages WHERE channel_id = ? AND id NOT IN ("
                    "SELECT id FROM conversation_messages WHERE channel_id = ? ORDER BY id DESC LIMIT ?"
                    ")",
                    (channel_id, channel_id, keep_after_compact),
                )
            else:
                await db.execute(
                    "DELETE FROM conversation_messages WHERE channel_id = ? AND visibility = 'public' AND id NOT IN ("
                    "SELECT id FROM conversation_messages WHERE channel_id = ? AND visibility = 'public' ORDER BY id DESC LIMIT ?"
                    ")",
                    (channel_id, channel_id, keep_after_compact),
                )
            await db.commit()
    except Exception:
        return False
    return True


async def run_compaction_if_due(settings: Any, channel_id: int | None, scope: str = "public") -> bool:
    """Background compaction glue: summarize via the same public Hermes bridge."""
    if channel_id is None:
        return False

    async def _summarize(prompt: str) -> str:
        from .bot import sanitize_public_ask_output  # lazy: bot imports this module

        result = await run_hermes(
            hermes_bin=settings.hermes_bin,
            profile=settings.hermes_profile,
            repo=settings.chaos_redux_repo,
            prompt=prompt,
            timeout_seconds=getattr(settings, "hermes_timeout_seconds", 300),
            model=getattr(settings, "ask_model", None),
            provider=getattr(settings, "ask_provider", None),
            reasoning_effort=getattr(settings, "ask_reasoning_effort", None),
            toolsets="safe",
            ignore_rules=True,
            activity_label=f"conversation compact ({scope})",
            actor_id=0,
        )
        if not result.ok:
            return ""
        return sanitize_public_ask_output(result.stdout.strip())

    return await compact_if_due(settings.db_path, channel_id, summarize=_summarize, scope=scope)


def schedule_compaction(settings: Any, channel_id: int | None, scope: str = "public") -> None:
    """Fire-and-forget compaction; never raises into the event loop."""

    async def _run() -> None:
        try:
            await run_compaction_if_due(settings, channel_id, scope=scope)
        except Exception:
            return

    try:
        asyncio.create_task(_run(), name="chaosx-conversation-compact")
    except RuntimeError:
        # No running event loop (tests); compaction simply won't fire.
        return
