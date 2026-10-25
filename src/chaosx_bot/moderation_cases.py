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


def _actions_conn(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS moderation_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            rule TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            actor_id INTEGER,
            dry_run INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    return conn


def record_action(
    db_path: str | Path,
    *,
    user_id: int,
    action: str,
    rule: str = "",
    reason: str = "",
    actor_id: int | None = None,
    dry_run: bool = False,
) -> int:
    conn = _actions_conn(db_path)
    try:
        cursor = conn.execute(
            "INSERT INTO moderation_actions (created_at, user_id, action, rule, reason, actor_id, dry_run) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_iso_now(), int(user_id), action, rule, reason[:300], actor_id, 1 if dry_run else 0),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def warning_count(db_path: str | Path, user_id: int, *, days: int = 90) -> int:
    """How many real (non-dry-run) warnings this member has on record inside the window."""
    from datetime import datetime, timedelta, timezone

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    conn = _actions_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM moderation_actions WHERE user_id = ? AND action = 'warn' "
            "AND dry_run = 0 AND created_at >= ?",
            (int(user_id), since),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0] or 0)


def is_established(db_path: str | Path, user_id: int, *, min_messages: int = 40) -> bool:
    """A member with real history gets the benefit of owner approval before a ban.

    Hoops, 2026-10-25: "New members should usually be less forgiven." Accounts with little history are the ones
    that spam, so they are banned autonomously; only members who have actually taken part wait for the owner."""
    conn = sqlite3.connect(str(db_path), timeout=15)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM message_archive WHERE author_id = ? AND visibility = 'public'",
            (int(user_id),),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0] or 0) >= min_messages


def recent_actions(db_path: str | Path, *, limit: int = 10) -> list[tuple[str, int, str, str, int]]:
    conn = _actions_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT created_at, user_id, action, rule, dry_run FROM moderation_actions ORDER BY id DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
    finally:
        conn.close()
    return [(str(r[0]), int(r[1]), str(r[2]), str(r[3]), int(r[4])) for r in rows]
