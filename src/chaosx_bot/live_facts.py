"""Live data for answers: server activity, GitHub state, playtests, moderation.

Answers used to come from the knowledge index and memory alone, so any question about *current* state — how many
members, what is open on GitHub, what is in testing, who is at a warning count — was answered from stale records
or refused outright with "I have no live pull here".

These collectors read the live sources on demand (Discord objects, the bot's own archive tables, the `gh` CLI,
the moderation store), with short timeouts and a small cache, and the result is injected into the ask prompt only
when the question actually asks about live state.

Privacy rule: moderation names are included only when the asker is the server owner. Everyone else gets counts.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any

CACHE_TTL_SECONDS = 120
GH_TIMEOUT_SECONDS = 12

# A request wants live data when it asks about current state rather than knowledge.
LIVE_LOOKUP_TERMS = (
    "how many members", "member count", "how many people", "server size", "population",
    "newest member", "new member", "joined recently", "recently joined", "last to join", "latest join",
    "who joined",
    "active", "activity", "most active", "busiest", "quietest", "last message", "when was the last",
    "this week", "last 7 days", "last week", "last 30 days", "today", "yesterday", "right now", "currently",
    "at the moment", "as of now",
    "open issue", "open issues", "issues open", "pull request", "merge request", "recent commit",
    "latest commit", "last commit", "commits", "latest release", "newest release", "current version",
    "which version", "what version",
    "playtest", "playtests", "play testing", "needs testing", "testing list", "in testing", "marked for testing",
    "warn", "warns", "warning", "warnings", "warned", "timeout", "timeouts", "moderation", "mod actions",
    "muted", "banned",
    "your status", "bot status", "are you online", "uptime", "how long have you been",
    # Broad "how are things" asks: a plain status question carries no keyword from the list above, so the model
    # answered without data and invented its own "I can't pull current numbers here" disclaimer — exactly the
    # denial of capability this bot must never produce. Cheap to over-trigger (the block is cached and small).
    "status", "state of", "how are things", "how are we doing", "how is it going", "how's it going",
    "going on", "what's new", "whats new", "any news", "update", "updates", "overview", "summary",
    "recap", "rundown", "situation",
    # Live-state adjectives: "is the repo quiet", "has it gone dead". Cheap to over-trigger.
    "quiet", "busy", "dead", "inactive", "alive", "online",
)

_cache: dict[str, dict[str, Any]] = {}


def _audience(bot: Any, requester_id: int) -> str:
    """Cache key for the viewer: the owner's block can contain member names, so it must never be shared."""
    owner_id = getattr(getattr(bot, "settings", None), "owner_id", 0) or 0
    if requester_id and owner_id and int(requester_id) == int(owner_id):
        return "owner"
    return "public"


_LIVE_SHAPE_RE = re.compile(
    r"\b("
    r"what'?s (happening|going on|new|the latest|the word|the story)|"
    r"how (are|is) (things|it|everything|everybody|everyone|we)|how'?s (things|it|everything|life)|"
    r"where (are we|do (we|things) stand|things stand)|"
    r"any(thing)? (new|update|updates|news|progress|movement|changes?)|"
    r"what'?s been happening|what has been happening|"
    r"are we (still|on track|there yet)|"
    r"how many|"
    r"latest|newest|currently|right now|at the moment|these days|as of (now|today)"
    r")\b"
)


def _normalized(request: str) -> str:
    return re.sub(r"\s+", " ", (request or "").strip().casefold())


def wants_live_facts(request: str) -> bool:
    """Live data is fetched when the ask is about the present — not on every answer.

    Hoops, twice, and both instructions hold at once:

    * "Everything should be always dynamic. All answers must be dynamic. It must have access to all live data."
    * "It should pull live data only when needed. This time is not wasted with each answer." — asked after a
      question about an unreadable video was answered with a full server status report.

    History of the two failure modes, because each fix caused the other:

    * The original gate was too narrow: a plain "what is the current status on things" matched nothing, arrived
      with no block, and the model invented "I still can't pull current numbers here" — a denial of capability.
    * Removing the gate fixed that and created the opposite problem: the block rode on every answer and got
      recited at the first opportunity.

    So: fetch whenever the ask could be about current state (missing one is worse than fetching one time too
    many), and do not fetch for questions that plainly are not — a knowledge question, a link, banter. A needless
    block is now harmless anyway, because RELEVANCE_RULE forbids reciting it unasked; that is what makes a
    generous gate safe again.
    """
    text = _normalized(request)
    if not text:
        return True  # nothing to judge on: prefer having the data over guessing
    if any(term in text for term in LIVE_LOOKUP_TERMS):
        return True
    return bool(_LIVE_SHAPE_RE.search(text))


# ---------------------------------------------------------------- discord


def _channel_label(bot: Any, channel_id: int) -> str:
    channel = None
    try:
        channel = bot.get_channel(channel_id)
    except Exception:
        channel = None
    name = getattr(channel, "name", "") if channel else ""
    return f"#{name}" if name else f"channel {channel_id}"


def _archive_counts(
    db_path: str, days: int, *, owner_id: int | None = None
) -> tuple[list[tuple[int, int]], list[tuple[int, str, int]]]:
    """(channel_id, messages) and (author_id, author_name, messages) over the trailing window.

    The owner is excluded from every count. He is the operator and the reader, never a subject of the bot's
    reporting: his own messages must not be ranked as "most active member", and they must not inflate the
    channel activity he is being shown. Activity here means the community's, not his.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    owner_clause = " AND author_id != ?" if owner_id else ""
    params: tuple = (since,) + ((int(owner_id),) if owner_id else ())
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        channels = conn.execute(
            "SELECT channel_id, COUNT(*) FROM message_archive WHERE created_at >= ?" + owner_clause +
            " GROUP BY channel_id ORDER BY COUNT(*) DESC LIMIT 6",
            params,
        ).fetchall()
        members = conn.execute(
            "SELECT author_id, MAX(author_name), COUNT(*) FROM message_archive WHERE created_at >= ?" + owner_clause +
            " GROUP BY author_id ORDER BY COUNT(*) DESC LIMIT 5",
            params,
        ).fetchall()
    finally:
        conn.close()
    return channels, members


def _guild_member_facts(bot: Any) -> tuple[int, list[tuple[str, str]]]:
    """(directory size, newest joins as (name, iso date))."""
    directory = getattr(getattr(bot, "guild_members", None), "_members", None) or []
    size = len(directory)
    joins: list[tuple[str, str]] = []
    for entry in directory:
        joined = entry.get("joined_at") or ""
        if not joined:
            continue
        user = entry.get("user") or {}
        name = entry.get("nick") or user.get("global_name") or user.get("username") or "someone"
        joins.append((str(name), str(joined)))
    joins.sort(key=lambda pair: pair[1], reverse=True)
    return size, joins[:3]


async def _discord_section(bot: Any) -> list[str]:
    lines: list[str] = []
    directory_size, newest = await asyncio.to_thread(_guild_member_facts, bot)
    guild = None
    try:
        guild = bot.get_guild(bot.settings.allowed_guild_id)
    except Exception:
        guild = None
    count = directory_size or getattr(guild, "member_count", 0) or 0
    if count:
        lines.append(f"- Members: {count}")
    if newest:
        joined = "; ".join(f"{name} ({date[:10]})" for name, date in newest)
        lines.append(f"- Newest joins: {joined}")
    for window in (7, 30):
        try:
            channels, members = await asyncio.to_thread(
                _archive_counts,
                bot.settings.db_path,
                window,
                owner_id=getattr(bot.settings, "owner_id", 0) or None,
            )
        except Exception:
            channels, members = [], []
        if channels:
            busiest = ", ".join(
                f"{bot.get_channel(cid).name if bot.get_channel(cid) else cid} {n}" for cid, n in channels[:4]
            )
            lines.append(f"- Busiest channels ({window}d): {busiest}")
        if members and window == 7:
            top = ", ".join(f"{name or uid} {n}" for uid, name, n in members[:4])
            lines.append(f"- Most active members (7d): {top}")
    return lines


# ---------------------------------------------------------------- github


async def _gh(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "gh", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), timeout=GH_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        return ""
    if proc.returncode != 0:
        return ""
    return out.decode("utf-8", "replace").strip()


async def _github_section(repo: str) -> list[str]:
    if not repo:
        return []
    lines: list[str] = []
    issues = await _gh(
        "issue", "list", "--repo", repo, "--state", "open", "--limit", "30",
        "--json", "number,title,createdAt",
    )
    if issues:
        try:
            rows = json.loads(issues)
        except Exception:
            rows = []
        if rows:
            lines.append(f"- Open issues: {len(rows)}")
            for row in rows[:4]:
                lines.append(f"  - #{row.get('number')} {str(row.get('title'))[:70]}")
        else:
            lines.append("- Open issues: none")
    commits_raw = await _gh("api", f"repos/{repo}/commits?per_page=5")
    if commits_raw:
        try:
            rows = json.loads(commits_raw)
        except Exception:
            rows = []
        entries = []
        for row in rows[:4]:
            sha = str(row.get("sha", ""))[:7]
            message = str((row.get("commit") or {}).get("message", "")).splitlines()[:1]
            date = str(((row.get("commit") or {}).get("author") or {}).get("date", ""))[:10]
            entries.append(f"{sha} {message[0][:60] if message else ''} ({date})")
        if entries:
            lines.append("- Recent commits:")
            for row in entries:
                lines.append(f"  - {row}")
    release = await _gh("release", "list", "--repo", repo, "--limit", "1")
    if release:
        lines.append(f"- Latest release: {release.splitlines()[0][:90]}")
    return lines


# ---------------------------------------------------------------- playtests / moderation


def _playtest_rows(db_path: str) -> tuple[list[tuple], list[tuple]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        scheduled = conn.execute(
            "SELECT playtest_id, target, start_time, status FROM playtest_records "
            "ORDER BY start_time DESC LIMIT 5"
        ).fetchall()
        nominations = conn.execute(
            "SELECT label, created_at FROM testing_nominations WHERE active = 1 ORDER BY created_at DESC LIMIT 8"
        ).fetchall()
    finally:
        conn.close()
    return scheduled, nominations


async def _playtest_section(db_path: str) -> list[str]:
    try:
        scheduled, nominations = await asyncio.to_thread(_playtest_rows, db_path)
    except Exception:
        return []
    lines: list[str] = []
    if scheduled:
        lines.append("- Scheduled playtests:")
        for pid, target, start, status in scheduled[:4]:
            lines.append(f"  - {target or pid} at {start} ({status})")
    else:
        lines.append("- Scheduled playtests: none on record")
    if nominations:
        labels = ", ".join(str(label) for label, _created in nominations)
        lines.append(f"- Marked as needs testing: {labels}")
    else:
        lines.append("- Marked as needs testing: nothing on the list")
    return lines


async def _moderation_section(bot: Any, requester_id: int) -> list[str]:
    try:
        rows = await bot.store.list_warned_users(guild_id=bot.settings.allowed_guild_id, limit=25)
    except Exception:
        return []
    if not rows:
        return ["- Active warnings: none on record"]
    is_owner = bool(requester_id) and int(requester_id) == int(bot.settings.owner_id)
    lines = [f"- Members at a warning count: {len(rows)}"]
    for row in rows[:5]:
        actor_id, count, last_at, reason = (list(row) + [None] * 4)[:4]
        name = ""
        try:
            member = bot.get_guild(bot.settings.allowed_guild_id).get_member(int(actor_id))
            name = getattr(member, "display_name", "") or ""
        except Exception:
            name = ""
        who = name or (str(actor_id) if is_owner else "a member")
        detail = f"  - {who}: {count} warning(s), last {str(last_at)[:10]}"
        # Reasons are identifying, so they stay out of public answers entirely.
        if reason and is_owner:
            detail += f" — {str(reason)[:70]}"
        lines.append(detail)
    if not is_owner:
        lines.append("  (names withheld in public answers; the owner or /admin can list them)")
    return lines


_CAPABILITY_SUMMARY = ""


def set_capability_summary(text: str) -> None:
    """Record what the bot can actually do (probed at startup), for the prompt to describe truthfully."""
    global _CAPABILITY_SUMMARY
    _CAPABILITY_SUMMARY = str(text or "")


def capability_summary() -> str:
    return _CAPABILITY_SUMMARY


# ---------------------------------------------------------------- entry point


async def collect(bot: Any, *, requester_id: int = 0, force: bool = False) -> str:
    """Gather the live block, cached per audience. Returns "" when nothing could be read."""
    audience = _audience(bot, requester_id)
    entry = _cache.setdefault(audience, {"at": 0.0, "block": ""})
    now = time.time()
    if not force and entry["block"] and now - float(entry["at"]) < CACHE_TTL_SECONDS:
        return str(entry["block"])

    sections: list[tuple[str, list[str]]] = []
    sections.append(("Server activity", await _discord_section(bot)))
    sections.append(("Project (GitHub)", await _github_section(str(getattr(bot.settings, "github_repo", "") or ""))))
    sections.append(("Playtests", await _playtest_section(bot.settings.db_path)))
    sections.append(("Moderation", await _moderation_section(bot, requester_id)))

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body: list[str] = [
        f"LIVE DATA (read from the live sources at {stamp}; these are current values, not records — "
        "state them plainly as the present situation). You do have live access: never say you cannot pull "
        "current numbers, never say you have no live figures here, and never present these values as "
        "last-known record rather than current status. Use these values directly:",
        # Availability is not an invitation. This block rides on every ask so the answer can never deny current
        # values — but it is background, and reciting it unasked is its own failure (a question about an
        # unreadable video once produced a full status report plus unrelated commentary).
        "- This block is BACKGROUND, not a report to deliver. Use only the parts that bear on the question. "
        "Never recite it as a status update unless the member actually asked about status, activity, or "
        "updates — and never add commentary, opinions, warnings or advice that were not asked for.",
    ]
    empty: list[str] = []
    for title, lines in sections:
        if lines:
            body.append(f"{title}:")
            body.extend(lines)
        else:
            empty.append(title)
    body.append(
        "- If a section is missing above, say you cannot read that source right now — never guess numbers, "
        "and never claim you have no live access to the sections that are present."
    )
    if _CAPABILITY_SUMMARY:
        body.append(f"- Capabilities: {_CAPABILITY_SUMMARY}")
    if empty:
        body.append(f"- Unavailable right now: {', '.join(empty)}.")
    block = "\n".join(body)
    _cache[audience] = {"at": now, "block": block}
    return block
