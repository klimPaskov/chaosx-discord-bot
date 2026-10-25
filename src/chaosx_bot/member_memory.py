"""Durable member memory: facts that survive profile rewrites, and stay honest about themselves.

Hoops, 2026-10-25, after the suggestions were accepted:
- contradiction handling — "I stopped playing HOI4" supersedes "plays HOI4 daily" instead of sitting beside it
- per-fact age — a fact carries when it was last heard, so prompts can say "heard 4 months ago"
- semantic dedupe — "prefers short answers" and "dislikes long replies" collapse into one slot
- judgement, not first-person-only — third-party statements still count, but jokes, sarcasm, hyperbole and
  hypotheticals never become facts, including when someone jokes about themselves

A profile is a summary of the recent self and every rebuild can drop a detail that mattered. This layer keeps
individual facts with status, confidence, age and a hit count, and compacts itself.
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
DEDUPE_SIMILARITY = 0.55

_NOISE_PREFIX = re.compile(r"(?i)^(user profile for .*?:\s*|facts?\s*[:\-]\s*|profile\s*[:\-]\s*)")
_HEADING = re.compile(r"^[#*\s]*(preferences|notable facts|behaviour|behavior|facts|summary|style)\b.*$", re.I)
_BULLET = re.compile(r"^[-*•\d.)\s]+")

# Jokes, sarcasm and hyperbole are the facts that embarrass a memory system. These markers are deliberately
# blunt: a false negative costs one dropped joke; a false positive puts a joke about someone in their profile
# forever. Hoops: "if a user says that they are gay jokingly, then it shouldn't be saved as a fact."
_JOKE_RE = re.compile(
    r"(?i)(\bjok(?:e|ing|ingly)\b|\bjk\b|\blol\b|\blmao\b|\brofl\b|😂|🤣|/s\b|\bsarcas|\bkidding\b|"
    r"\bjust kidding\b|\bnot really\b|\bhypothetical|\bimagine if\b|\bpretend|\bwhat if i\b|"
    r"\bfor the meme\b|\bshitpost|\bcopypasta|\bbeing ironic\b|\bi'?m so gay\b|\bi am so gay\b|"
    r"\bjust a joke\b|\bmeme answer\b)"
)

# "Nothing observed", "No personal details" and "Display name: X" are notes about the absence of facts, not facts.
_NON_FACT = re.compile(
    r"(?i)^(no\b|none\b|nothing\b|unknown\b|not (?:much|clear|enough|enough information)\b|no personal\b|"
    r"display name\b|one-off\b|one off\b|recurring .{0,30}(?:line|phrase)\b|unclear\b)"
)

_NEGATION = re.compile(
    r"(?i)\b(not|no longer|never|stopped|quit|quitting|left|leaving|dropped|dropping|cancelled|canceled|"
    r"uninstalled|refunded|deleted)\b"
)

_STOP = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "with", "at", "by", "from", "as",
    "is", "are", "was", "were", "be", "been", "has", "have", "had", "does", "did", "will", "would", "can",
    "could", "should", "very", "really", "also", "their", "his", "her", "him", "she", "he", "they", "them",
    "that", "this", "these", "those", "it", "its", "i", "me", "my", "we", "our", "you", "your", "not",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def age_label(last_seen: str) -> str:
    """Human age for a fact: 'heard today', 'heard 3 weeks ago', 'heard 4 months ago'."""
    moment = _parse(last_seen)
    if moment is None:
        return ""
    days = max(0, (_now() - moment).days)
    if days == 0:
        return "heard today"
    if days == 1:
        return "heard yesterday"
    if days < 14:
        return f"heard {days} days ago"
    if days < 60:
        weeks = days // 7
        return f"heard {weeks} week{'s' if weeks > 1 else ''} ago"
    if days < 730:
        months = days // 30
        return f"heard {months} month{'s' if months > 1 else ''} ago"
    years = days // 365
    return f"heard {years} year{'s' if years > 1 else ''} ago"


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 2}


def _normalise(fact: str) -> str:
    text = re.sub(r"[^a-z0-9 ]+", " ", fact.lower())
    return " ".join(text.split())[:200]


def _proper_nouns(text: str) -> set[str]:
    """Capitalised words past the first position: names, places, products. They must match to merge."""
    words = re.findall(r"[A-Za-z0-9][\w'-]*", text or "")
    return {word.lower() for index, word in enumerate(words) if index > 0 and word[:1].isupper()}


def _similar(a: str, b: str) -> bool:
    left, right = _tokens(a), _tokens(b)
    if not left or not right:
        return False
    if _proper_nouns(a) != _proper_nouns(b):
        return False
    shared = len(left & right)
    if shared < 4:
        return False
    ratio = shared / len(left | right)
    # Same wording, or a lot of shared substance: two facts that differ by one word keep both slots.
    return ratio >= 0.7 or (ratio >= DEDUPE_SIMILARITY and shared >= 6)


def _contradicts(a: str, b: str) -> bool:
    """Same subject, opposite polarity: 'plays HOI4 daily' vs 'stopped playing HOI4'."""
    left, right = _tokens(a), _tokens(b)
    if not left or not right:
        return False
    shared = left & right
    # Opposite polarity about the same object: "plays HOI4 daily" vs "stopped playing HOI4". Two shared
    # content words and a low overall overlap is enough — the negation is the signal, not the wording.
    if len(shared) < 2 or len(shared) / len(left | right) < 0.15:
        return False
    return bool(_NEGATION.search(a)) != bool(_NEGATION.search(b))


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row
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
            status TEXT NOT NULL DEFAULT 'active',
            confidence REAL NOT NULL DEFAULT 0.7,
            source TEXT NOT NULL DEFAULT 'statement',
            channel_id INTEGER,
            UNIQUE(author_id, normalised)
        )
        """
    )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(user_memory_facts)")}
    for column, ddl in (
        ("status", "ALTER TABLE user_memory_facts ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"),
        ("confidence", "ALTER TABLE user_memory_facts ADD COLUMN confidence REAL NOT NULL DEFAULT 0.7"),
        ("source", "ALTER TABLE user_memory_facts ADD COLUMN source TEXT NOT NULL DEFAULT 'statement'"),
        ("channel_id", "ALTER TABLE user_memory_facts ADD COLUMN channel_id INTEGER"),
    ):
        if column not in columns:
            conn.execute(ddl)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_facts_member ON user_memory_facts(author_id, status, last_seen)"
    )
    return conn


def is_joke(fact: str) -> bool:
    return bool(_JOKE_RE.search(fact or ""))


def is_non_fact(fact: str) -> bool:
    return bool(_NON_FACT.match((fact or "").strip()))


def split_facts(profile_text: str) -> list[str]:
    """Profile prose → the individual facts worth remembering (jokes dropped here, never stored)."""
    facts: list[str] = []
    for raw_line in (profile_text or "").splitlines():
        line = raw_line.strip()
        if not line or _HEADING.match(line) or is_joke(line) or is_non_fact(line):
            continue
        line = _NOISE_PREFIX.sub("", line)
        line = _BULLET.sub("", line)
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z])", line):
            sentence = sentence.strip(" -•*")
            if len(sentence) < 25 or len(sentence) > MAX_FACT_CHARS or is_joke(sentence) or is_non_fact(sentence):
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


def remember(
    db_path: str | Path,
    author_id: int,
    fact: str,
    *,
    confidence: float = 0.7,
    source: str = "statement",
    channel_id: int | None = None,
) -> str:
    """Store one fact. Returns 'new', 'merged', 'superseded' or 'skipped'."""
    fact = (fact or "").strip()
    if len(fact) < 20 or len(fact) > MAX_FACT_CHARS or is_joke(fact) or is_non_fact(fact):
        return "skipped"
    now = _iso(_now())
    key = _normalise(fact)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM user_memory_facts WHERE author_id = ? AND normalised = ?",
            (int(author_id), key),
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE user_memory_facts SET hits = hits + 1, last_seen = ?, status = 'active', "
                "confidence = MAX(confidence, ?) WHERE id = ?",
                (now, float(confidence), row["id"]),
            )
            conn.commit()
            return "merged"

        superseded = False
        for candidate in conn.execute(
            "SELECT id, fact, confidence FROM user_memory_facts WHERE author_id = ? AND status = 'active'",
            (int(author_id),),
        ).fetchall():
            if _contradicts(candidate["fact"], fact):
                conn.execute("UPDATE user_memory_facts SET status = 'stale' WHERE id = ?", (candidate["id"],))
                superseded = True
                continue
            if _similar(candidate["fact"], fact):
                keep_new = float(confidence) > float(candidate["confidence"] or 0)
                conn.execute(
                    "UPDATE user_memory_facts SET hits = hits + 1, last_seen = ?, confidence = MAX(confidence, ?), "
                    "fact = CASE WHEN ? THEN ? ELSE fact END WHERE id = ?",
                    (now, float(confidence), 1 if keep_new else 0, fact, candidate["id"]),
                )
                conn.commit()
                return "merged"

        conn.execute(
            "INSERT INTO user_memory_facts (author_id, fact, normalised, hits, first_seen, last_seen, status, "
            "confidence, source, channel_id) VALUES (?, ?, ?, 1, ?, ?, 'active', ?, ?, ?)",
            (int(author_id), fact, key, now, now, float(confidence), source, channel_id),
        )
        conn.commit()
        return "superseded" if superseded else "new"
    finally:
        conn.close()


def absorb_profile(db_path: str | Path, author_id: int, profile_text: str, **kwargs) -> int:
    """Fold a rebuilt profile into the durable layer. Returns how many facts were accepted."""
    accepted = 0
    for fact in split_facts(profile_text):
        if remember(db_path, author_id, fact, **kwargs) != "skipped":
            accepted += 1
    return accepted


def compact(
    db_path: str | Path, author_id: int, *, keep: int = FACT_LIMIT_PER_MEMBER, decay_days: int = DECAY_DAYS
) -> int:
    """Retire what went quiet and keep the strongest `keep` active facts. Returns rows removed."""
    cutoff = _iso(_now() - timedelta(days=decay_days))
    conn = _connect(db_path)
    try:
        conn.execute(
            "DELETE FROM user_memory_facts WHERE author_id = ? AND status = 'stale' AND last_seen < ?",
            (int(author_id), cutoff),
        )
        conn.execute(
            "UPDATE user_memory_facts SET status = 'stale' WHERE author_id = ? AND status = 'active' AND last_seen < ?",
            (int(author_id), cutoff),
        )
        keep_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM user_memory_facts WHERE author_id = ? AND status = 'active' "
                "ORDER BY hits DESC, confidence DESC, last_seen DESC LIMIT ?",
                (int(author_id), max(1, keep)),
            ).fetchall()
        ]
        removed = 0
        if keep_ids:
            placeholders = ",".join("?" for _ in keep_ids)
            removed = (
                conn.execute(
                    f"DELETE FROM user_memory_facts WHERE author_id = ? AND status = 'active' "
                    f"AND id NOT IN ({placeholders})",
                    (int(author_id), *keep_ids),
                ).rowcount
                or 0
            )
        conn.commit()
    finally:
        conn.close()
    return removed


def durable_facts(
    db_path: str | Path, author_id: int, *, limit: int = PROMPT_FACTS, with_age: bool = True
) -> list[str]:
    """The strongest active facts, each dated with when it was last confirmed."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT fact, last_seen FROM user_memory_facts WHERE author_id = ? AND status = 'active' "
            "ORDER BY hits DESC, confidence DESC, last_seen DESC LIMIT ?",
            (int(author_id), max(1, limit)),
        ).fetchall()
    finally:
        conn.close()
    out: list[str] = []
    for row in rows:
        text = str(row["fact"]).strip()
        if not text:
            continue
        if with_age:
            age = age_label(str(row["last_seen"]))
            if age:
                text = f"{text} ({age})"
        if text not in out:
            out.append(text)
    return out


def facts_block(db_path: str | Path, author_id: int, *, limit: int = PROMPT_FACTS) -> str:
    facts = durable_facts(db_path, author_id, limit=limit)
    if not facts:
        return ""
    return (
        "Long-term memory (older details kept on purpose; each age is when it was last confirmed):\n"
        + "\n".join(f"- {fact}" for fact in facts)
    )


def forget(db_path: str | Path, author_id: int) -> int:
    """Delete everything this module remembers about one member."""
    conn = _connect(db_path)
    try:
        removed = conn.execute(
            "DELETE FROM user_memory_facts WHERE author_id = ?", (int(author_id),)
        ).rowcount or 0
        conn.execute("DELETE FROM user_profiles WHERE author_id = ?", (int(author_id),))
        conn.commit()
    finally:
        conn.close()
    return removed


def stats(db_path: str | Path) -> tuple[int, int]:
    """(members with active facts, total active facts) — for the owner digest."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(DISTINCT author_id), COUNT(*) FROM user_memory_facts WHERE status = 'active'"
        ).fetchone()
    finally:
        conn.close()
    return int(row[0] or 0), int(row[1] or 0)


def digest(db_path: str | Path, *, days: int = 7) -> dict:
    """Material for the weekly owner digest."""
    since = _iso(_now() - timedelta(days=days))
    quiet_before = _iso(_now() - timedelta(days=60))
    conn = _connect(db_path)
    try:
        new_facts = conn.execute(
            "SELECT COUNT(*) FROM user_memory_facts WHERE first_seen >= ? AND status = 'active'", (since,)
        ).fetchone()[0]
        superseded = conn.execute(
            "SELECT COUNT(*) FROM user_memory_facts WHERE status = 'stale' AND last_seen >= ?", (since,)
        ).fetchone()[0]
        active_members = conn.execute(
            "SELECT COUNT(DISTINCT author_id) FROM user_memory_facts WHERE status = 'active'"
        ).fetchone()[0]
        quiet = conn.execute(
            "SELECT author_id, COUNT(*) n FROM user_memory_facts WHERE status = 'active' "
            "GROUP BY author_id HAVING MAX(last_seen) < ? ORDER BY n DESC LIMIT 10",
            (quiet_before,),
        ).fetchall()
        recent = conn.execute(
            "SELECT fact FROM user_memory_facts WHERE first_seen >= ? AND status = 'active' "
            "ORDER BY id DESC LIMIT 10",
            (since,),
        ).fetchall()
    finally:
        conn.close()
    return {
        "new_facts": int(new_facts),
        "superseded": int(superseded),
        "active_members": int(active_members),
        "quiet_members": [(int(row["author_id"]), int(row["n"])) for row in quiet],
        "recent_facts": [str(row["fact"]) for row in recent],
    }
