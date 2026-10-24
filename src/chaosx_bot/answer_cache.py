"""Cache for repeated public questions.

A community asks the same question dozens of times ("how does the chaos meter work", "when is release"). Each
answer costs a retrieval pass and a model round trip. Identical, non-live questions are served from here instead.

Three rules keep it honest:

* Anything touching current state is never cached -- figures go stale, and a stale number is worse than a slow answer.
* Entries are stamped with the index revision they were produced from, so a rebuilt index invalidates them for free.
* Near-duplicates count: "how does the chaos meter work" and "how does the chaos meter actually work" are the same
  question with different filler.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

from .db import connect

_SCHEMA = """
CREATE TABLE IF NOT EXISTS answer_cache (
    key TEXT PRIMARY KEY,
    answer TEXT NOT NULL,
    at REAL NOT NULL,
    index_commit TEXT NOT NULL DEFAULT ''
)
"""

_PUNCT_RE = re.compile(r"[^\w\s]")
_SPACE_RE = re.compile(r"\s+")

# Same idea as the live-facts gate: if the question is about the present, do not serve a stored answer.
_LIVE_RE = re.compile(
    r"\b(?:latest|current|currently|today|tonight|now|right now|status|news|recent|recently|this (?:week|month|"
    r"year)|update[sd]?|release[sd]?|patch|how many|how much|who is online|who is active)\b",
    re.IGNORECASE,
)

# Filler that does not change what is being asked. Removing it makes near-duplicate matching cheap and stable.
_FILLER = {
    "the", "a", "an", "please", "actually", "just", "really", "so", "then", "and", "to", "of", "for", "in", "on",
    "does", "do", "did", "is", "are", "was", "were", "it", "that", "this", "about", "me", "tell", "explain",
}

MAX_ANSWER_CHARS = 1800
NEAR_DUPLICATE_THRESHOLD = 0.85


def normalize(question: str) -> str:
    text = _PUNCT_RE.sub(" ", (question or "").lower())
    return _SPACE_RE.sub(" ", text).strip()


def _tokens(question: str) -> set[str]:
    return {t for t in normalize(question).split() if t and t not in _FILLER}


def similarity(left: str, right: str) -> float:
    """Jaccard over content tokens. 1.0 means the same question with different filler."""
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def is_live(question: str) -> bool:
    return bool(_LIVE_RE.search(question or ""))


class AnswerCache:
    def __init__(self, db_path: Path | str, *, ttl: int = 3600, enabled: bool = True) -> None:
        self.db_path = Path(db_path)
        self.ttl = ttl
        self.enabled = enabled
        self.hits = 0
        self.misses = 0

    def _connect(self) -> sqlite3.Connection:
        conn = connect(self.db_path)
        conn.execute(_SCHEMA)
        # Databases created before revision stamping have no commit column; add it in place.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(answer_cache)")}
        if "index_commit" not in columns:
            conn.execute("ALTER TABLE answer_cache ADD COLUMN index_commit TEXT NOT NULL DEFAULT ''")
            conn.commit()
        return conn

    def _usable(self, question: str) -> bool:
        return bool(self.enabled and question and not is_live(question))

    def get(self, question: str, *, live_commit: str = "") -> str | None:
        """Return a stored answer for this question, or None."""
        if not self._usable(question):
            self.misses += 1
            return None
        key = normalize(question)
        if not key:
            self.misses += 1
            return None
        try:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT answer, at, index_commit FROM answer_cache WHERE key = ?", (key,)
                ).fetchone()
                if row is None:
                    candidates = conn.execute(
                        "SELECT key, answer, at, index_commit FROM answer_cache ORDER BY at DESC LIMIT 200"
                    ).fetchall()
                else:
                    candidates = []
            finally:
                conn.close()
        except Exception:
            self.misses += 1
            return None

        if row is not None:
            answer, at, commit = row
            if self._fresh(at) and self._same_revision(commit, live_commit):
                self.hits += 1
                return str(answer)
            self.misses += 1
            return None

        if not live_commit:
            self.misses += 1
            return None  # without a revision, near-duplicates cannot be trusted to the current index
        best: tuple[float, str] | None = None
        for stored_key, answer, at, commit in candidates:
            if not self._fresh(at) or not self._same_revision(commit, live_commit):
                continue
            score = similarity(key, stored_key)
            if score >= NEAR_DUPLICATE_THRESHOLD and (best is None or score > best[0]):
                best = (score, str(answer))
        if best is None:
            self.misses += 1
            return None
        self.hits += 1
        return best[1]

    def put(self, question: str, answer: str, *, commit: str = "") -> None:
        if not self._usable(question):
            return
        if not answer or len(answer) > MAX_ANSWER_CHARS:
            return  # long, tailored answers are not worth replaying verbatim
        key = normalize(question)
        if not key:
            return
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO answer_cache (key, answer, at, index_commit) VALUES (?, ?, ?, ?)",
                    (key, answer, time.time(), commit),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass

    def stats_line(self) -> str:
        total = self.hits + self.misses
        share = (100.0 * self.hits / total) if total else 0.0
        return f"answer cache: {self.hits} hits / {self.misses} misses ({share:.0f}%)"

    def _fresh(self, at: float) -> bool:
        return time.time() - float(at) <= self.ttl

    @staticmethod
    def _same_revision(stored: str, live_commit: str) -> bool:
        return (not live_commit) or (str(stored or "") == str(live_commit))
