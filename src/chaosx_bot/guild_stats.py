"""Authoritative server facts taken from Discord itself.

The bot's own tables answer a different question: its `users` table only holds members it can currently
see (the bot deliberately does not request the privileged GUILD_MEMBERS intent, so a full member list is
403 and the table holds a partial set — 36 rows while the server had 69 members). Any number the bot
reports as "members" must therefore come from Discord, and when Discord cannot be reached the count is
reported as unavailable rather than guessed from local tables.

`with_counts=true` gives Discord's own `approximate_member_count` / `approximate_presence_count` — the
numbers members see in the member list. Exact enumeration needs the privileged GUILD_MEMBERS intent,
which this bot deliberately does not request (it gets 403 Missing Access).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

DISCORD_API = "https://discord.com/api/v10"
DEFAULT_TTL_SECONDS = 600
UNKNOWN = "unavailable"


@dataclass(frozen=True)
class GuildCounts:
    """Server size as Discord reports it."""

    guild_id: int
    members: int | None
    online: int | None
    source: str
    fetched_at: str

    @property
    def available(self) -> bool:
        return self.members is not None

    def as_signals(self) -> dict[str, Any]:
        return {
            "members": self.members,
            "online": self.online,
            "members_source": self.source,
        }


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def unavailable(guild_id: int) -> GuildCounts:
    return GuildCounts(guild_id=guild_id, members=None, online=None, source=UNKNOWN, fetched_at=_utcnow_iso())


def parse_guild_counts(payload: dict[str, Any] | None, *, guild_id: int) -> GuildCounts:
    """Pure parse of a `GET /guilds/{id}?with_counts=true` response.

    `0` is not a member count — Discord omits the fields when counts are unavailable, and a server with
    zero members cannot contain the bot — so anything unusable yields an unavailable result instead.
    """
    if not isinstance(payload, dict):
        return unavailable(guild_id)
    members = payload.get("approximate_member_count")
    online = payload.get("approximate_presence_count")
    parsed_members = int(members) if isinstance(members, int) and members > 0 else None
    parsed_online = int(online) if isinstance(online, int) and online >= 0 else None
    return GuildCounts(
        guild_id=guild_id,
        members=parsed_members,
        online=parsed_online,
        source="discord" if parsed_members is not None else UNKNOWN,
        fetched_at=_utcnow_iso(),
    )


def auth_header(token: str) -> str:
    """Discord wants `Bot <token>`; `Bearer <token>` is answered with 401 for bot tokens."""
    raw = (token or "").strip()
    return raw if raw.lower().startswith("bot ") else f"Bot {raw}"


async def fetch_guild_counts(
    guild_id: int, *, token: str, client: httpx.AsyncClient | None = None
) -> GuildCounts:
    """Discord's member/presence counts for one guild. Never raises; unknown on any failure."""
    if not guild_id or not token:
        return unavailable(guild_id)
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=20)
    try:
        response = await http.get(
            f"{DISCORD_API}/guilds/{guild_id}",
            params={"with_counts": "true"},
            headers={"Authorization": auth_header(token)},
        )
        if response.status_code != 200:
            return unavailable(guild_id)
        return parse_guild_counts(response.json(), guild_id=guild_id)
    except (httpx.HTTPError, ValueError):
        return unavailable(guild_id)
    finally:
        if owns_client:
            await http.aclose()


class GuildCountsCache:
    """Small TTL cache so repeated prompts do not re-ask Discord."""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self.ttl = timedelta(seconds=max(0, ttl_seconds))
        self._cached: GuildCounts | None = None

    async def get(
        self, guild_id: int, *, token: str, now: datetime | None = None, force: bool = False
    ) -> GuildCounts:
        moment = now or datetime.now(timezone.utc)
        cached = self._cached
        if cached is not None and not force and cached.guild_id == guild_id:
            try:
                age = moment - datetime.fromisoformat(cached.fetched_at)
            except ValueError:
                age = self.ttl + timedelta(seconds=1)
            if age <= self.ttl:
                return cached
        fresh = await fetch_guild_counts(guild_id, token=token)
        self._cached = fresh
        return fresh
