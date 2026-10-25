"""Per-user preferences and answer feedback.

Two things Hoops asked for that need to outlive a restart:

- a member can tell ChaosX to stop (or resume) answering their questions unprompted, permanently or for a
  while;
- 👍/👎 on an answer is stored, with an optional note, so the bot can lean towards what that member finds
  useful and so the owner can review what went wrong across all answers later.

Nothing here touches Discord: pure SQLite so the logic is testable and the bot layer stays thin.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

AUTO_ANSWER_MODES = {"on", "off", "temporary"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id TEXT PRIMARY KEY,
    auto_answer_mode TEXT NOT NULL DEFAULT 'on',
    auto_answer_until TEXT,
    detail_level TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS answer_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT,
    user_id TEXT NOT NULL,
    question TEXT,
    answer TEXT,
    rating TEXT NOT NULL,
    comment TEXT,
    created_at TEXT NOT NULL,
    reviewed INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_answer_feedback_once
    ON answer_feedback(message_id, user_id, rating)
    WHERE message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_answer_feedback_user ON answer_feedback(user_id);
CREATE TABLE IF NOT EXISTS feedback_prompt_prefs (
    user_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'on',
    until TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_answer_feedback_created ON answer_feedback(created_at);
"""


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.executescript(_SCHEMA)
    return conn


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def get_pref(db_path: str | Path, user_id: int | str) -> dict:
    """The member's preferences, defaulting to "answer me unprompted"."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT auto_answer_mode, auto_answer_until, detail_level FROM user_prefs WHERE user_id = ?",
            (str(user_id),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"auto_answer_mode": "on", "auto_answer_until": None, "detail_level": None}
    return {"auto_answer_mode": row[0], "auto_answer_until": row[1], "detail_level": row[2]}


def set_auto_answer(
    db_path: str | Path,
    user_id: int | str,
    *,
    enabled: bool,
    hours: float | None = None,
) -> dict:
    """Turn unprompted answers on or off for one member.

    `hours=None` with enabled=True means "permanently". `hours` with enabled=True means "for the next N
    hours, then back on". enabled=False always means off until they turn it back on.
    """
    if enabled and hours:
        mode = "temporary"
        until = _now() + timedelta(hours=float(hours))
    elif enabled:
        mode = "on"
        until = None
    else:
        mode = "off"
        until = None
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO user_prefs (user_id, auto_answer_mode, auto_answer_until, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                auto_answer_mode = excluded.auto_answer_mode,
                auto_answer_until = excluded.auto_answer_until,
                updated_at = excluded.updated_at
            """,
            (str(user_id), mode, _iso(until) if until else None, _iso(_now())),
        )
        conn.commit()
    finally:
        conn.close()
    return {"auto_answer_mode": mode, "auto_answer_until": _iso(until) if until else None}


def set_detail_level(db_path: str | Path, user_id: int | str, level: str | None) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO user_prefs (user_id, detail_level, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET detail_level = excluded.detail_level, updated_at = excluded.updated_at
            """,
            (str(user_id), level, _iso(_now())),
        )
        conn.commit()
    finally:
        conn.close()


def auto_answer_allows(db_path: str | Path, user_id: int | str) -> bool:
    """Whether an unprompted answer may be posted for this member right now."""
    pref = get_pref(db_path, user_id)
    mode = pref["auto_answer_mode"] or "on"
    if mode == "off":
        return False
    if mode == "temporary":
        until = parse_iso(pref["auto_answer_until"])
        if until is None:
            return True
        if until <= _now():
            # The window ran out: back to normal, and record that so the panel shows the truth.
            set_auto_answer(db_path, user_id, enabled=True)
            return True
    return True


def describe_auto_answer(pref: dict) -> str:
    mode = pref.get("auto_answer_mode") or "on"
    if mode == "off":
        return "off — I only answer when you mention me or reply to me"
    if mode == "temporary":
        until = parse_iso(pref.get("auto_answer_until"))
        if until:
            return f"on until {until.strftime('%Y-%m-%d %H:%M')} UTC, then back to normal"
    return "on — I answer your questions even without a mention"


def record_feedback(
    db_path: str | Path,
    *,
    user_id: int | str,
    rating: str,
    message_id: int | str | None = None,
    question: str = "",
    answer: str = "",
    comment: str = "",
) -> int:
    rating = "up" if str(rating).lower() in {"up", "like", "👍", "+1"} else "down"
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO answer_feedback
                (message_id, user_id, question, answer, rating, comment, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(message_id) if message_id is not None else None,
                str(user_id),
                question[:2000],
                answer[:4000],
                rating,
                comment[:2000],
                _iso(_now()),
            ),
        )
        conn.commit()
        if cursor.rowcount and cursor.lastrowid:
            return int(cursor.lastrowid)
        # Already rated with the same thumb (a reaction after the button, or a second click).
        row = conn.execute(
            "SELECT id FROM answer_feedback WHERE message_id = ? AND user_id = ? AND rating = ?",
            (
                str(message_id) if message_id is not None else None,
                str(user_id),
                rating,
            ),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def add_feedback_comment(db_path: str | Path, feedback_id: int, comment: str) -> bool:
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            "UPDATE answer_feedback SET comment = ? WHERE id = ?", (comment[:2000], int(feedback_id))
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def user_feedback_profile(db_path: str | Path, user_id: int | str) -> dict:
    """What this member has told us with their thumbs, used to bias the next answer."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT rating, COUNT(*) FROM answer_feedback WHERE user_id = ? GROUP BY rating",
            (str(user_id),),
        ).fetchall()
    finally:
        conn.close()
    counts = {str(rating): int(count) for rating, count in rows}
    return {"up": counts.get("up", 0), "down": counts.get("down", 0)}


def feedback_summary(db_path: str | Path, *, days: int = 7, limit: int = 20) -> str:
    """Owner-facing digest: how members rated recent answers and what they said."""
    since = _iso(_now() - timedelta(days=days))
    conn = _connect(db_path)
    try:
        totals = conn.execute(
            "SELECT rating, COUNT(*) FROM answer_feedback WHERE created_at >= ? GROUP BY rating", (since,)
        ).fetchall()
        rows = conn.execute(
            """
            SELECT user_id, rating, comment, question, created_at FROM answer_feedback
            WHERE created_at >= ? AND (comment IS NOT NULL AND comment != '' OR rating = 'down')
            ORDER BY id DESC LIMIT ?
            """,
            (since, limit),
        ).fetchall()
        overall = conn.execute("SELECT COUNT(*) FROM answer_feedback").fetchone()[0]
    finally:
        conn.close()
    counts = {rating: count for rating, count in totals}
    lines = [
        f"Feedback, last {days} day(s): 👍 {counts.get('up', 0)} · 👎 {counts.get('down', 0)} "
        f"· all time {overall}",
        "",
    ]
    if not rows:
        lines.append("No notes or dislikes in this window.")
        return "\n".join(lines)
    for user_id, rating, comment, question, created_at in rows:
        mark = "👍" if rating == "up" else "👎"
        question_text = (question or "").strip().replace("\n", " ")[:110]
        comment_text = (comment or "").strip().replace("\n", " ")[:200]
        line = f"{mark} <@{user_id}> {created_at[:10]} — {question_text or '(no question stored)'}"
        if comment_text:
            line += f"\n   note: {comment_text}"
        lines.append(line)
    return "\n".join(lines)


_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.S)


def parse_quick_actions(text: str, *, limit: int = 3) -> list[tuple[str, str]]:
    """Read the model's suggested follow-ups.

    Accepts a bare JSON array, a fenced one, or objects with label/prompt (or title/question). Anything
    malformed yields nothing rather than a half-parsed button, and labels are trimmed to Discord's 80-char
    button limit.
    """
    raw = (text or "").strip()
    if not raw:
        return []
    match = _JSON_ARRAY_RE.search(raw)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    actions: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in data:
        if isinstance(item, dict):
            label = str(item.get("label") or item.get("title") or "").strip()
            prompt = str(item.get("prompt") or item.get("question") or label).strip()
        elif isinstance(item, str):
            label = item.strip()
            prompt = label
        else:
            continue
        if not label or not prompt:
            continue
        label = re.sub(r"\s+", " ", label)[:80]
        if label.casefold() in seen:
            continue
        seen.add(label.casefold())
        actions.append((label, prompt[:300]))
        if len(actions) >= limit:
            break
    return actions


def feedback_prompt_allows(db_path: str | Path, user_id: int | str) -> bool:
    """Whether to offer the rating panel under this member's next automatic answer.

    Hoops: the feedback prompt "can also be dismissed for set amount of time or permanently" — a dismissal
    only silences the prompt, rating is still possible with the 👍/👎 reactions.
    """
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT mode, until FROM feedback_prompt_prefs WHERE user_id = ?", (str(user_id),)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return True
    mode, until_raw = row
    if mode == "off":
        return False
    if mode == "snooze":
        until = parse_iso(until_raw)
        if until is None:
            return True
        if until <= _now():
            return True
    return True


def dismiss_feedback_prompt(db_path: str | Path, user_id: int | str, *, hours: float | None) -> dict:
    """Snooze the feedback prompt for `hours`, or turn it off permanently when hours is None."""
    mode = "off" if hours is None else "snooze"
    until = None if hours is None else _iso(_now() + timedelta(hours=float(hours)))
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO feedback_prompt_prefs (user_id, mode, until, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET mode = excluded.mode, until = excluded.until,
                updated_at = excluded.updated_at
            """,
            (str(user_id), mode, until, _iso(_now())),
        )
        conn.commit()
    finally:
        conn.close()
    return {"mode": mode, "until": until}


def describe_feedback_prompt(db_path: str | Path, user_id: int | str) -> str:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT mode, until FROM feedback_prompt_prefs WHERE user_id = ?", (str(user_id),)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return "shown after my automatic answers"
    mode, until_raw = row
    if mode == "off":
        return "off — rate any answer with 👍/👎 instead"
    until = parse_iso(until_raw)
    if until and until > _now():
        return f"paused until {until.strftime('%Y-%m-%d %H:%M')} UTC"
    return "shown after my automatic answers"


def message_rating(db_path: str | Path, message_id: int | str, user_id: int | str) -> str | None:
    """The rating this member already gave that message, so a reaction does not double-store."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT rating FROM answer_feedback WHERE message_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1",
            (str(message_id), str(user_id)),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def stored_turn(db_path: str | Path, message_id: int | str) -> tuple[str, str]:
    """The question and answer behind a bot message, when the bot kept them (feedback context)."""
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        row = conn.execute(
            """
            SELECT request, output_excerpt FROM message_ask_memory
            WHERE CAST(bot_message_id AS TEXT) = ? ORDER BY rowid DESC LIMIT 1
            """,
            (str(message_id),),
        ).fetchone()
    except sqlite3.Error:
        return "", ""
    finally:
        conn.close()
    if not row:
        return "", ""
    return str(row[0] or ""), str(row[1] or "")
