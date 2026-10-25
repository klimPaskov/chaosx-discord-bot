"""Turn repeated 👎 into documentation work, not longer prompts.

Hoops, 2026-10-25: "when three or more 👎 land on the same topic, open a gap entry (and optionally a GitHub
issue draft) so the docs get fixed at the source".

A dislike is attributed to the topic keywords of the question it was given to. When a topic crosses the
threshold the first time, an open gap is recorded; the owner digest lists open gaps, and a gap can be marked
resolved once the docs catch up.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

GAP_THRESHOLD = 3
_STOP = {
    "the", "and", "for", "with", "that", "this", "how", "what", "why", "when", "where", "who", "does", "can",
    "you", "your", "are", "was", "were", "not", "but", "from", "about", "into", "have", "has", "had", "will",
    "would", "could", "should", "there", "here", "they", "them", "their", "use", "used", "make", "made",
    "get", "got", "any", "all", "chaosx", "chaos", "redux",
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def topics(question: str, limit: int = 3) -> list[str]:
    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", question or "")]
    picked: list[str] = []
    for word in words:
        if word in _STOP or word in picked:
            continue
        picked.append(word)
        if len(picked) >= limit:
            break
    return picked


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_gaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL UNIQUE,
            dislikes INTEGER NOT NULL DEFAULT 0,
            sample_question TEXT NOT NULL DEFAULT '',
            sample_note TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            issue_draft TEXT NOT NULL DEFAULT ''
        )
        """
    )
    return conn


def record_dislike(db_path: str | Path, *, question: str, note: str = "") -> dict | None:
    """Attribute one 👎 to its topic. Returns the gap row when it just crossed the threshold."""
    conn = _connect(db_path)
    fired: dict | None = None
    try:
        for topic in topics(question):
            row = conn.execute("SELECT id, dislikes, status FROM knowledge_gaps WHERE topic = ?", (topic,)).fetchone()
            now = _iso_now()
            if row is None:
                conn.execute(
                    "INSERT INTO knowledge_gaps (topic, dislikes, sample_question, sample_note, status, created_at, updated_at) "
                    "VALUES (?, 1, ?, ?, 'open', ?, ?)",
                    (topic, (question or "")[:300], (note or "")[:300], now, now),
                )
            else:
                conn.execute(
                    "UPDATE knowledge_gaps SET dislikes = dislikes + 1, updated_at = ?, "
                    "sample_question = CASE WHEN sample_question = '' THEN ? ELSE sample_question END "
                    "WHERE id = ?",
                    (now, (question or "")[:300], row[0]),
                )
                if row[1] + 1 == GAP_THRESHOLD and row[2] == "open":
                    fired = {"topic": topic, "id": row[0], "question": (question or "")[:300]}
                    draft = (
                        f"Docs gap: members keep rating answers about “{topic}” as unhelpful.\n\n"
                        f"Example question: {(question or '')[:300]}\n\n"
                        f"Action: add or extend the source material for this topic, then rebuild the index."
                    )
                    conn.execute("UPDATE knowledge_gaps SET issue_draft = ? WHERE id = ?", (draft, row[0]))
        conn.commit()
    finally:
        conn.close()
    return fired


def open_gaps(db_path: str | Path, *, limit: int = 8) -> list[tuple[str, int, str]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT topic, dislikes, sample_question FROM knowledge_gaps WHERE status = 'open' "
            "ORDER BY dislikes DESC, updated_at DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
    finally:
        conn.close()
    return [(str(topic), int(count), str(sample)) for topic, count, sample in rows]


def resolve(db_path: str | Path, topic: str) -> bool:
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            "UPDATE knowledge_gaps SET status = 'resolved', updated_at = ? WHERE topic = ?",
            (_iso_now(), topic.strip().lower()),
        )
        conn.commit()
        return bool(cursor.rowcount)
    finally:
        conn.close()
