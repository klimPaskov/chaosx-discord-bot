"""Durable member memory: facts that survive profile rewrites.

Hoops, 2026-10-25: "there should be like automatic memory compacting system, like in hermes. So old important
information is always remembered."

A profile is a *summary of the recent self* — every rebuild can drop a detail that mattered. This module keeps a
second layer: individual facts, each with a first-seen and last-seen date and a hit count, so a fact mentioned
once long ago and never contradicted stays available while chit-chat fades.

How it works:
1. When a profile is rebuilt, `absorb_profile()` splits it into facts and upserts them. A fact seen again bumps
   `hits` and `last_seen` instead of duplicating.
2. `compact()` drops facts that have not been re-seen in a long time and keeps the strongest `keep` facts, so the
   table cannot grow without bound.
3. Prompts read `durable_facts()` (a few short lines), not the whole table.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_FACT_CHARS = 240
FACT_LIMIT_PER_MEMBER = 120
DECAY_DAYS = 240
PROMPT_FACTS = 8

_NOISE_PREFIX = re.compile(r"(?i)^(user profile for .*?:\s*|facts?\s*[:\-]\s*|profile\s*[:\-]\s*)")
_HEADING = re.compile(r"^[#*\s]*(preferences|notable facts|behaviour|behavior|facts|summary|style)\b.*$", re.I)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_memory_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author_id INTEGER NOT NULL,
            fact TEXT NOT NULL,
            normalised TEXT NOT NULL,
            hits INTEGER NOT NULL DEFAULT 1,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            UNIQUE(author_id, normalised)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_member ON user_memory_facts(author_id, last_seen)")
    return conn


def _normalise(fact: str) -> str:
    text = re.sub(r"[^a-z0-9 ]+", " ", fact.lower())
    return " ".join(text.split())[:200]


def split_facts(profile_text: str) -> list[str]:
    """Profile prose → the individual facts worth remembering."""
    facts: list[str] = []
    for raw_line in (profile_text or "").splitlines():
        line = raw_line.strip()
        if not line or _HEADING.match(line):
            continue
        line = _NOISE_PREFIX.sub("", line)
        line = re.sub(r"^[-*•\d.)\s]+", "", line)
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z])", line):
            sentence = sentence.strip(" -•*")
            if len(sentence) < 25 or len(sentence) > MAX_FACT_CHARS:
                continue
            if sentence.lower().startswith(("user profile", "this member", "the member")):
                sentence = re.sub(r"(?i)^(this|the) member\s+", "", sentence)
            facts.append(sentence)
    seen: set[str] = set()
    unique: list[str] = []
    for fact in facts:
        key = _normalise(fact)
        if key and key not in seen:
            seen.add(key)
            unique.append(fact)
    return unique[:40]


def absorb_profile(db_path: str | Path, author_id: int, profile_text: str) -> int:
    """Fold a freshly rebuilt profile into the durable layer. Returns how many facts were stored."""
    facts = split_facts(profile_text)
    if not facts:
        return 0
    now = _iso(_now())
    conn = _connect(db_path)
    try:
        for fact in facts:
            conn.execute(
                """
                INSERT INTO user_memory_facts (author_id, fact, normalised, hits, first_seen, last_seen)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(author_id, normalised)
                DO UPDATE SET hits = hits + 1, last_seen = excluded.last_seen, fact = excluded.fact
                """,
                (int(author_id), fact, _normalise(fact), now, now),
            )
        conn.commit()
    finally:
        conn.close()
    return len(facts)


def compact(db_path: str | Path, author_id: int, *, keep: int = FACT_LIMIT_PER_MEMBER, decay_days: int = DECAY_DAYS) -> int:
    """Drop facts that went quiet for `decay_days` and beyond `keep`, strongest first. Returns rows removed."""
    cutoff = _iso(_now() - timedelta(days=decay_days))
    conn = _connect(db_path)
    try:
        conn.execute(
            "DELETE FROM user_memory_facts WHERE author_id = ? AND last_seen < ?",
            (int(author_id), cutoff),
        )
        keep_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM user_memory_facts WHERE author_id = ? ORDER BY hits DESC, last_seen DESC LIMIT ?",
                (int(author_id), max(1, keep)),
            ).fetchall()
        ]
        if keep_ids:
            placeholders = ",".join("?" for _ in keep_ids)
            cursor = conn.execute(
                f"DELETE FROM user_memory_facts WHERE author_id = ? AND id NOT IN ({placeholders})",
                (int(author_id), *keep_ids),
            )
            removed = cursor.rowcount or 0
        else:
            removed = 0
        conn.commit()
    finally:
        conn.close()
    return removed


def durable_facts(db_path: str | Path, author_id: int, *, limit: int = PROMPT_FACTS) -> list[str]:
    """The oldest-but-strongest facts, short enough to sit in a prompt."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT fact FROM user_memory_facts WHERE author_id = ?
            ORDER BY hits DESC, first_seen ASC LIMIT ?
            """,
            (int(author_id), max(1, limit)),
        ).fetchall()
    finally:
        conn.close()
    out: list[str] = []
    for (fact,) in rows:
        text = str(fact).strip()
        if text and text not in out:
            out.append(text)
    return out


def facts_block(db_path: str | Path, author_id: int, *, limit: int = PROMPT_FACTS) -> str:
    facts = durable_facts(db_path, author_id, limit=limit)
    if not facts:
        return ""
    return "Long-term memory (older details kept on purpose):\n" + "\n".join(f"- {fact}" for fact in facts)


def forget(db_path: str | Path, author_id: int) -> int:
    """Delete everything this module remembers about one member."""
    conn = _connect(db_path)
    try:
        cursor = conn.execute("DELETE FROM user_memory_facts WHERE author_id = ?", (int(author_id),))
        removed = cursor.rowcount or 0
        conn.execute("DELETE FROM user_profiles WHERE author_id = ?", (int(author_id),))
        conn.commit()
    finally:
        conn.close()
    return removed


def stats(db_path: str | Path) -> tuple[int, int]:
    """(members with facts, total facts) — for the owner digest."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT COUNT(DISTINCT author_id), COUNT(*) FROM user_memory_facts"
        ).fetchone()
    finally:
        conn.close()
    return int(rows[0] or 0), int(rows[1] or 0)
