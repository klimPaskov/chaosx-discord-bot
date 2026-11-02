"""Registry for the guild's custom emoji, resolved once and used by name.

The Chaos Redux server carries a full custom set (chaos tiers, ChaosX marks, the factory
mascot, pepe, cats). Messages should use those instead of unicode look-alikes, so the bot
installs the guild's emoji at startup and every call site asks for one by name:

    from .emoji import tier, render

    tier("Rising Chaos")        # -> "<:chaosx_tier3:123...>"  ("" until installed)
    render("gg :factory_gg:")   # -> "gg <:factory_gg:456...>"

Unknown names degrade safely: ``tier()`` falls back to the unicode ramp owned by
activity.py, and ``render()`` leaves unknown ``:tokens:`` untouched so ordinary colons in
prose survive.
"""
from __future__ import annotations

import re
from typing import Iterable

# Tier label -> emoji name in the Chaos Redux set.
TIER_EMOJI_NAMES: dict[str, str] = {
    "Calm World": "chaosx_tier1",
    "Gathering Storm": "chaosx_tier2",
    "Rising Chaos": "chaosx_tier3",
    "Chaos Tier": "chaosx_tier4",
    "Total Chaos": "chaosx_tier5",
    "World Collapse": "chaosx_collapse",
}

_MENTIONS: dict[str, str] = {}
# The client, so the map can rebuild itself if a startup race leaves it empty.
_BOT = None
_TOKEN = re.compile(r":([A-Za-z0-9_]{2,32}(?::[A-Za-z0-9_]{2,32})?):")
# One pass for both shapes: an existing <:name:id> mention, or a bare :name: shortcode.
_RENDER_RE = re.compile(
    r"<a?:([A-Za-z0-9_]{2,32}):(\d{15,25})>"  # mention
    r"|:([A-Za-z0-9_]{2,32}(?::[A-Za-z0-9_]{2,32})?):"  # shortcode
)
# A real mention: "<:name:123>" (or "<a:name:123>" when animated).
_MENTION_RE = re.compile(r"<a?:([A-Za-z0-9_]{2,32}):(\d{15,25})>")


def install(emojis: Iterable[object]) -> int:
    """Rebuild the name -> ``<:name:id>`` map from a guild's emoji collection."""
    _MENTIONS.clear()
    for item in emojis:
        name = getattr(item, "name", None)
        ident = getattr(item, "id", None)
        if not name or ident is None:
            continue
        prefix = "<a" if getattr(item, "animated", False) else "<"
        _MENTIONS[str(name)] = f"{prefix}:{name}:{ident}>"
    return len(_MENTIONS)


def attach(bot) -> int:
    """Bind the client and resolve every guild's emoji right away."""
    global _BOT
    _BOT = bot
    return refresh()


def refresh() -> int:
    """Re-read the emoji caches of every guild the bot can see."""
    if _BOT is None:
        return len(_MENTIONS)
    _MENTIONS.clear()
    for guild in getattr(_BOT, "guilds", []) or []:
        install(getattr(guild, "emojis", []) or [])
    return len(_MENTIONS)


def _ensure() -> None:
    """A startup race can leave the map empty; rebuild on first use instead of degrading."""
    if not _MENTIONS and _BOT is not None:
        refresh()


def clear() -> None:
    _MENTIONS.clear()


def get(name: str, fallback: str = "") -> str:
    """The emoji mention for ``name``, or ``fallback`` when the bot has not seen it."""
    _ensure()
    return _MENTIONS.get(name, fallback)


def known(name: str) -> bool:
    return name in _MENTIONS


def tier(tier_name: str) -> str:
    """Custom emoji for a tier label, or "" when the set is not installed."""
    _ensure()
    return _MENTIONS.get(TIER_EMOJI_NAMES.get(tier_name, ""), "")


def render(text: str) -> str:
    """Swap ``:emoji_name:`` tokens for real mentions and repair mentions with dead IDs.

    Both shapes are handled in one pass: resolving a shortcode inside an already-correct
    ``<:name:id>`` would wrap it in a second pair of brackets and print garbage.
    """
    _ensure()
    if _MENTIONS is None or ":" not in text:
        return text

    def swap(match: "re.Match[str]") -> str:
        name, ident, token = match.group(1), match.group(2), match.group(3)
        if token is None:
            # "<:name:id>" - keep the icon if we still have it, otherwise re-point at the live ID.
            return _MENTIONS.get(name) or match.group(0)
        # ":name:" - models sometimes spread a shortcode across two colons (":chaosx:tier3:").
        resolved = _MENTIONS.get(token) or _MENTIONS.get(token.replace(":", "_"))
        if resolved:
            return resolved
        if "_" in token:
            # A shortcode nobody has is a stray name, not prose: drop it instead of printing
            # ":factory_vibes:" into a member's answer.
            return ""
        return match.group(0)

    return _RENDER_RE.sub(swap, text)


def catalog_line() -> str:
    """One-line inventory of the installed icons, as exact shortcodes."""
    _ensure()
    if not _MENTIONS:
        return ""
    return ", ".join(sorted(_MENTIONS))


def missing(names: Iterable[str]) -> list[str]:
    """Which of ``names`` the bot has not resolved (useful for startup diagnostics)."""
    return [n for n in names if n not in _MENTIONS]

def install_outbound_hook() -> bool:
    """Render shortcodes on every message ChaosX sends, whichever API path built it."""
    import functools

    import discord

    http = discord.http.HTTPClient
    patched = 0

    def wrap(method: str, *, payload_key: str | None = None) -> None:
        nonlocal patched
        original = getattr(http, method, None)
        if original is None or getattr(original, "_chaosx_rendered", False):
            return

        @functools.wraps(original)
        async def wrapper(self, *args, **kwargs):
            if payload_key is None:
                if isinstance(kwargs.get("content"), str):
                    kwargs["content"] = render(kwargs["content"])
            else:
                payload = kwargs.get(payload_key)
                if isinstance(payload, dict):
                    data = payload.get("data")
                    if isinstance(data, dict) and isinstance(data.get("content"), str):
                        data["content"] = render(data["content"])
                    elif isinstance(payload.get("content"), str) and payload_key == "payload":
                        payload["content"] = render(payload["content"])
            return await original(self, *args, **kwargs)

        wrapper._chaosx_rendered = True  # type: ignore[attr-defined]
        setattr(http, method, wrapper)
        patched += 1

    wrap("send_message")
    wrap("edit_message")
    wrap("create_interaction_response", payload_key="payload")
    wrap("execute_webhook", payload_key="payload")
    wrap("edit_webhook_message", payload_key="payload")
    return bool(patched)
