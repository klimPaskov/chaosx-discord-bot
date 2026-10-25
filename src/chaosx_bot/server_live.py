"""Live server data for the owner's requests: exact ids, every channel, real recent messages.

Hoops, 2026-10-25: "it must be able to do literally anything and with very high precision. It should
autonomously read and gather data when asked, and always know exact ids and know all channels and retrieve
all messages whenever asked."

The owner answer path only had a static prompt, so it replied "I cannot read the server" while 5,900 messages
sat in the archive and every channel was visible to the bot. This module assembles the current picture per
request — channel directory with ids, roles, member ids, and a live history scan of the channels the request
names. Read-only.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

import discord

from .server_intel import extract_keywords, search_archive

MAX_CHANNELS_TO_SCAN = 3
MESSAGES_PER_CHANNEL = 400
MAX_BLOCK_CHARS = 9000
HISTORY_INTENT_RE = re.compile(
    r"(?i)\b(raid\w*|blame[ds]?|rule[- ]?break\w*|non[- ]?civil|toxic\w*|harass\w*|warn\w*|ban(?:ned|ning)?|"
    r"timeout\w*|kick\w*|moderation|moderator|evidence|who said|who was|who were|who is|spam\w*|drama|"
    r"what did we (?:decide|say)|last (?:week|month|time))\b"
)
MEMBER_INTENT_RE = re.compile(r"(?i)\b(who is|who's|user|member|account|id of|ids?|mention)\b")
_NOISE = {"the", "and", "for", "with", "that", "this", "channel", "channels", "chat", "text", "conversation", "general"}


# Words that make a scan hit everything and reveal nothing. Chosen from a real miss: the first live run
# matched "trying, pressure, anything, think, bad, idea" across 400 messages and named nobody.
_SCAN_NOISE = {
    "trying", "pressure", "anything", "think", "thought", "bad", "idea", "ideas", "thing", "things", "stuff",
    "would", "could", "should", "likely", "most", "some", "take", "down", "delete", "messages", "message",
    "channel", "channels", "users", "user", "warn", "permanently", "ban", "tell", "what", "were", "was",
    "because", "before", "after", "first", "then", "that", "this", "with", "from", "have", "just", "like",
    "want", "wants", "going", "make", "made", "does", "did", "really", "very", "much", "more", "their",
}


def history_keywords(request: str, *, limit: int = 6) -> list[str]:
    """The incident words to read history for, as search stems.

    "blamed for the raid and acted non civil, broke rules" has to search for blame / raid / civil / rule, not
    for "trying", "anything" or "steam" — the first live run did the latter and named nobody.
    """
    stems: list[str] = []
    for match in HISTORY_INTENT_RE.finditer(request or ""):
        word = match.group(0).lower()
        for suffix in ("ning", "ing", "ied", "ed", "es", "s"):
            if len(word) > 4 and word.endswith(suffix):
                word = word[: -len(suffix)]
                break
        if word and word not in stems:
            stems.append(word)
    for match in re.findall(r"[a-zA-Z][a-zA-Z'-]{4,}", request or ""):
        lowered = match.lower()
        if lowered in _SCAN_NOISE or any(stem in lowered for stem in stems):
            continue
        if lowered.endswith("ly"):
            continue
        stems.append(lowered)
    return stems[:limit]


def looks_like_history_request(text: str) -> bool:
    return bool(HISTORY_INTENT_RE.search(text or ""))


def _tokens(text: str) -> set[str]:
    words = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split()
    return {word for word in words if len(word) > 2 and word not in _NOISE}


def channel_directory(guild: discord.Guild) -> str:
    lines: list[str] = []
    by_category: dict[str, list[Any]] = {}
    for channel in guild.channels:
        # Duck-typed on purpose: a category has no history and must not be listed as a channel.
        if not hasattr(channel, "name") or isinstance(channel, discord.CategoryChannel):
            continue
        category_attr = getattr(channel, "category", None)
        category = getattr(category_attr, "name", None) or "no category"
        by_category.setdefault(category, []).append(channel)
    for category, channels in by_category.items():
        listing = ", ".join(f"#{channel.name} id={channel.id}" for channel in channels[:40])
        lines.append(f"{category}: {listing}")
    return "\n".join(lines)[:4000]


def role_directory(guild: discord.Guild) -> str:
    roles = sorted((role for role in guild.roles if not role.is_default()), key=lambda role: role.position, reverse=True)
    return "\n".join(
        f"@{role.name} id={role.id} position={role.position} members={len(role.members)}" for role in roles[:40]
    )


def guild_facts(guild: discord.Guild) -> str:
    me = guild.me
    perms = me.guild_permissions if me else None
    labels = {
        "administrator": "administrator",
        "manage_channels": "manage channels",
        "manage_roles": "manage roles",
        "manage_messages": "manage messages",
        "moderate_members": "timeout members",
        "ban_members": "ban members",
        "kick_members": "kick members",
        "read_message_history": "read history",
    }
    have = [label for attr, label in labels.items() if perms is not None and getattr(perms, attr, False)]
    return (
        f"Server: {guild.name} id={guild.id}; members={guild.member_count}; owner id={guild.owner_id}; "
        f"my top role={me.top_role.name if me and me.top_role else 'unknown'}; "
        f"my permissions include: {', '.join(have) or 'none'}"
    )


async def resolve_members(guild: discord.Guild, request: str) -> str:
    found: dict[int, discord.Member] = {}
    for member in guild.members:
        name = (member.name or "").lower()
        display = (member.display_name or "").lower()
        if name and re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", request.lower()) or (
            display and re.search(rf"(?<![a-z0-9]){re.escape(display)}(?![a-z0-9])", request.lower())
        ):
            found[member.id] = member
        if len(found) >= 8:
            break
    if not found:
        try:
            for member in await guild.query_members(query=request[:100], limit=8):
                found[member.id] = member
        except (discord.HTTPException, AttributeError, TypeError):
            pass
    if not found:
        return ""
    return "Members matched by name: " + ", ".join(
        f"{member.name} (display {member.display_name!r}, id {member.id})" for member in list(found.values())[:8]
    )


async def scan_channels(
    guild: discord.Guild, channels: list[Any], keywords: list[str], *, per_channel: int = MESSAGES_PER_CHANNEL
) -> str:
    """Read recent history and report who said what — counts, ids and quotes, never inference."""
    needles = [word for word in keywords if len(word) > 2]
    if not needles:
        return ""
    blocks: list[str] = []
    for channel in [item for item in channels if hasattr(item, "history")][:MAX_CHANNELS_TO_SCAN]:
        per_author: Counter[tuple[str, int]] = Counter()
        samples: dict[tuple[str, int], list[str]] = {}
        scanned = 0
        hits = 0
        oldest = newest = None
        try:
            async for message in channel.history(limit=per_channel):
                scanned += 1
                oldest = message.created_at
                newest = newest or message.created_at
                body = " ".join((message.content or "").split())
                if not body:
                    continue
                lowered = body.lower()
                if not any(needle in lowered for needle in needles):
                    continue
                hits += 1
                key = (message.author.display_name or message.author.name, message.author.id)
                per_author[key] += 1
                if len(samples.setdefault(key, [])) < 2:
                    samples[key].append(body[:200])
        except (discord.Forbidden, discord.HTTPException) as exc:
            blocks.append(f"#{channel.name} id={channel.id}: unreadable ({type(exc).__name__}).")
            continue
        window = f"{oldest:%Y-%m-%d %H:%M} → {newest:%Y-%m-%d %H:%M}" if oldest and newest else "no timestamps"
        if not hits:
            blocks.append(
                f"#{channel.name} id={channel.id}: read {scanned} message(s) ({window}); none mention "
                f"{', '.join(needles)}."
            )
            continue
        lines = [
            f"#{channel.name} id={channel.id}: {hits} of {scanned} recent message(s) mention "
            f"{', '.join(needles)} ({window})."
        ]
        for (name, user_id), count in per_author.most_common(10):
            quotes = " / ".join(f"“{sample}”" for sample in samples.get((name, user_id), []))
            lines.append(f"  - {name} id={user_id}: {count} message(s). {quotes}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def archive_block(db_path, request: str, *, limit: int = 10) -> str:
    """Older evidence the live scan cannot reach, with the exact ids and dates."""
    try:
        rows = search_archive(db_path, question=request, limit=limit)
    except Exception:
        return ""
    if not rows:
        return ""
    lines = []
    for row in rows:
        if str(row.get("author_name") or "") == "ChaosX":
            continue  # the bot's own answers are not evidence about members
        body = " ".join(str(row.get("content") or "").split())[:200]
        lines.append(
            f"- {row.get('created_at', '')[:16]} in channel {row.get('channel_id')} by {row.get('author_name')} "
            f"(id {row.get('author_id')}): “{body}”"
        )
    return "Archive matches:\n" + "\n".join(lines)


async def gather(bot, guild: discord.Guild | None, request: str) -> str:
    """The live + archived evidence block for an owner request. Never raises."""
    if guild is None:
        return ""
    sections = ["LIVE SERVER DATA (read just now — use these exact ids; never claim you cannot read the server):"]
    try:
        sections.append(guild_facts(guild))
    except Exception as exc:  # pragma: no cover - defensive
        sections.append(f"server facts unavailable: {type(exc).__name__}")
    try:
        sections.append("Channels:\n" + channel_directory(guild))
    except Exception as exc:  # pragma: no cover - defensive
        sections.append(f"channel directory unavailable: {type(exc).__name__}")
    if re.search(r"(?i)\b(role|roles|permission|permissions)\b", request or ""):
        try:
            sections.append("Roles:\n" + role_directory(guild))
        except Exception as exc:  # pragma: no cover - defensive
            sections.append(f"role directory unavailable: {type(exc).__name__}")
    if MEMBER_INTENT_RE.search(request or "") or looks_like_history_request(request):
        try:
            members = await resolve_members(guild, request)
            if members:
                sections.append(members)
        except Exception as exc:  # pragma: no cover - defensive
            sections.append(f"member lookup unavailable: {type(exc).__name__}")

    history = looks_like_history_request(request)
    wanted = [channel for channel in guild.text_channels if _tokens(channel.name) & _tokens(request)]
    if history and not wanted and bot is not None:
        # Nothing named: scan where the bot actually sees the most traffic, which is where an incident lives.
        try:
            counts = getattr(bot, "_channel_activity", None)
            if counts:
                wanted = sorted(counts, key=counts.get, reverse=True)[:MAX_CHANNELS_TO_SCAN]
        except Exception:
            wanted = []
    if wanted and (history or len(_tokens(request)) >= 2):
        keywords = history_keywords(request) if history else sorted(_tokens(request))[:6]
        try:
            scan = await scan_channels(guild, wanted, keywords)
            if scan:
                sections.append("Recent messages matching the request:\n" + scan)
        except Exception as exc:  # pragma: no cover - defensive
            sections.append(f"message scan unavailable: {type(exc).__name__}")
    if history:
        try:
            archived = archive_block(getattr(getattr(bot, "settings", None), "db_path", None), request)
            if archived:
                sections.append(archived)
        except Exception:  # pragma: no cover - defensive
            pass
    return "\n\n".join(sections)[:MAX_BLOCK_CHARS]
