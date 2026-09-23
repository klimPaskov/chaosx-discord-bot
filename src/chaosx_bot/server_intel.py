"""Server intelligence: the private digest Hoops gets, and archive Q&A.

Two jobs:

* **Weekly intel digest** (delivered privately to the owner) — what people asked, where the bot
  could not help, activity, moderation, and the owner's own admin actions. Everything comes from
  the bot's own tables; nothing is inferred beyond what the rows say.
* **Archive Q&A** — "what did we decide about X?" answered from the message archive with real
  citations (channel, date, author, jump link), never from the model's imagination.

Both share one rule with the routine posts: facts are counted or quoted, and anything missing is
reported as missing.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .routine_posts import sanitize_post

WINDOW_DAYS = 7
MAX_ASK_SAMPLES = 60
MAX_REFUSED_SAMPLES = 12
MAX_ARCHIVE_HITS = 25
MAX_INTEL_CHARS = 2000
PUBLIC_ASK_REDIRECT_MARKER = "I can only answer Chaos Redux questions"

# Words that carry no topical signal when grouping what people asked.
QUESTION_STOPWORDS = {
    "the", "a", "an", "and", "or", "is", "are", "was", "were", "be", "to", "of", "in", "on",
    "for", "with", "how", "what", "why", "when", "where", "who", "which", "does", "do", "did",
    "can", "could", "should", "would", "i", "you", "he", "she", "it", "we", "they", "my", "your",
    "this", "that", "these", "those", "if", "then", "than", "so", "but", "not", "no", "yes",
    "there", "here", "from", "about", "into", "as", "at", "by", "have", "has", "had", "will",
    "still", "also", "just", "get", "got", "make", "made", "use", "used", "using", "any",
}


@dataclass
class IntelFacts:
    """Raw, windowed facts for one digest."""

    window_days: int = WINDOW_DAYS
    since: str = ""
    asks: list[dict[str, Any]] = field(default_factory=list)
    refused: list[dict[str, Any]] = field(default_factory=list)
    shadow: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    answers: int = 0
    banter: int = 0
    archived_messages: int = 0
    top_channels: list[tuple[int, int]] = field(default_factory=list)
    channels_active: int = 0
    new_members: int = 0
    active_members: int = 0
    # Server size, from Discord. Distinct from `known_members`, which is how many people the bot's own
    # users table has seen (a smaller, different thing that must never be reported as the member count).
    member_count: int = 0
    online_members: int = 0
    known_members: int = 0
    admin_actions: list[dict[str, Any]] = field(default_factory=list)
    failed_runs: int = 0
    summaries: list[dict[str, Any]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------------------


def _rows(db: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    try:
        cur = db.execute(sql, params)
    except sqlite3.OperationalError:
        return []
    columns = [description[0] for description in cur.description or []]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _scalar(db: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    rows = _rows(db, sql, params)
    if not rows:
        return 0
    return int(list(rows[0].values())[0] or 0)


def collect_intel(db_path: Path, *, window_days: int = WINDOW_DAYS) -> IntelFacts:
    """Read the bot's own tables for one window. Synchronous: call via asyncio.to_thread."""
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    facts = IntelFacts(window_days=window_days, since=since)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        facts.asks = _rows(
            db,
            "SELECT created_at, mode, actor_id, channel_id, request FROM message_ask_memory "
            "WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (since, MAX_ASK_SAMPLES),
        )
        facts.refused = _rows(
            db,
            "SELECT created_at, actor_id, channel_id, request FROM message_ask_memory "
            "WHERE created_at >= ? AND output_excerpt LIKE ? ORDER BY created_at DESC LIMIT ?",
            (since, f"%{PUBLIC_ASK_REDIRECT_MARKER}%", MAX_REFUSED_SAMPLES),
        )
        facts.shadow = _rows(
            db,
            "SELECT created_at, reason, confidence, actor_id, channel_id, content_excerpt FROM auto_scan_events "
            "WHERE action = 'shadow' AND created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (since, MAX_REFUSED_SAMPLES),
        )
        facts.warnings = _rows(
            db,
            "SELECT created_at, actor_id, channel_id, content_excerpt, response_excerpt FROM auto_scan_events "
            "WHERE action = 'soft_warning' AND created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (since, MAX_REFUSED_SAMPLES),
        )
        facts.answers = _scalar(
            db,
            "SELECT COUNT(*) FROM auto_scan_events WHERE action = 'answer' AND created_at >= ?",
            (since,),
        )
        facts.banter = _scalar(
            db,
            "SELECT COUNT(*) FROM auto_scan_events WHERE action = 'banter' AND created_at >= ?",
            (since,),
        )
        facts.archived_messages = _scalar(
            db, "SELECT COUNT(*) FROM message_archive WHERE created_at >= ?", (since,)
        )
        top = _rows(
            db,
            "SELECT channel_id, COUNT(*) AS n FROM message_archive WHERE created_at >= ? "
            "GROUP BY channel_id ORDER BY n DESC LIMIT 6",
            (since,),
        )
        facts.top_channels = [(int(r["channel_id"]), int(r["n"])) for r in top]
        facts.channels_active = _scalar(
            db,
            "SELECT COUNT(DISTINCT channel_id) FROM message_archive WHERE created_at >= ?",
            (since,),
        )
        facts.new_members = _scalar(
            db, "SELECT COUNT(*) FROM users WHERE first_seen_at >= ?", (since,)
        )
        facts.active_members = _scalar(
            db, "SELECT COUNT(*) FROM users WHERE last_seen_at >= ?", (since,)
        )
        # Members the bot can see (partial: no privileged members intent). NOT the server size —
        # `_build_server_intel` overwrites member_count with Discord's own count and keeps this figure
        # here under an honest label.
        facts.known_members = _scalar(db, "SELECT COUNT(*) FROM users")
        facts.admin_actions = _rows(
            db,
            "SELECT created_at, actor_id, command, summary FROM audit_log WHERE created_at >= ? "
            "ORDER BY created_at DESC LIMIT 15",
            (since,),
        )
        facts.failed_runs = _scalar(
            db,
            "SELECT COUNT(*) FROM hermes_runs WHERE created_at >= ? AND status != 'ok'",
            (since,),
        )
        facts.summaries = _rows(
            db,
            "SELECT channel_id, summary, updated_at FROM conversation_summaries "
            "WHERE updated_at >= ? ORDER BY updated_at DESC LIMIT 5",
            (since,),
        )
    finally:
        db.close()
    if not facts.asks or facts.archived_messages == 0:
        facts.missing.append("some tables were empty for this window")
    return facts


def resolve_channel(channel_id: int | None, channel_names: dict[int, str]) -> str:
    if not channel_id:
        return "unknown channel"
    name = channel_names.get(int(channel_id))
    return f"#{name}" if name else f"channel {channel_id}"


def resolve_member(actor_id: int | None, member_names: dict[int, str]) -> str:
    if not actor_id:
        return "unknown"
    name = member_names.get(int(actor_id))
    return name or f"user {actor_id}"


def jump_link(guild_id: int | None, channel_id: int | None, message_id: int | None) -> str:
    if not guild_id or not channel_id or not message_id:
        return ""
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def _quote(text: str, limit: int = 110) -> str:
    flat = re.sub(r"\s+", " ", (text or "")).strip()
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# --------------------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------------------


def _ask_lines(facts: IntelFacts, member_names: dict[int, str], channel_names: dict[int, str]) -> list[str]:
    lines: list[str] = []
    for row in facts.asks[:MAX_ASK_SAMPLES]:
        lines.append(
            f"- [{resolve_channel(row.get('channel_id'), channel_names)}] "
            f"{resolve_member(row.get('actor_id'), member_names)}: {_quote(str(row.get('request') or ''))}"
        )
    return lines


def _gap_lines(facts: IntelFacts, member_names: dict[int, str], channel_names: dict[int, str]) -> list[str]:
    lines: list[str] = []
    for row in facts.refused:
        lines.append(
            f"- out of scope: [{resolve_channel(row.get('channel_id'), channel_names)}] "
            f"{resolve_member(row.get('actor_id'), member_names)}: {_quote(str(row.get('request') or ''))}"
        )
    for row in facts.shadow:
        lines.append(
            f"- gate skipped ({_quote(str(row.get('reason') or 'no reason'), 40)}): "
            f"[{resolve_channel(row.get('channel_id'), channel_names)}] "
            f"{_quote(str(row.get('content_excerpt') or ''))}"
        )
    return lines or ["- nothing that ChaosX skipped or refused this window"]


def build_intel_prompt(
    *,
    facts: IntelFacts,
    member_names: dict[int, str],
    channel_names: dict[int, str],
    max_chars: int = MAX_INTEL_CHARS,
) -> str:
    top_channels = ", ".join(
        f"{resolve_channel(channel_id, channel_names)} ({count})" for channel_id, count in facts.top_channels
    ) or "none"
    ask_lines = "\n".join(_ask_lines(facts, member_names, channel_names)) or "- (no asks recorded)"
    gap_lines = "\n".join(_gap_lines(facts, member_names, channel_names))
    admin_lines = "\n".join(
        f"- {str(row.get('created_at'))[:16]} {row.get('command')}: {_quote(str(row.get('summary') or ''), 60)}"
        for row in facts.admin_actions
    ) or "- (no admin actions)"
    return f"""Write Hoops' private weekly server-intel digest for ChaosX.

He is the owner. This is a private briefing, not a public post: be direct, specific, and short.
Use ONLY the facts below; invent nothing, no pings or mentions.

Window: last {facts.window_days} days (since {facts.since[:16]}).

Traffic
- Messages archived: {facts.archived_messages} across {facts.channels_active} channels
- Busiest channels: {top_channels}
- Server size (Discord): {facts.member_count} members{f", {facts.online_members} online now" if facts.online_members else ""}
- Bot-side (partial — the bot has no privileged members intent): {facts.known_members} members visible to the bot, {facts.active_members} active this window, {facts.new_members} new

Bot answers
- Auto-scan answers: {facts.answers}; banter replies: {facts.banter}
- Public/admin asks recorded: {len(facts.asks)}
- Failed model runs: {facts.failed_runs}

What people asked (real requests):
{ask_lines}

Where ChaosX could not help:
{gap_lines}

Owner's admin actions:
{admin_lines}

Write exactly these sections, plain Discord markdown, no code blocks, no pings:
1. A title line: "**Server intel — week of {facts.since[:10]}**"
2. "What people wanted" — 3-5 bullets grouping the real requests into themes, with counts.
3. "Blind spots" — the skipped/refused items that look like they should have been answered, or say the gate looked right; never invent requests.
4. "Numbers" — one compact line with the counts above.
5. "Worth your attention" — at most two concrete suggestions drawn only from the facts (e.g. a
   repeated question that deserves a FAQ entry, a rule that keeps coming up).

Keep it under {max_chars} characters. Every claim must trace to a line above."""


def intel_fallback(
    *,
    facts: IntelFacts,
    member_names: dict[int, str],
    channel_names: dict[int, str],
) -> str:
    """Deterministic digest used when the model is unavailable."""
    lines = [
        f"**Server intel — week of {facts.since[:10]}**",
        "",
        "**What people wanted**",
        "\n".join(_ask_lines(facts, member_names, channel_names)[:10]) or "- (no asks recorded)",
        "",
        "**Blind spots**",
        "\n".join(_gap_lines(facts, member_names, channel_names)),
        "",
        "**Numbers**",
        f"- {facts.archived_messages} messages in {facts.channels_active} channels, "
        f"{facts.answers} auto-answers, {facts.banter} banter replies, {len(facts.asks)} asks, "
        f"{facts.new_members} new members, {facts.failed_runs} failed runs",
    ]
    if facts.missing:
        lines.append(f"- caveats: {', '.join(facts.missing)}")
    return sanitize_post("\n".join(lines), max_chars=MAX_INTEL_CHARS)


# --------------------------------------------------------------------------------------
# Archive Q&A
# --------------------------------------------------------------------------------------


def extract_keywords(question: str, *, limit: int = 6) -> list[str]:
    words = re.findall(r"[a-zA-Z0-9_'-]{3,}", (question or "").lower())
    keywords: list[str] = []
    for word in words:
        if word in QUESTION_STOPWORDS or word in keywords:
            continue
        keywords.append(word)
        if len(keywords) >= limit:
            break
    return keywords


def load_display_names(db_path: Path) -> dict[int, str]:
    """user_id → known display name from the bot's users table (used for readable digests)."""
    db = sqlite3.connect(db_path)
    try:
        rows = _rows(db, "SELECT user_id, display_name FROM users")
    finally:
        db.close()
    names: dict[int, str] = {}
    for row in rows:
        try:
            user_id = int(row.get("user_id") or 0)
        except (TypeError, ValueError):
            continue
        name = str(row.get("display_name") or "").strip()
        if user_id and name:
            names[user_id] = name
    return names


def search_archive(
    db_path: Path,
    *,
    question: str,
    limit: int = MAX_ARCHIVE_HITS,
    include_private: bool = True,
    candidate_multiplier: int = 4,
) -> list[dict[str, Any]]:
    """Keyword search over the message archive, best match first.

    Any keyword may match (an AND of every keyword finds almost nothing on real questions); messages
    are then scored by how many distinct keywords they contain, with newer messages winning ties.
    Multi-keyword questions require more than one hit, so a single incidental word cannot carry the
    answer (the old behaviour surfaced unrelated chatter for "what did we decide about …").
    """
    keywords = extract_keywords(question)
    if not keywords:
        return []
    min_score = 2 if len(keywords) >= 3 else 1
    where = " OR ".join("content LIKE ?" for _ in keywords)
    params: list[Any] = [f"%{word}%" for word in keywords]
    visibility = "" if include_private else " AND visibility = 'public'"
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        rows = _rows(
            db,
            f"SELECT message_id, channel_id, author_id, author_name, content, created_at, visibility "
            f"FROM message_archive WHERE ({where}){visibility} ORDER BY created_at DESC LIMIT ?",
            (*params, max(1, limit) * max(1, candidate_multiplier)),
        )
    finally:
        db.close()
    for row in rows:
        haystack = str(row.get("content") or "").lower()
        row["score"] = sum(1 for word in keywords if word in haystack)
    rows = [row for row in rows if int(row.get("score") or 0) >= min_score] or [
        row for row in rows if int(row.get("score") or 0) >= 1
    ]
    rows.sort(key=lambda row: (-int(row.get("score") or 0), _negative_time_key(str(row.get("created_at") or ""))))
    return rows[: max(1, limit)]


def _negative_time_key(value: str) -> str:
    """Sort helper: newest first inside equal scores (ISO strings compare lexicographically)."""
    return "".join(chr(255 - ord(ch)) for ch in value)


def build_archive_prompt(
    *,
    question: str,
    hits: list[dict[str, Any]],
    channel_names: dict[int, str],
    guild_id: int | None,
    max_chars: int = 1600,
) -> str:
    citations = "\n".join(
        f"- {str(row.get('created_at'))[:10]} {resolve_channel(row.get('channel_id'), channel_names)} "
        f"{row.get('author_name')}: {_quote(str(row.get('content') or ''), 220)} "
        f"({jump_link(guild_id, row.get('channel_id'), row.get('message_id'))})"
        for row in hits
    ) or "- (nothing matched in the archive)"
    return f"""Answer Hoops' question about what this server decided or discussed, using ONLY the archived messages below.

Question: {question}

Archived messages (real, newest first):
{citations}

Rules:
- Answer in 2-6 sentences: what was decided/discussed, who said it, when.
- Cite what you use as "YYYY-MM-DD #channel — author" and include the jump link for the key message.
- If the archive does not show a decision, say so plainly and say what it does show instead
  (e.g. only discussion, or only an older discussion). Never invent a decision, date or person.
- No pings or mentions. Under {max_chars} characters."""


def archive_fallback(
    *,
    question: str,
    hits: list[dict[str, Any]],
    channel_names: dict[int, str],
    guild_id: int | None,
) -> str:
    if not hits:
        return sanitize_post(
            f"No archived messages match “{_quote(question, 80)}”. Nothing was decided in the archive yet — "
            "or it was discussed somewhere the bot does not archive.",
            max_chars=MAX_INTEL_CHARS,
        )
    lines = [f"**Archive matches for “{_quote(question, 80)}”** (newest first)"]
    for row in hits[:12]:
        link = jump_link(guild_id, row.get("channel_id"), row.get("message_id"))
        lines.append(
            f"- {str(row.get('created_at'))[:10]} {resolve_channel(row.get('channel_id'), channel_names)} "
            f"**{row.get('author_name')}**: {_quote(str(row.get('content') or ''), 160)}"
            + (f" ([jump]({link}))" if link else "")
        )
    return sanitize_post("\n".join(lines), max_chars=MAX_INTEL_CHARS)


def summarize_topics(questions: Iterable[str], *, limit: int = 8) -> list[tuple[str, int]]:
    """Cheap keyword grouping of real requests (no model) for the digest numbers line."""
    counts: dict[str, int] = {}
    for question in questions:
        for keyword in extract_keywords(question, limit=4):
            counts[keyword] = counts.get(keyword, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ranked[:limit]
