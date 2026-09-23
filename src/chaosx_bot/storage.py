from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from .activity import BONUS_DAILY_CAP, parse_day, tier_for_xp

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    actor_id INTEGER NOT NULL,
    guild_id INTEGER,
    channel_id INTEGER,
    command TEXT NOT NULL,
    summary TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hermes_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    actor_id INTEGER NOT NULL,
    guild_id INTEGER,
    channel_id INTEGER,
    prompt_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    output_excerpt TEXT
);

CREATE TABLE IF NOT EXISTS admin_ask_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    actor_id INTEGER NOT NULL,
    guild_id INTEGER,
    channel_id INTEGER,
    prompt_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    request TEXT NOT NULL,
    output_excerpt TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_admin_ask_memory_scope
ON admin_ask_memory(actor_id, guild_id, channel_id, id);

CREATE TABLE IF NOT EXISTS message_ask_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    mode TEXT NOT NULL,
    actor_id INTEGER NOT NULL,
    guild_id INTEGER,
    channel_id INTEGER,
    source_message_id INTEGER,
    bot_message_id INTEGER NOT NULL UNIQUE,
    parent_bot_message_id INTEGER,
    prompt_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    request TEXT NOT NULL,
    output_excerpt TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_message_ask_memory_scope
ON message_ask_memory(guild_id, channel_id, id);

CREATE INDEX IF NOT EXISTS idx_message_ask_memory_bot_message
ON message_ask_memory(bot_message_id);

CREATE TABLE IF NOT EXISTS auto_scan_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    confidence INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    guild_id INTEGER,
    channel_id INTEGER,
    source_message_id INTEGER,
    bot_message_id INTEGER,
    content_excerpt TEXT NOT NULL,
    response_excerpt TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_auto_scan_events_scope
ON auto_scan_events(guild_id, channel_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_auto_scan_events_action
ON auto_scan_events(action, created_at DESC);

CREATE TABLE IF NOT EXISTS github_deliveries (
    delivery_id TEXT PRIMARY KEY,
    event TEXT NOT NULL,
    action TEXT,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_cards (
    card_key TEXT PRIMARY KEY,
    destination TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    source_url TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issue_drafts (
    draft_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    actor_id INTEGER NOT NULL,
    guild_id INTEGER,
    channel_id INTEGER,
    summary TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
);

CREATE TABLE IF NOT EXISTS playtest_records (
    playtest_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    actor_id INTEGER NOT NULL,
    guild_id INTEGER,
    channel_id INTEGER,
    target TEXT NOT NULL,
    start_time TEXT,
    duration_minutes INTEGER,
    voice TEXT,
    build TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS playtest_syntheses (
    synthesis_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    guild_id INTEGER,
    destination_channel_id INTEGER,
    report_count INTEGER NOT NULL,
    prompt_hash TEXT NOT NULL,
    discord_message_id INTEGER
);

CREATE TABLE IF NOT EXISTS playtest_synthesis_sources (
    synthesis_id TEXT NOT NULL,
    playtest_id TEXT NOT NULL UNIQUE,
    PRIMARY KEY (synthesis_id, playtest_id)
);

CREATE TABLE IF NOT EXISTS automation_config (
    name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    destination TEXT NOT NULL DEFAULT '',
    config_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS testing_poll_options (
    slot INTEGER PRIMARY KEY,
    option_key TEXT NOT NULL DEFAULT '',
    option_label TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS testing_votes (
    user_id INTEGER PRIMARY KEY,
    option_key TEXT NOT NULL,
    option_label TEXT NOT NULL DEFAULT '',
    weight INTEGER NOT NULL DEFAULT 0,
    voted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routine_posts (
    name TEXT PRIMARY KEY,
    period_key TEXT NOT NULL DEFAULT '',
    posted_at TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS announcements (
    announcement_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    actor_id INTEGER NOT NULL,
    guild_id INTEGER,
    topic TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    destination_channel_id TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS server_action_plans (
    plan_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    actor_id INTEGER NOT NULL,
    guild_id INTEGER,
    request TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    params_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'planned',
    result TEXT NOT NULL DEFAULT '',
    executed_at TEXT NOT NULL DEFAULT ''
);

-- Shared with conversation_memory (identical statement, IF NOT EXISTS on both sides): the activity
-- rollup joins it for display names, so the store owns enough schema to rank members on its own.
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT,
    last_seen_at TEXT NOT NULL,
    is_bot INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS member_activity_daily (
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    messages INTEGER NOT NULL DEFAULT 0,
    xp REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, day)
);

CREATE TABLE IF NOT EXISTS member_bonus_xp (
    ref TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    xp REAL NOT NULL DEFAULT 0,
    awarded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_member_bonus_user ON member_bonus_xp(user_id);

CREATE TABLE IF NOT EXISTS member_role_state (
    user_id INTEGER PRIMARY KEY,
    tier TEXT NOT NULL DEFAULT '',
    synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS member_tiers (
    user_id INTEGER PRIMARY KEY,
    xp REAL NOT NULL DEFAULT 0,
    tier TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS member_prefs (
    user_id INTEGER PRIMARY KEY,
    banter_optout INTEGER NOT NULL DEFAULT 0,
    leaderboard_optout INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS playtest_automation_marks (
    playtest_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (playtest_id, kind)
);
"""

DEFAULT_AUTOMATIONS = {
    "repository_index_refresh": 1,
    "auto_question_answering": 1,
    "auto_soft_rule_warnings": 1,
    "auto_bot_topic_banter": 1,
    "skill_subagent_change_summary": 1,
    "playtest_reminders": 1,
    "post_playtest_result_request": 1,
    "playtest_result_synthesis": 1,
    "weekly_content_dump": 1,
    "release_announcement_posting": 0,
    "routine_dev_digest": 1,
    "routine_release_posts": 1,
    "routine_server_intel": 1,
}

AUTOMATION_DESCRIPTIONS = {
    "repository_index_refresh": "Refreshes ChaosX's local event/scenario/cluster/search index from the Chaos Redux repo.",
    "auto_question_answering": "Scanner gates clear Chaos Redux/server questions, then uses the public model to answer from exact local/catalog context.",
    "auto_soft_rule_warnings": "Scanner gates obvious rule problems, then uses the public model to write a short soft warning and reports it to the automations channel.",
    "auto_bot_topic_banter": "Scanner gates explicit conversations about ChaosX/the bot, then uses the public model to write short dynamic banter.",
    "skill_subagent_change_summary": "Would summarize changes made by agent/skill-driven work.",
    "playtest_reminders": "Sends playtest reminder messages when a playtest is scheduled.",
    "post_playtest_result_request": "Asks testers for results/observations after a playtest window.",
    "playtest_result_synthesis": "Batches new playtest observations into a private model-generated report with bugs, balance concerns, successful checks, uncertain findings, and next actions.",
    "weekly_content_dump": "Image-led weekly content-dump post. Posts only when enough fresh visuals/assets exist.",
    "release_announcement_posting": "DEPRECATED and superseded by routine_release_posts, which posts release announcements from the live mod version. Kept only so the registry row stays visible; safe to leave disabled.",
    "routine_dev_digest": "Autonomous weekly dev digest: real commit/catalog/issue/Q&A facts, posted once a week to the routine posts channel.",
    "routine_release_posts": "Autonomous release announcements: posts when the mod version in descriptor.mod changes (or a GitHub release appears).",
    "routine_server_intel": "Private weekly server-intel DM to the owner: what people asked, where ChaosX could not help, activity, moderation, and his admin actions.",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            for name, enabled in DEFAULT_AUTOMATIONS.items():
                await db.execute(
                    "INSERT OR IGNORE INTO automation_config(name, enabled, updated_at) VALUES (?, ?, ?)",
                    (name, enabled, now_iso()),
                )
            await db.commit()

    async def audit(self, *, actor_id: int, guild_id: int | None, channel_id: int | None, command: str, summary: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO audit_log(created_at, actor_id, guild_id, channel_id, command, summary) VALUES (?, ?, ?, ?, ?, ?)",
                (now_iso(), actor_id, guild_id, channel_id, command, summary[:2000]),
            )
            await db.commit()

    async def record_hermes_run(self, *, actor_id: int, guild_id: int | None, channel_id: int | None, prompt_hash: str, status: str, output_excerpt: str = "") -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO hermes_runs(created_at, actor_id, guild_id, channel_id, prompt_hash, status, output_excerpt) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now_iso(), actor_id, guild_id, channel_id, prompt_hash, status, output_excerpt[:4000]),
            )
            await db.commit()

    async def record_admin_ask_turn(
        self,
        *,
        actor_id: int,
        guild_id: int | None,
        channel_id: int | None,
        prompt_hash: str,
        status: str,
        request: str,
        output_excerpt: str,
        keep_last: int = 20,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO admin_ask_memory(created_at, actor_id, guild_id, channel_id, prompt_hash, status, request, output_excerpt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (now_iso(), actor_id, guild_id, channel_id, prompt_hash, status, request[:2000], output_excerpt[:4000]),
            )
            if keep_last > 0:
                await db.execute(
                    """
                    DELETE FROM admin_ask_memory
                    WHERE actor_id = ?
                      AND guild_id IS ?
                      AND channel_id IS ?
                      AND id NOT IN (
                          SELECT id FROM admin_ask_memory
                          WHERE actor_id = ?
                            AND guild_id IS ?
                            AND channel_id IS ?
                          ORDER BY id DESC
                          LIMIT ?
                      )
                    """,
                    (actor_id, guild_id, channel_id, actor_id, guild_id, channel_id, keep_last),
                )
            await db.commit()

    async def list_admin_ask_memory(self, *, actor_id: int, guild_id: int | None, channel_id: int | None, limit: int = 5) -> list[tuple]:
        if limit <= 0:
            return []
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT created_at, prompt_hash, status, request, output_excerpt
                FROM admin_ask_memory
                WHERE actor_id = ?
                  AND guild_id IS ?
                  AND channel_id IS ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (actor_id, guild_id, channel_id, limit),
            )
            rows = [tuple(row) for row in await cur.fetchall()]
        return list(reversed(rows))

    async def clear_admin_ask_memory(self, *, actor_id: int, guild_id: int | None, channel_id: int | None) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                DELETE FROM admin_ask_memory
                WHERE actor_id = ?
                  AND guild_id IS ?
                  AND channel_id IS ?
                """,
                (actor_id, guild_id, channel_id),
            )
            await db.commit()
            return cur.rowcount

    async def record_message_ask_turn(
        self,
        *,
        mode: str,
        actor_id: int,
        guild_id: int | None,
        channel_id: int | None,
        source_message_id: int | None,
        bot_message_id: int,
        parent_bot_message_id: int | None,
        prompt_hash: str,
        status: str,
        request: str,
        output_excerpt: str,
        keep_last: int = 0,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO message_ask_memory(
                    created_at, mode, actor_id, guild_id, channel_id, source_message_id,
                    bot_message_id, parent_bot_message_id, prompt_hash, status, request, output_excerpt
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_iso(),
                    mode[:40],
                    actor_id,
                    guild_id,
                    channel_id,
                    source_message_id,
                    bot_message_id,
                    parent_bot_message_id,
                    prompt_hash,
                    status,
                    request[:1200],
                    output_excerpt[:2500],
                ),
            )
            if keep_last > 0:
                await db.execute(
                    """
                    DELETE FROM message_ask_memory
                    WHERE guild_id IS ?
                      AND channel_id IS ?
                      AND id NOT IN (
                          SELECT id FROM message_ask_memory
                          WHERE guild_id IS ?
                            AND channel_id IS ?
                          ORDER BY id DESC
                          LIMIT ?
                      )
                    """,
                    (guild_id, channel_id, guild_id, channel_id, keep_last),
                )
            await db.commit()

    async def get_message_ask_turn(self, *, bot_message_id: int | None, guild_id: int | None, channel_id: int | None) -> tuple | None:
        if not bot_message_id:
            return None
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT created_at, mode, actor_id, prompt_hash, status, request, output_excerpt, bot_message_id, parent_bot_message_id
                FROM message_ask_memory
                WHERE bot_message_id = ?
                  AND guild_id IS ?
                  AND channel_id IS ?
                LIMIT 1
                """,
                (bot_message_id, guild_id, channel_id),
            )
            row = await cur.fetchone()
        return tuple(row) if row else None

    async def list_message_ask_chain(self, *, bot_message_id: int | None, guild_id: int | None, channel_id: int | None, limit: int = 6) -> list[tuple]:
        if not bot_message_id or limit <= 0:
            return []
        rows: list[tuple] = []
        seen: set[int] = set()
        current = bot_message_id
        async with aiosqlite.connect(self.db_path) as db:
            while current and len(rows) < limit and current not in seen:
                seen.add(current)
                cur = await db.execute(
                    """
                    SELECT created_at, mode, actor_id, prompt_hash, status, request, output_excerpt, bot_message_id, parent_bot_message_id
                    FROM message_ask_memory
                    WHERE bot_message_id = ?
                      AND guild_id IS ?
                      AND channel_id IS ?
                    LIMIT 1
                    """,
                    (current, guild_id, channel_id),
                )
                row = await cur.fetchone()
                if not row:
                    break
                data = tuple(row)
                rows.append(data)
                current = data[8]
        return list(reversed(rows))

    async def list_recent_message_ask_memory(self, *, guild_id: int | None, channel_id: int | None, limit: int = 3) -> list[tuple]:
        if limit <= 0:
            return []
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT created_at, mode, actor_id, prompt_hash, status, request, output_excerpt, bot_message_id, parent_bot_message_id
                FROM message_ask_memory
                WHERE guild_id IS ?
                  AND channel_id IS ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (guild_id, channel_id, limit),
            )
            rows = [tuple(row) for row in await cur.fetchall()]
        return list(reversed(rows))

    async def automation_enabled(self, name: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT enabled FROM automation_config WHERE name = ?", (name,))
            row = await cur.fetchone()
        return bool(row and row[0])

    async def warning_count_for(self, actor_id: int) -> int:
        """Total recorded soft warnings for one user (the warned-users count)."""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM auto_scan_events WHERE actor_id = ? AND action = 'soft_warning'",
                (actor_id,),
            )
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def list_warned_users(self, *, guild_id: int | None = None, limit: int = 25) -> list[tuple]:
        """Group soft-warning events by user: actor_id, warning count, last warned at, latest reason."""
        limit = max(1, min(limit, 100))
        where: list[str] = ["action = 'soft_warning'"]
        params: list[object] = []
        if guild_id is not None:
            where.append("guild_id IS ?")
            params.append(guild_id)
        sql = """
            SELECT
                actor_id,
                COUNT(*) AS warning_count,
                MAX(created_at) AS last_warned_at,
                (SELECT reason FROM auto_scan_events w
                  WHERE w.actor_id = e.actor_id AND w.action = 'soft_warning'
                  ORDER BY w.id DESC LIMIT 1) AS latest_reason
            FROM auto_scan_events e
        """
        sql += " WHERE " + " AND ".join(where)
        sql += """
            GROUP BY actor_id
            ORDER BY warning_count DESC, last_warned_at DESC
            LIMIT ?
        """
        params.append(limit)
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(sql, tuple(params))
            return [tuple(row) for row in await cur.fetchall()]

    async def record_auto_scan_event(
        self,
        *,
        action: str,
        reason: str,
        confidence: int,
        actor_id: int,
        guild_id: int | None,
        channel_id: int | None,
        source_message_id: int | None,
        bot_message_id: int | None,
        content_excerpt: str,
        response_excerpt: str,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO auto_scan_events(
                    created_at, action, reason, confidence, actor_id, guild_id, channel_id,
                    source_message_id, bot_message_id, content_excerpt, response_excerpt
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_iso(),
                    action[:40],
                    reason[:500],
                    max(0, min(100, int(confidence))),
                    actor_id,
                    guild_id,
                    channel_id,
                    source_message_id,
                    bot_message_id,
                    content_excerpt[:1600],
                    response_excerpt[:4000],
                ),
            )
            await db.commit()

    async def list_auto_scan_events(self, *, guild_id: int | None = None, limit: int = 10, action: str = "") -> list[tuple]:
        limit = max(1, min(limit, 50))
        where: list[str] = []
        params: list[object] = []
        if guild_id is not None:
            where.append("guild_id IS ?")
            params.append(guild_id)
        if action.strip():
            where.append("action = ?")
            params.append(action.strip()[:40])
        sql = """
            SELECT id, created_at, action, reason, confidence, actor_id, guild_id, channel_id,
                   source_message_id, bot_message_id, content_excerpt, response_excerpt
            FROM auto_scan_events
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(sql, tuple(params))
            return [tuple(row) for row in await cur.fetchall()]

    async def record_github_delivery(self, *, delivery_id: str, event: str, action: str | None, status: str, summary: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute(
                    "INSERT INTO github_deliveries(delivery_id, event, action, received_at, status, summary) VALUES (?, ?, ?, ?, ?, ?)",
                    (delivery_id, event, action, now_iso(), status, summary[:4000]),
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def upsert_card(self, *, card_key: str, destination: str, title: str, body: str, source_url: str = "") -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO message_cards(card_key, destination, title, body, source_url, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(card_key) DO UPDATE SET destination=excluded.destination, title=excluded.title, body=excluded.body, source_url=excluded.source_url, updated_at=excluded.updated_at
                """,
                (card_key, destination, title, body[:4000], source_url, now_iso()),
            )
            await db.commit()

    async def create_issue_draft(self, *, draft_id: str, actor_id: int, guild_id: int | None, channel_id: int | None, summary: str, body: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO issue_drafts(draft_id, created_at, actor_id, guild_id, channel_id, summary, body, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft')",
                (draft_id, now_iso(), actor_id, guild_id, channel_id, summary[:500], body[:8000]),
            )
            await db.commit()

    async def list_issue_drafts(self, limit: int = 10) -> list[tuple]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT draft_id, created_at, summary, status FROM issue_drafts ORDER BY created_at DESC LIMIT ?", (limit,))
            return [tuple(row) for row in await cur.fetchall()]

    async def update_playtest_schedule(
        self,
        *,
        playtest_id: str,
        start_time: str,
        duration_minutes: int = 0,
        voice: str = "",
        build: str = "",
    ) -> None:
        """Store the parsed timing block of a playtest draft (reminder/result automation reads it)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE playtest_records SET start_time = ?, duration_minutes = ?, voice = ?, build = ? "
                "WHERE playtest_id = ?",
                (start_time, max(0, int(duration_minutes)), voice[:200], build[:200], playtest_id),
            )
            await db.commit()

    async def list_scheduled_playtests(self, *, guild_id: int, limit: int = 25) -> list[dict]:
        """Playtests that carry a real parsed start time (placeholders excluded)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT playtest_id, created_at, guild_id, channel_id, target, start_time, "
                "duration_minutes, voice, build, status FROM playtest_records "
                "WHERE guild_id = ? AND start_time NOT IN ('', 'AI draft', 'draft') "
                "ORDER BY start_time DESC LIMIT ?",
                (guild_id, max(1, min(limit, 100))),
            )
            return [dict(row) for row in await cur.fetchall()]

    async def mark_playtest_automation(self, *, playtest_id: str, kind: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO playtest_automation_marks(playtest_id, kind, created_at) VALUES (?, ?, ?)",
                (playtest_id, kind, now_iso()),
            )
            await db.commit()

    async def playtest_automation_marks(self, *, kind: str) -> set[str]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT playtest_id FROM playtest_automation_marks WHERE kind = ?", (kind,)
            )
            return {str(row[0]) for row in await cur.fetchall()}

    async def create_playtest(self, *, playtest_id: str, actor_id: int, guild_id: int | None, channel_id: int | None, target: str, start_time: str, duration_minutes: int, voice: str, build: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO playtest_records(playtest_id, created_at, actor_id, guild_id, channel_id, target, start_time, duration_minutes, voice, build, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft')",
                (playtest_id, now_iso(), actor_id, guild_id, channel_id, target, start_time, duration_minutes, voice, build),
            )
            await db.commit()

    async def add_playtest_report(self, *, playtest_id: str, report: dict) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE playtest_records SET report_json = ?, status = 'reported' WHERE playtest_id = ?",
                (json.dumps(report, ensure_ascii=False), playtest_id),
            )
            await db.commit()

    async def list_playtests(self, limit: int = 10) -> list[tuple]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT playtest_id, target, start_time, duration_minutes, voice, build, status FROM playtest_records ORDER BY created_at DESC LIMIT ?", (limit,))
            return [tuple(row) for row in await cur.fetchall()]

    async def list_playtest_reports(self, limit: int = 10) -> list[tuple]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT playtest_id, created_at, target, status, report_json
                FROM playtest_records
                WHERE status = 'reported'
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [tuple(row) for row in await cur.fetchall()]

    async def community_captures(self, *, since_iso: str, limit: int = 10) -> list[tuple]:
        """Community ideas/suggestions written up in the window, straight from the audit log.

        Only the vault-write rows are used (`vault event-idea` / `vault suggestion`), because their
        summary is the note name the member's submission became. File mtimes are NOT a source: the
        vault is synced in bulk, so mtime is sync time, not authoring time.
        """
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT created_at, command, summary, COALESCE(actor_id, 0)
                FROM audit_log
                WHERE created_at >= ? AND command IN ('vault event-idea', 'vault suggestion')
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (since_iso, max(1, min(limit, 50))),
            )
            return [tuple(row) for row in await cur.fetchall()]

    async def list_playtest_reports_since(self, *, since_iso: str, limit: int = 6) -> list[tuple]:
        """Reports recorded in the window, newest first (community observations for the digest)."""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT created_at, target, report_json
                FROM playtest_records
                WHERE status = 'reported' AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (since_iso, max(1, min(limit, 25))),
            )
            return [tuple(row) for row in await cur.fetchall()]

    async def list_unsynthesized_playtest_reports(
        self, *, guild_id: int, limit: int = 25
    ) -> list[tuple]:
        limit = max(1, min(limit, 100))
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT p.playtest_id, p.created_at, p.target, p.status, p.report_json
                FROM playtest_records AS p
                WHERE p.status = 'reported'
                  AND p.guild_id = ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM playtest_synthesis_sources AS source
                      WHERE source.playtest_id = p.playtest_id
                  )
                ORDER BY p.created_at ASC
                LIMIT ?
                """,
                (guild_id, limit),
            )
            return [tuple(row) for row in await cur.fetchall()]

    async def record_playtest_synthesis(
        self,
        *,
        synthesis_id: str,
        guild_id: int,
        destination_channel_id: int,
        playtest_ids: list[str],
        prompt_hash: str,
        discord_message_id: int,
    ) -> None:
        if not playtest_ids:
            raise ValueError("playtest synthesis requires at least one report")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                INSERT INTO playtest_syntheses(
                    synthesis_id, created_at, guild_id, destination_channel_id,
                    report_count, prompt_hash, discord_message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    synthesis_id,
                    now_iso(),
                    guild_id,
                    destination_channel_id,
                    len(playtest_ids),
                    prompt_hash,
                    discord_message_id,
                ),
            )
            await db.executemany(
                """
                INSERT INTO playtest_synthesis_sources(synthesis_id, playtest_id)
                VALUES (?, ?)
                """,
                [(synthesis_id, playtest_id) for playtest_id in playtest_ids],
            )
            await db.commit()

    async def list_automations(self) -> list[tuple[str, int, str, str]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT name, enabled, destination FROM automation_config ORDER BY name")
            return [(*tuple(row), AUTOMATION_DESCRIPTIONS.get(str(row[0]), "No description yet.")) for row in await cur.fetchall()]

    async def set_automation(self, name: str, enabled: bool) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("UPDATE automation_config SET enabled = ?, updated_at = ? WHERE name = ?", (1 if enabled else 0, now_iso(), name))
            await db.commit()
            return cur.rowcount > 0

    async def set_automation_destination(self, names: list[str], destination: str) -> None:
        if not names:
            return
        placeholders = ",".join("?" for _ in names)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE automation_config SET destination = ?, updated_at = ? WHERE name IN ({placeholders})",
                (destination, now_iso(), *names),
            )
            await db.commit()

    # ---------------------------------------------------------------- member activity / chaos tiers
    async def activity_cursor(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT value FROM activity_state WHERE key = 'archive_cursor'")
            row = await cur.fetchone()
        try:
            return int(row[0]) if row else 0
        except (TypeError, ValueError):
            return 0

    async def set_activity_cursor(self, value: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO activity_state(key, value) VALUES('archive_cursor', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(int(value)),),
            )
            await db.commit()

    async def archive_rows_after(
        self, cursor: int, *, limit: int = 5000, ignore_ids: set[int] | None = None
    ) -> list[tuple]:
        """(id, author_id, created_at, channel_id, content) beyond the rollup cursor, oldest first.

        `ignore_ids` keeps bot authors out of the rollup — ChaosX must not rank on its own leaderboard.
        """
        skip = sorted({int(value) for value in (ignore_ids or set()) if value})
        clause = f" AND author_id NOT IN ({','.join('?' for _ in skip)})" if skip else ""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                f"""
                SELECT id, author_id, created_at, channel_id, content
                FROM message_archive
                WHERE id > ? AND author_id IS NOT NULL{clause}
                ORDER BY id ASC
                LIMIT ?
                """,
                (int(cursor), *skip, max(1, int(limit))),
            )
            return [tuple(row) for row in await cur.fetchall()]

    async def archive_day_rows(
        self, user_id: int, day: str, *, ignore_ids: set[int] | None = None
    ) -> list[tuple[str, int | None]]:
        """(content, channel_id) for one member's day, in order — used to re-roll that day in full."""
        if int(user_id) in {int(value) for value in (ignore_ids or set()) if value}:
            return []
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT content, channel_id FROM message_archive WHERE author_id = ? "
                "AND substr(created_at, 1, 10) = ? ORDER BY created_at ASC, id ASC",
                (int(user_id), str(day)),
            )
            return [(str(row[0] or ""), row[1]) for row in await cur.fetchall()]

    async def upsert_activity_days(self, rows: list[tuple[int, str, int, float]]) -> int:
        """(user_id, day, messages, xp) rows; recomputed days are replaced, never double-counted."""
        if not rows:
            return 0
        stamp = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                """
                INSERT INTO member_activity_daily(user_id, day, messages, xp, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(user_id, day) DO UPDATE SET
                    messages = excluded.messages, xp = excluded.xp, updated_at = excluded.updated_at
                """,
                [(int(u), str(d), int(m), float(x), stamp) for u, d, m, x in rows],
            )
            await db.commit()
        return len(rows)

    async def award_bonus_xp(
        self, *, ref: str, user_id: int, kind: str, xp: float, when: str
    ) -> float:
        """Credit one contribution once. Returns the XP granted (0.0 when it was already credited).

        `ref` is the contribution's identity (the spec filename, the playtest row, the issue number), so
        re-running the capture can never pay twice. The day's bonus total is capped (`BONUS_DAILY_CAP`).
        """
        today = parse_day(when) or str(when)[:10]
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT 1 FROM member_bonus_xp WHERE ref = ?", (str(ref),))
            if await cur.fetchone() is not None:
                return 0.0
            cur = await db.execute(
                "SELECT COALESCE(SUM(xp), 0) FROM member_bonus_xp WHERE user_id = ? AND substr(awarded_at, 1, 10) = ?",
                (int(user_id), today),
            )
            already = float((await cur.fetchone())[0] or 0)
            grant = max(0.0, min(float(xp), BONUS_DAILY_CAP - already))
            if grant <= 0:
                return 0.0
            await db.execute(
                "INSERT INTO member_bonus_xp(ref, user_id, kind, xp, awarded_at) VALUES(?, ?, ?, ?, ?)",
                (str(ref), int(user_id), str(kind), round(grant, 3), str(when)),
            )
            await db.commit()
        return round(grant, 3)

    async def set_testing_poll_options(self, options: list[tuple[str, str]]) -> None:
        """Fill the poll's fixed slots (1..5). Fixed slots keep the buttons alive across restarts."""
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM testing_poll_options")
            await db.executemany(
                "INSERT INTO testing_poll_options(slot, option_key, option_label, updated_at) "
                "VALUES(?, ?, ?, ?)",
                [(slot, key, label[:120], now) for slot, (key, label) in enumerate(options[:5], start=1)],
            )
            await db.commit()

    async def testing_poll_options(self) -> dict[int, tuple[str, str]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT slot, option_key, option_label FROM testing_poll_options ORDER BY slot"
            )
            rows = await cur.fetchall()
        return {int(slot): (str(key), str(label)) for slot, key, label in rows}

    async def set_testing_vote(
        self, user_id: int, option_key: str, option_label: str, weight: int
    ) -> None:
        """One vote per member - a new choice replaces the old one; the weight is snapshotted."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO testing_votes(user_id, option_key, option_label, weight, voted_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    option_key = excluded.option_key,
                    option_label = excluded.option_label,
                    weight = excluded.weight,
                    voted_at = excluded.voted_at
                """,
                (
                    int(user_id),
                    str(option_key),
                    str(option_label)[:120],
                    int(weight),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            await db.commit()

    async def member_testing_vote(self, user_id: int) -> tuple[str, str, int] | None:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT option_key, option_label, weight FROM testing_votes WHERE user_id = ?",
                (int(user_id),),
            )
            row = await cur.fetchone()
        return (str(row[0]), str(row[1]), int(row[2])) if row else None

    async def testing_vote_tally(self) -> list[tuple[str, str, int, int]]:
        """(option_key, option_label, voters, weighted_total) per option, strongest first."""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT option_key, MAX(option_label), COUNT(*), SUM(weight)
                FROM testing_votes GROUP BY option_key
                ORDER BY SUM(weight) DESC, COUNT(*) DESC, option_key
                """
            )
            rows = await cur.fetchall()
        return [(str(k), str(label), int(voters), int(total or 0)) for k, label, voters, total in rows]

    async def member_activity_totals(self, user_id: int) -> tuple[int, int]:
        """(messages, active days) for one member, for the tier-up congratulation."""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT COALESCE(SUM(messages), 0), COUNT(*) FROM member_activity_daily WHERE user_id = ?",
                (int(user_id),),
            )
            row = await cur.fetchone()
        return (int(row[0] or 0), int(row[1] or 0))

    async def bonus_xp_breakdown(self, user_id: int) -> list[tuple[str, float, str]]:
        """(kind, xp, awarded_at) for one member's contributions, newest first."""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT kind, xp, awarded_at FROM member_bonus_xp WHERE user_id = ? "
                "ORDER BY awarded_at DESC",
                (int(user_id),),
            )
            return [(str(kind), float(xp), str(when)) for kind, xp, when in await cur.fetchall()]

    async def bonus_xp_total(self, user_id: int) -> float:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT COALESCE(SUM(xp), 0) FROM member_bonus_xp WHERE user_id = ?", (int(user_id),)
            )
            return float((await cur.fetchone())[0] or 0)

    async def recompute_member_tiers(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT user_id, SUM(xp) FROM ("
                "SELECT user_id, xp FROM member_activity_daily "
                "UNION ALL SELECT user_id, xp FROM member_bonus_xp"
                ") GROUP BY user_id HAVING SUM(xp) > 0"
            )
            totals = [(int(u), float(x or 0)) for u, x in await cur.fetchall()]
            stamp = datetime.now(timezone.utc).isoformat()
            rows = [
                (user_id, round(xp, 3), tier_for_xp(xp), stamp)
                for user_id, xp in totals
            ]
            await db.executemany(
                """
                INSERT INTO member_tiers(user_id, xp, tier, updated_at) VALUES(?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    xp = excluded.xp, tier = excluded.tier, updated_at = excluded.updated_at
                """,
                rows,
            )
            await db.commit()
        return len(rows)

    async def all_member_tiers(self) -> list[tuple[int, float, str]]:
        """Every member with recorded activity: (user_id, xp, tier)."""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT user_id, xp, tier FROM member_tiers ORDER BY xp DESC")
            return [(int(user_id), float(xp), str(tier)) for user_id, xp, tier in await cur.fetchall()]

    async def tier_role_state(self, user_id: int) -> str | None:
        """The tier the member's role was last synced to, or None if never synced."""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT tier FROM member_role_state WHERE user_id = ?", (int(user_id),))
            row = await cur.fetchone()
        return str(row[0]) if row else None

    async def set_tier_role_state(self, user_id: int, tier: str, when: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO member_role_state(user_id, tier, synced_at) VALUES(?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET tier = excluded.tier, synced_at = excluded.synced_at",
                (int(user_id), str(tier), str(when)),
            )
            await db.commit()

    async def member_tier(self, user_id: int) -> tuple[float, str] | None:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT xp, tier FROM member_tiers WHERE user_id = ?", (int(user_id),))
            row = await cur.fetchone()
        return (float(row[0]), str(row[1])) if row else None

    async def top_members(
        self,
        *,
        limit: int = 10,
        since_day: str | None = None,
        exclude_ids: set[int] | None = None,
    ) -> list[tuple]:
        """(user_id, display_name, xp, messages, active_days) ranked by XP.

        `exclude_ids` drops leaderboard opt-outs (and anything else the caller hides) from the rankings.
        """
        skip = sorted({int(value) for value in (exclude_ids or set()) if value})
        clause = f" AND a.user_id NOT IN ({','.join('?' for _ in skip)})" if skip else ""
        select = (
            "SELECT a.user_id, COALESCE(u.display_name, ''), SUM(a.xp), SUM(a.messages), COUNT(*) "
            "FROM member_activity_daily a LEFT JOIN users u ON u.user_id = a.user_id "
        )
        async with aiosqlite.connect(self.db_path) as db:
            if since_day:
                sql = (
                    select
                    + f"WHERE a.day >= ?{clause} GROUP BY a.user_id ORDER BY SUM(a.xp) DESC, a.user_id LIMIT ?"
                )
                params: tuple = (str(since_day), *skip, max(1, int(limit)))
            else:
                sql = (
                    select
                    + f"WHERE 1=1{clause} GROUP BY a.user_id ORDER BY SUM(a.xp) DESC, a.user_id LIMIT ?"
                )
                params = (*skip, max(1, int(limit)))
            cur = await db.execute(sql, params)
            return [tuple(row) for row in await cur.fetchall()]

    async def member_rank(self, user_id: int, *, since_day: str | None = None) -> int | None:
        """1-based position of a member on the leaderboard, or None when they have no recorded activity."""
        rows = await self.top_members(limit=1000, since_day=since_day)
        for position, row in enumerate(rows, start=1):
            if int(row[0]) == int(user_id):
                return position
        return None

    async def last_seen_in_channel(self, channel_id: int) -> dict[int, str]:
        """user_id -> newest archived message timestamp in one channel (banter eligibility)."""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT author_id, MAX(created_at) FROM message_archive WHERE channel_id = ? "
                "AND author_id IS NOT NULL GROUP BY author_id",
                (int(channel_id),),
            )
            return {int(row[0]): str(row[1]) for row in await cur.fetchall()}

    async def known_bot_ids(self) -> set[int]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT user_id FROM users WHERE COALESCE(is_bot, 0) = 1")
            return {int(row[0]) for row in await cur.fetchall()}

    async def activity_xp_by_member(self) -> dict[int, float]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT user_id, xp FROM member_tiers")
            return {int(row[0]): float(row[1] or 0) for row in await cur.fetchall()}

    async def member_prefs(self, user_id: int) -> dict[str, bool]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT banter_optout, leaderboard_optout FROM member_prefs WHERE user_id = ?", (int(user_id),)
            )
            row = await cur.fetchone()
        if not row:
            return {"banter_optout": False, "leaderboard_optout": False}
        return {"banter_optout": bool(row[0]), "leaderboard_optout": bool(row[1])}

    async def set_member_pref(self, user_id: int, field: str, value: bool) -> None:
        if field not in {"banter_optout", "leaderboard_optout"}:
            raise ValueError(f"unknown member preference: {field}")
        stamp = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"INSERT INTO member_prefs(user_id, {field}, updated_at) VALUES(?, ?, ?) "
                f"ON CONFLICT(user_id) DO UPDATE SET {field} = excluded.{field}, updated_at = excluded.updated_at",
                (int(user_id), 1 if value else 0, stamp),
            )
            await db.commit()

    async def opted_out_members(self, field: str = "banter_optout") -> set[int]:
        if field not in {"banter_optout", "leaderboard_optout"}:
            raise ValueError(f"unknown member preference: {field}")
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(f"SELECT user_id FROM member_prefs WHERE {field} = 1")
            return {int(row[0]) for row in await cur.fetchall()}

    async def upsert_member_tier(self, user_id: int, xp: float, tier: str) -> None:
        stamp = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO member_tiers(user_id, xp, tier, updated_at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET xp = excluded.xp, tier = excluded.tier, "
                "updated_at = excluded.updated_at",
                (int(user_id), float(xp), str(tier), stamp),
            )
            await db.commit()

    async def routine_post_states(self, names: list[str]) -> dict[str, dict]:
        """Stored per-post state (period key, last check, last post) for the named post types."""
        if not names:
            return {}
        placeholders = ",".join("?" for _ in names)
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                f"SELECT name, period_key, posted_at, checked_at, channel_id, message_id, status, detail "
                f"FROM routine_posts WHERE name IN ({placeholders})",
                tuple(names),
            )
            rows = await cur.fetchall()
        columns = ("name", "period_key", "posted_at", "checked_at", "channel_id", "message_id", "status", "detail")
        return {str(row[0]): dict(zip(columns, row)) for row in rows}

    async def record_routine_post(
        self,
        name: str,
        *,
        period_key: str = "",
        posted_at: str = "",
        checked_at: str = "",
        channel_id: str = "",
        message_id: str = "",
        status: str = "",
        detail: str = "",
    ) -> None:
        """Upsert one routine-post state row.

        Empty values never clobber existing data, so a baseline write (checked_at +
        detail) can coexist with a later real post (period_key + posted_at).
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO routine_posts(name, period_key, posted_at, checked_at, channel_id, message_id, status, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    period_key = CASE WHEN excluded.period_key != '' THEN excluded.period_key ELSE routine_posts.period_key END,
                    posted_at = CASE WHEN excluded.posted_at != '' THEN excluded.posted_at ELSE routine_posts.posted_at END,
                    checked_at = CASE WHEN excluded.checked_at != '' THEN excluded.checked_at ELSE routine_posts.checked_at END,
                    channel_id = CASE WHEN excluded.channel_id != '' THEN excluded.channel_id ELSE routine_posts.channel_id END,
                    message_id = CASE WHEN excluded.message_id != '' THEN excluded.message_id ELSE routine_posts.message_id END,
                    status = CASE WHEN excluded.status != '' THEN excluded.status ELSE routine_posts.status END,
                    detail = CASE WHEN excluded.detail != '' THEN excluded.detail ELSE routine_posts.detail END
                """,
                (name, period_key, posted_at, checked_at, channel_id, message_id, status, detail[:4000]),
            )
            await db.commit()

    async def routine_stats(self, *, since_iso: str) -> dict[str, int]:
        """Windowed server facts for the weekly digest (counted rows, never estimates)."""
        async with aiosqlite.connect(self.db_path) as db:
            async def scalar(sql: str, params: tuple = ()) -> int:
                cur = await db.execute(sql, params)
                row = await cur.fetchone()
                return int(row[0]) if row and row[0] is not None else 0

            return {
                "answers": await scalar(
                    "SELECT COUNT(*) FROM auto_scan_events WHERE action = 'answer' AND created_at >= ?",
                    (since_iso,),
                ),
                "warnings": await scalar(
                    "SELECT COUNT(*) FROM auto_scan_events WHERE action = 'soft_warning' AND created_at >= ?",
                    (since_iso,),
                ),
                "banter": await scalar(
                    "SELECT COUNT(*) FROM auto_scan_events WHERE action = 'banter' AND created_at >= ?",
                    (since_iso,),
                ),
                "asks": await scalar(
                    "SELECT COUNT(*) FROM message_ask_memory WHERE created_at >= ?", (since_iso,)
                ),
                "playtests": await scalar(
                    "SELECT COUNT(*) FROM playtest_records WHERE created_at >= ?", (since_iso,)
                ),
                "playtests_total": await scalar("SELECT COUNT(*) FROM playtest_records"),
            }

    async def record_announcement(
        self,
        announcement_id: str,
        *,
        actor_id: int,
        guild_id: int | None = None,
        topic: str = "",
        body: str = "",
        status: str = "draft",
        destination_channel_id: str = "",
        message_id: str = "",
        detail: str = "",
    ) -> None:
        """Upsert one announcement; empty fields never clobber stored values."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO announcements(
                    announcement_id, created_at, actor_id, guild_id, topic, body, status,
                    destination_channel_id, message_id, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(announcement_id) DO UPDATE SET
                    body = CASE WHEN excluded.body != '' THEN excluded.body ELSE announcements.body END,
                    status = CASE WHEN excluded.status != '' THEN excluded.status ELSE announcements.status END,
                    destination_channel_id = CASE WHEN excluded.destination_channel_id != '' THEN excluded.destination_channel_id ELSE announcements.destination_channel_id END,
                    message_id = CASE WHEN excluded.message_id != '' THEN excluded.message_id ELSE announcements.message_id END,
                    detail = CASE WHEN excluded.detail != '' THEN excluded.detail ELSE announcements.detail END
                """,
                (
                    announcement_id,
                    now_iso(),
                    actor_id,
                    guild_id,
                    topic[:200],
                    body[:8000],
                    status,
                    destination_channel_id,
                    message_id,
                    detail[:4000],
                ),
            )
            await db.commit()

    async def list_announcements(self, *, limit: int = 10) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT announcement_id, created_at, status, topic, destination_channel_id, message_id "
                "FROM announcements ORDER BY created_at DESC LIMIT ?",
                (max(1, limit),),
            )
            return [dict(row) for row in await cur.fetchall()]

    async def last_announcement(self, *, status: str = "posted") -> dict | None:
        """Most recent announcement in the given status (used for 'since the last announcement' facts)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT announcement_id, created_at, topic, body, status, detail FROM announcements "
                "WHERE status = ? ORDER BY created_at DESC LIMIT 1",
                (status,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def latest_draft_announcement(self, *, topic: str = "") -> dict | None:
        """Newest stored draft (optionally for an exact topic) so review→post keeps the same text."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if topic.strip():
                cur = await db.execute(
                    "SELECT announcement_id, created_at, topic, body, status, detail FROM announcements "
                    "WHERE status = 'draft' AND lower(topic) = lower(?) ORDER BY created_at DESC LIMIT 1",
                    (topic.strip(),),
                )
            else:
                cur = await db.execute(
                    "SELECT announcement_id, created_at, topic, body, status, detail FROM announcements "
                    "WHERE status = 'draft' ORDER BY created_at DESC LIMIT 1"
                )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def record_action_plan(
        self,
        plan_id: str,
        *,
        actor_id: int,
        guild_id: int | None,
        request: str = "",
        action: str = "",
        params_json: str = "{}",
        status: str = "planned",
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO server_action_plans(plan_id, created_at, actor_id, guild_id, request, action, params_json, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(plan_id) DO UPDATE SET
                    action = excluded.action,
                    params_json = excluded.params_json,
                    status = excluded.status
                """,
                (plan_id, now_iso(), actor_id, guild_id, request[:500], action, params_json[:4000], status),
            )
            await db.commit()

    async def get_action_plan(self, plan_id: str) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT plan_id, created_at, actor_id, guild_id, request, action, params_json, status, result, executed_at "
                "FROM server_action_plans WHERE plan_id = ?",
                (plan_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def finish_action_plan(self, plan_id: str, *, status: str, result: str = "") -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE server_action_plans SET status = ?, result = ?, executed_at = ? WHERE plan_id = ?",
                (status, result[:2000], now_iso(), plan_id),
            )
            await db.commit()

    async def list_action_plans(self, *, limit: int = 10) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT plan_id, created_at, action, status, request, result FROM server_action_plans "
                "ORDER BY created_at DESC LIMIT ?",
                (max(1, limit),),
            )
            return [dict(row) for row in await cur.fetchall()]
