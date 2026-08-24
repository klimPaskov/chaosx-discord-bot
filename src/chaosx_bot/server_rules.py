"""Live server-rules access for ChaosX (from the #rules channel).

The rules channel's announcement predates the bot, so the gateway capture
never logged it. This module fetches the current rules over the Discord REST
API (with the required Discord User-Agent header), caches them with a TTL,
and serves them to public asks, auto-scan soft warnings, and the owner/admin
path so the bot actually knows and can enforce the server rules.
"""

from __future__ import annotations

import time
from typing import Any

import aiohttp

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_BOT_UA = "DiscordBot (https://github.com/klimPaskov/chaosx-discord-bot, 0.1.0)"
RULES_REFRESH_TTL_S = 6 * 3600
RULES_FETCH_LIMIT = 10


class ServerRules:
    """Cached fetcher for the pinned rules message(s) of a channel."""

    def __init__(self, *, bot_token: str, channel_id: int, http_timeout_s: float = 15.0) -> None:
        self._token = bot_token
        self._channel_id = channel_id
        self._timeout = aiohttp.ClientTimeout(total=http_timeout_s)
        self._text = ""
        self._fetched_at = 0.0
        self._last_error = ""

    async def _fetch_from_api(self) -> str:
        url = (
            f"{DISCORD_API_BASE}/channels/{self._channel_id}/messages"
            f"?limit={RULES_FETCH_LIMIT}"
        )
        headers = {"Authorization": f"Bot {self._token}", "User-Agent": DISCORD_BOT_UA}
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    raise RuntimeError(f"rules fetch HTTP {response.status}")
                data: Any = await response.json()
        parts: list[str] = []
        for message in reversed(data or []):  # oldest -> newest
            content = (message.get("content") or "").strip()
            if content:
                parts.append(content)
        return "\n\n".join(parts)

    async def refresh(self) -> bool:
        """Fetch fresh rules from the channel; keep the previous copy on failure."""
        try:
            text = await self._fetch_from_api()
            if text.strip():
                self._text = text
                self._fetched_at = time.monotonic()
                self._last_error = ""
                return True
        except Exception as exc:  # noqa: BLE001 - rules are best-effort
            self._last_error = repr(exc)
        return False

    async def text(self) -> str:
        """Return the current rules, refreshing when stale or never fetched."""
        if not self._text or time.monotonic() - self._fetched_at > RULES_REFRESH_TTL_S:
            await self.refresh()
        return self._text

    def text_sync(self) -> str:
        """Return the cached rules without any network I/O."""
        return self._text

    def needs_refresh(self) -> bool:
        """True when rules are missing or stale and should be refetched in the background."""
        return bool(self._channel_id) and (
            not self._text or time.monotonic() - self._fetched_at > RULES_REFRESH_TTL_S
        )

    def rules_block(self, header: str = "Server rules (from the #rules channel)") -> str:
        """Prompt-ready rules block, or empty when no rules were fetched yet."""
        text = self._text.strip()
        if not text:
            return ""
        return f"{header}:\n{text}"
