from __future__ import annotations

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
"""


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def audit(self, *, actor_id: int, guild_id: int | None, channel_id: int | None, command: str, summary: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO audit_log(created_at, actor_id, guild_id, channel_id, command, summary) VALUES (?, ?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), actor_id, guild_id, channel_id, command, summary[:2000]),
            )
            await db.commit()

    async def record_hermes_run(self, *, actor_id: int, guild_id: int | None, channel_id: int | None, prompt_hash: str, status: str, output_excerpt: str = "") -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO hermes_runs(created_at, actor_id, guild_id, channel_id, prompt_hash, status, output_excerpt) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), actor_id, guild_id, channel_id, prompt_hash, status, output_excerpt[:4000]),
            )
            await db.commit()
