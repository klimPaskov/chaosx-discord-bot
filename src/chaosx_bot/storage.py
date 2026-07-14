from __future__ import annotations

import json
import re
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

CREATE TABLE IF NOT EXISTS question_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    mode TEXT NOT NULL,
    actor_id INTEGER NOT NULL,
    guild_id INTEGER,
    channel_id INTEGER,
    source_message_id INTEGER,
    bot_message_id INTEGER,
    parent_bot_message_id INTEGER,
    question_key TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_question_answers_key
ON question_answers(question_key, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_question_answers_scope
ON question_answers(guild_id, channel_id, created_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS question_answers_fts USING fts5(
    question,
    answer,
    content='question_answers',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS question_answers_ai AFTER INSERT ON question_answers BEGIN
    INSERT INTO question_answers_fts(rowid, question, answer) VALUES (new.id, new.question, new.answer);
END;

CREATE TRIGGER IF NOT EXISTS question_answers_ad AFTER DELETE ON question_answers BEGIN
    INSERT INTO question_answers_fts(question_answers_fts, rowid, question, answer) VALUES ('delete', old.id, old.question, old.answer);
END;

CREATE TRIGGER IF NOT EXISTS question_answers_au AFTER UPDATE ON question_answers BEGIN
    INSERT INTO question_answers_fts(question_answers_fts, rowid, question, answer) VALUES ('delete', old.id, old.question, old.answer);
    INSERT INTO question_answers_fts(rowid, question, answer) VALUES (new.id, new.question, new.answer);
END;

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
    "question_answer_tracking": 1,
    "skill_subagent_change_summary": 1,
    "playtest_reminders": 1,
    "post_playtest_result_request": 1,
    "weekly_content_dump": 1,
    "release_announcement_posting": 0,
}

AUTOMATION_DESCRIPTIONS = {
    "repository_index_refresh": "Refreshes ChaosX's local event/scenario/cluster/search index from the Chaos Redux repo.",
    "question_answer_tracking": "Stores successful public ChaosX Q&A pairs and supports /admin qna list/search/popular.",
    "skill_subagent_change_summary": "Would summarize changes made by agent/skill-driven work.",
    "playtest_reminders": "Sends playtest reminder messages when a playtest is scheduled.",
    "post_playtest_result_request": "Asks testers for results/observations after a playtest window.",
    "weekly_content_dump": "Image-led weekly content-dump post. Posts only when enough fresh visuals/assets exist.",
    "release_announcement_posting": "Reserved for release announcement posting; should stay off until explicitly used.",
}


QUESTION_KEY_PATTERN = re.compile(r"[^\w\s'-]+")
WHITESPACE_PATTERN = re.compile(r"\s+")


def normalize_question_key(question: str) -> str:
    text = (question or "").casefold()
    text = re.sub(r"<@!?\d+>", " ", text)
    text = QUESTION_KEY_PATTERN.sub(" ", text)
    text = WHITESPACE_PATTERN.sub(" ", text).strip(" ?!.:,;\t\n\r")
    return text[:500] or "empty-question"


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

    async def record_question_answer(
        self,
        *,
        mode: str,
        actor_id: int,
        guild_id: int | None,
        channel_id: int | None,
        source_message_id: int | None,
        bot_message_id: int | None,
        parent_bot_message_id: int | None,
        question: str,
        answer: str,
        prompt_hash: str,
        status: str = "ok",
    ) -> None:
        question_key = normalize_question_key(question)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO question_answers(
                    created_at, mode, actor_id, guild_id, channel_id, source_message_id,
                    bot_message_id, parent_bot_message_id, question_key, question, answer, prompt_hash, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    question_key,
                    question[:1600],
                    answer[:4000],
                    prompt_hash,
                    status[:40],
                ),
            )
            await db.commit()

    async def list_question_answers(self, *, guild_id: int | None = None, limit: int = 10, query: str = "") -> list[tuple]:
        limit = max(1, min(limit, 50))
        where: list[str] = []
        params: list[object] = []
        if guild_id is not None:
            where.append("guild_id IS ?")
            params.append(guild_id)
        if query.strip():
            needle = f"%{query.strip()}%"
            where.append("(question LIKE ? OR answer LIKE ?)")
            params.extend([needle, needle])
        sql = """
            SELECT id, created_at, mode, actor_id, guild_id, channel_id, question, answer, bot_message_id, prompt_hash, status
            FROM question_answers
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(sql, tuple(params))
            return [tuple(row) for row in await cur.fetchall()]

    async def list_popular_question_answers(self, *, guild_id: int | None = None, limit: int = 10, query: str = "") -> list[tuple]:
        limit = max(1, min(limit, 50))
        where: list[str] = []
        params: list[object] = []
        if guild_id is not None:
            where.append("guild_id IS ?")
            params.append(guild_id)
        if query.strip():
            needle = f"%{query.strip()}%"
            where.append("(question LIKE ? OR answer LIKE ?)")
            params.extend([needle, needle])
        sql = """
            WITH filtered AS (
                SELECT *
                FROM question_answers
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += """
            ), grouped AS (
                SELECT question_key, COUNT(*) AS ask_count, MAX(created_at) AS last_asked_at, MAX(id) AS latest_id
                FROM filtered
                GROUP BY question_key
            )
            SELECT
                grouped.question_key,
                grouped.ask_count,
                grouped.last_asked_at,
                latest.question AS latest_question,
                latest.answer AS latest_answer
            FROM grouped
            JOIN question_answers latest ON latest.id = grouped.latest_id
            ORDER BY grouped.ask_count DESC, grouped.last_asked_at DESC
            LIMIT ?
        """
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
