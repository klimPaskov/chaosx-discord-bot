"""What a panel was attached to, so its buttons still work after the bot restarts.

Discord buttons are only as durable as the custom id behind them. Panels used to carry throwaway ids, so every
restart orphaned every panel already posted: the buttons stayed visible and clicking them failed. Now the ids are
stable (`cxfb:up`, `cxfb:act2`, …) and the question, answer and quick actions are stored against the message id,
so a click months later can rebuild the same panel.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_TABLE = """
CREATE TABLE IF NOT EXISTS answer_panels (
    message_id INTEGER PRIMARY KEY,
    question   TEXT NOT NULL,
    answer     TEXT NOT NULL,
    user_id    INTEGER,
    actions    TEXT,
    created_at TEXT NOT NULL
)
"""


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute(_TABLE)
    conn.commit()
    return conn


def remember(
    db_path: str | Path,
    *,
    message_id: int,
    question: str,
    answer: str,
    user_id: int | None = None,
    actions: list[tuple[str, str]] | None = None,
) -> bool:
    """Store the panel's subject against its message, so the buttons survive a restart."""
    if not message_id:
        return False
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO answer_panels (message_id, question, answer, user_id, actions, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(message_id) DO UPDATE SET question=excluded.question, answer=excluded.answer, "
            "actions=excluded.actions",
            (
                int(message_id),
                question or "",
                answer or "",
                user_id,
                json.dumps([list(pair) for pair in (actions or [])]),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def load(db_path: str | Path, message_id: int) -> dict[str, Any] | None:
    """The stored subject for a panel message, or None when it predates this table."""
    if not message_id:
        return None
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT question, answer, user_id, actions FROM answer_panels WHERE message_id = ?",
            (int(message_id),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    question, answer, user_id, actions = row
    try:
        pairs = [tuple(pair) for pair in json.loads(actions or "[]")]
    except (ValueError, TypeError):
        pairs = []
    return {"question": question or "", "answer": answer or "", "user_id": user_id, "actions": pairs}
