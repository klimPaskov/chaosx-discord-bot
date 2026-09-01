"""Server member directory as reference context for ChaosX answers.

Public asks sometimes ask about who is in the server or reference a user by
name. This module fetches the guild's member list over the Discord REST API
(paginated, with the required User-Agent), caches it with a TTL, and renders
a compact display-name directory the model can cite WITHOUT pinging anyone.
Bots are excluded. Only members the bot can see are returned by the API.
"""

from __future__ import annotations

import time
from typing import Any

import aiohttp

from .server_rules import DISCORD_API_BASE, DISCORD_BOT_UA

MEMBERS_REFRESH_TTL_S = 6 * 3600
MEMBERS_PAGE_LIMIT = 1000
MEMBERS_MAX_TOTAL = 2000
MEMBER_DIRECTORY_MAX_CHARS = 4000
MEMBER_DIRECTORY_LIMIT = 120


def user_reference_name(member: dict[str, Any]) -> str:
    """Actual global username (unique), with legacy discriminator when present.

    NEVER the display name: nicknames/global names can collide or be
    impersonated — usernames are the only stable reference names.
    """
    user = member.get("user") or {}
    username = str(user.get("username") or "").strip()
    if not username:
        return ""
    discriminator = str(user.get("discriminator") or "0")
    if discriminator not in ("0", "0000"):
        return f"{username}#{discriminator}"
    return username


def author_reference_name(author: Any) -> str:
    """Actual username for discord.py user/member objects (unique reference)."""
    name = getattr(author, "name", None) or ""
    if not name:
        return ""
    discriminator = str(getattr(author, "discriminator", None) or "0")
    if discriminator not in ("0", "0000"):
        return f"{name}#{discriminator}"
    return name


def _display_name(member: dict[str, Any]) -> str:
    """Server-facing display name: nickname, else global name, else username."""
    return str(
        (member.get("nick") or "").strip()
        or (member.get("user") or {}).get("global_name")
        or (member.get("user") or {}).get("username")
        or ""
    ).strip()


def colliding_display_ids(
    entries: list[tuple[int, str]], *, owner_id: int | None = None
) -> set[int]:
    """IDs where the display name is shared by >=2 members, or where a
    non-owner member uses the owner's display name (confirmed impersonation).
    Those members must be referenced by their actual username instead."""
    by_display: dict[str, list[int]] = {}
    for uid, display in entries:
        if display:
            by_display.setdefault(display, []).append(uid)
    collided: set[int] = set()
    for display, ids in by_display.items():
        if len(ids) > 1:
            collided.update(ids)
    if owner_id is not None:
        owner_display = next((d for uid, d in entries if uid == owner_id and d), None)
        if owner_display:
            collided.update(uid for uid, d in entries if d == owner_display and uid != owner_id)
    return collided


def format_member_directory(
    members: list[dict[str, Any]], *, owner_id: int | None = None
) -> str:
    """Render a compact display-name directory for prompts.

    Each member is listed as ``- <display name> (<id>)`` so the model can
    reference users without pinging them (no mentions, no tags). Members
    whose display name collides with another member's (confirmed
    impersonation) are listed by their actual username instead. Sorted by
    displayed reference; capped to keep the block small.
    """
    entries: list[tuple[int, str, str]] = []
    seen: set[int] = set()
    for member in members:
        if (member.get("user") or {}).get("bot"):
            continue
        uid = member.get("id")
        if uid is None or int(uid) in seen:
            continue
        seen.add(int(uid))
        entries.append((int(uid), _display_name(member), user_reference_name(member)))
    collided = colliding_display_ids([(uid, d) for uid, d, _ in entries], owner_id=owner_id)
    names: list[str] = []
    for uid, display, username in entries:
        if not display:
            continue
        ref = username if uid in collided and username else display
        names.append(f"- {ref} ({uid})")
    if not names:
        return ""
    names.sort(key=str.casefold)
    return "\n".join(names[:MEMBER_DIRECTORY_LIMIT])


class GuildMembers:
    """Cached fetcher for the server's member directory."""

    def __init__(self, *, bot_token: str, guild_id: int, http_timeout_s: float = 20.0) -> None:
        self._token = bot_token
        self._guild_id = guild_id
        self._timeout = aiohttp.ClientTimeout(total=http_timeout_s)
        self._members: list[dict[str, Any]] = []
        self._fetched_at = 0.0
        self._last_error = ""

    async def _fetch_from_api(self) -> list[dict[str, Any]]:
        headers = {"Authorization": f"Bot {self._token}", "User-Agent": DISCORD_BOT_UA}
        members: list[dict[str, Any]] = []
        after = ""
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            while len(members) < MEMBERS_MAX_TOTAL:
                url = f"{DISCORD_API_BASE}/guilds/{self._guild_id}/members?limit={MEMBERS_PAGE_LIMIT}"
                if after:
                    url += f"&after={after}"
                async with session.get(url, headers=headers) as response:
                    if response.status != 200:
                        raise RuntimeError(f"members fetch HTTP {response.status}")
                    page: Any = await response.json()
                if not page:
                    break
                members.extend(page)
                if len(page) < MEMBERS_PAGE_LIMIT:
                    break
                after = str(page[-1].get("user", {}).get("id", ""))
                if not after:
                    break
        # Exclude bots (the bot itself and any other bots).
        return [m for m in members if not (m.get("user") or {}).get("bot")]

    async def refresh(self) -> bool:
        """Fetch the member list; keep the previous copy on failure."""
        try:
            members = await self._fetch_from_api()
            if members:
                self._members = members
                self._fetched_at = time.monotonic()
                self._last_error = ""
                return True
        except Exception as exc:  # noqa: BLE001 - reference is best-effort
            self._last_error = repr(exc)
        return False

    def needs_refresh(self) -> bool:
        return bool(self._guild_id) and (
            not self._members or time.monotonic() - self._fetched_at > MEMBERS_REFRESH_TTL_S
        )

    def members_block(self, header: str = "Server member directory (display names; when several members share a display name, the actual username is shown instead — never ping/mention them)") -> str:
        """Prompt-ready member directory, or empty when not fetched yet."""
        text = format_member_directory(self._members)
        if not text:
            return ""
        return f"{header}:\n{text}"
