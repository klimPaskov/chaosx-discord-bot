from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

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

CREATE TABLE IF NOT EXISTS automation_config (
    name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    destination TEXT NOT NULL DEFAULT '',
    config_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
"""

DEFAULT_AUTOMATIONS = {
    "repository_index_refresh": 1,
    "pull_request_ready_summary": 1,
    "ci_failure_first_recovery": 1,
    "skill_subagent_change_summary": 1,
    "playtest_reminders": 1,
    "post_playtest_result_request": 1,
    "weekly_project_digest": 0,
    "stale_blocker_reminder": 0,
    "release_announcement_posting": 0,
    "selected_channel_content_watcher": 0,
    "trusted_role_direct_issue_creation": 0,
    "agent_draft_pr_mode": 0,
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
            return await cur.fetchall()

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
            return await cur.fetchall()

    async def list_automations(self) -> list[tuple[str, int, str]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT name, enabled, destination FROM automation_config ORDER BY name")
            return await cur.fetchall()

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
