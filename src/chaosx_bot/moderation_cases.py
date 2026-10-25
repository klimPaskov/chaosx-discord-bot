"""Evidence kept before a destructive action.

Hoops, 2026-10-25: purging the channel also destroys the evidence of who did what. So the messages about to be
deleted are written down first — ids, authors, timestamps, content and a jump link — and the purge reports the
case number. Nothing here is a backup of the channel; it is the evidence for the decision.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MAX_SNAPSHOT_MESSAGES = 2000
MAX_CONTENT_CHARS = 500


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS moderation_cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            actor_id INTEGER NOT NULL,
            guild_id INTEGER,
            channel_id INTEGER,
            action TEXT NOT NULL,
            summary TEXT NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0,
            messages_json TEXT NOT NULL DEFAULT '[]'
        )
        """
    )
    return conn


def save_case(
    db_path: str | Path,
    *,
    actor_id: int,
    guild_id: int | None,
    channel_id: int | None,
    action: str,
    summary: str,
    messages: list[dict],
) -> int:
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO moderation_cases (created_at, actor_id, guild_id, channel_id, action, summary, message_count, messages_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _iso_now(),
                int(actor_id),
                guild_id,
                channel_id,
                action,
                summary[:400],
                len(messages),
                json.dumps(messages, ensure_ascii=False)[:8_000_000],
            ),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


async def snapshot_messages(channel, *, after=None, limit: int = MAX_SNAPSHOT_MESSAGES) -> list[dict]:
    """Read what is about to disappear. Best effort: a permission error yields an empty list, never a blocker."""
    rows: list[dict] = []
    guild_id = getattr(getattr(channel, "guild", None), "id", None)
    try:
        kwargs: dict = {"limit": limit, "oldest_first": False}
        if after is not None:
            kwargs["after"] = after
        async for message in channel.history(**kwargs):
            author = getattr(message, "author", None)
            rows.append(
                {
                    "id": str(message.id),
                    "author": str(getattr(author, "display_name", None) or getattr(author, "name", "unknown")),
                    "author_id": str(getattr(author, "id", "")),
                    "created_at": (
                        message.created_at.isoformat() if getattr(message, "created_at", None) is not None else ""
                    ),
                    "content": (message.content or "")[:MAX_CONTENT_CHARS],
                    "jump": (
                        f"https://discord.com/channels/{guild_id}/{channel.id}/{message.id}" if guild_id else ""
                    ),
                }
            )
    except Exception:
        return rows
    return rows


def case_summary(case_id: int, count: int) -> str:
    return f"case #{case_id} kept {count} message(s) as evidence"


def latest_cases(db_path: str | Path, *, limit: int = 5) -> list[tuple[int, str, str, int, str]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, created_at, action, message_count, summary FROM moderation_cases ORDER BY id DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
    finally:
        conn.close()
    return [(int(r[0]), str(r[1]), str(r[2]), int(r[3]), str(r[4])) for r in rows]


def case_messages(db_path: str | Path, case_id: int) -> list[dict]:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT messages_json FROM moderation_cases WHERE id = ?", (int(case_id),)).fetchone()
    finally:
        conn.close()
    if not row:
        return []
    try:
        return json.loads(row[0] or "[]")
    except json.JSONDecodeError:
        return []
