from __future__ import annotations

import discord


def is_owner(user_id: int, owner_id: int) -> bool:
    return int(user_id) == int(owner_id)


def is_allowed_guild(guild_id: int | None, allowed_guild_id: int | None) -> bool:
    if allowed_guild_id is None:
        return True
    return guild_id == allowed_guild_id


def safe_allowed_mentions() -> discord.AllowedMentions:
    """Disable mass/role/user mention parsing by default."""

    return discord.AllowedMentions(everyone=False, users=False, roles=False, replied_user=False)


def deny_reason(user_id: int, owner_id: int, guild_id: int | None, allowed_guild_id: int | None) -> str | None:
    if not is_owner(user_id, owner_id):
        return "ChaosX is owner-only. This command is not available to other users."
    if not is_allowed_guild(guild_id, allowed_guild_id):
        return "ChaosX is locked to a different guild."
    return None
