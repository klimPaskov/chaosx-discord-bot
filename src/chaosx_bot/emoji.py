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
_TOKEN = re.compile(r":([A-Za-z0-9_]{2,32}):")


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


def clear() -> None:
    _MENTIONS.clear()


def get(name: str, fallback: str = "") -> str:
    """The emoji mention for ``name``, or ``fallback`` when the bot has not seen it."""
    return _MENTIONS.get(name, fallback)


def known(name: str) -> bool:
    return name in _MENTIONS


def tier(tier_name: str) -> str:
    """Custom emoji for a tier label, or "" when the set is not installed."""
    return _MENTIONS.get(TIER_EMOJI_NAMES.get(tier_name, ""), "")


def render(text: str) -> str:
    """Swap ``:emoji_name:`` tokens for real mentions; leave unknown tokens alone."""
    if not _MENTIONS or ":" not in text:
        return text
    return _TOKEN.sub(lambda m: _MENTIONS.get(m.group(1), m.group(0)), text)


def missing(names: Iterable[str]) -> list[str]:
    """Which of ``names`` the bot has not resolved (useful for startup diagnostics)."""
    return [n for n in names if n not in _MENTIONS]
