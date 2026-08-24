"""Server channel layout as reference context for ChaosX answers.

Public asks often need to point users at the right channel (report bugs,
post suggestions, find playtest info). This module fetches the guild's
channel list over the Discord REST API (with the required User-Agent),
caches it with a TTL, and renders a compact reference the model can cite:
category + channel name + topic. Only channels the bot can view are
returned by the API, so restricted channels never appear.
"""

from __future__ import annotations

import time
from typing import Any

import aiohttp

from .server_rules import DISCORD_API_BASE, DISCORD_BOT_UA

CHANNELS_REFRESH_TTL_S = 6 * 3600
CHANNEL_REFERENCE_MAX_CHARS = 2500
TOPIC_MAX_CHARS = 120
SKIP_CHANNEL_TYPES = {2, 3, 4, 13, 14, 15, 16}  # voice, group dm, category, stage, forum, media, private

TEXT_TYPE_NAMES = {
    0: "text",
    5: "announcement",
    10: "news thread",
    11: "public thread",
    12: "private thread",
}


def format_channel_reference(channels: list[dict[str, Any]]) -> str:
    """Render a compact, category-grouped channel reference for prompts.

    Each channel is rendered as a real Discord mention (`<#channel_id>`),
    which the model can copy verbatim into answers so channel citations are
    clickable links, not plain text.
    """
    if not channels:
        return ""
    categories: dict[Any, str] = {}
    for channel in channels:
        if channel.get("type") == 4:  # category
            categories[channel["id"]] = channel.get("name") or ""

    lines: list[str] = []
    seen: set[Any] = set()
    # Announcement/text channels first (ordered by position), then threads.
    ordered = sorted(
        (c for c in channels if c.get("type") in (0, 5)),
        key=lambda c: (c.get("position") or 0, c.get("name") or ""),
    )
    for channel in ordered:
        cid = channel["id"]
        if cid in seen:
            continue
        seen.add(cid)
        name = (channel.get("name") or "").strip()
        if not name:
            continue
        topic = (channel.get("topic") or "").strip().replace("\n", " ")
        if topic:
            topic = topic[:TOPIC_MAX_CHARS]
        category = categories.get(channel.get("parent_id") or "")
        label = f"- <#{cid}>" + (f" — {topic}" if topic else "")
        if category:
            label = f"  {label}"
        lines.append(label)
    reference = "\n".join(lines)
    return reference[:CHANNEL_REFERENCE_MAX_CHARS]


class GuildChannels:
    """Cached fetcher for the server's channel layout."""

    def __init__(self, *, bot_token: str, guild_id: int, http_timeout_s: float = 15.0) -> None:
        self._token = bot_token
        self._guild_id = guild_id
        self._timeout = aiohttp.ClientTimeout(total=http_timeout_s)
        self._channels: list[dict[str, Any]] = []
        self._fetched_at = 0.0
        self._last_error = ""

    async def _fetch_from_api(self) -> list[dict[str, Any]]:
        url = f"{DISCORD_API_BASE}/guilds/{self._guild_id}/channels"
        headers = {"Authorization": f"Bot {self._token}", "User-Agent": DISCORD_BOT_UA}
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    raise RuntimeError(f"channels fetch HTTP {response.status}")
                data: Any = await response.json()
        return [c for c in data or [] if c.get("type") not in SKIP_CHANNEL_TYPES]

    async def refresh(self) -> bool:
        """Fetch the channel list; keep the previous copy on failure."""
        try:
            channels = await self._fetch_from_api()
            if channels:
                self._channels = channels
                self._fetched_at = time.monotonic()
                self._last_error = ""
                return True
        except Exception as exc:  # noqa: BLE001 - reference is best-effort
            self._last_error = repr(exc)
        return False

    def needs_refresh(self) -> bool:
        return bool(self._guild_id) and (
            not self._channels or time.monotonic() - self._fetched_at > CHANNELS_REFRESH_TTL_S
        )

    def channels_block(self, header: str = "Server channels (for reference)") -> str:
        """Prompt-ready channel reference, or empty when not fetched yet."""
        text = format_channel_reference(self._channels)
        if not text:
            return ""
        return f"{header}:\n{text}"
