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


def targeted_mentions(user_ids: list[int]) -> discord.AllowedMentions:
    """Allow mention parsing ONLY for the given user ids.

    Used for owner/mod-facing output (warned-users list, soft-warning
    notices) so `<@id>` renders as a real clickable user mention — without
    opening mass/role/other-user mention parsing. `users` accepts a list of
    ids or user objects; passing a list restricts parsing to exactly those.
    """

    return discord.AllowedMentions(
        everyone=False,
        users=[discord.Object(id=uid) for uid in dict.fromkeys(user_ids)],
        roles=False,
        replied_user=False,
    )


def public_deny_reason(guild_id: int | None, allowed_guild_id: int | None) -> str | None:
    if not is_allowed_guild(guild_id, allowed_guild_id):
        return "ChaosX is locked to a different guild."
    return None


def owner_deny_reason(user_id: int, owner_id: int, guild_id: int | None, allowed_guild_id: int | None) -> str | None:
    if not is_owner(user_id, owner_id):
        return "This ChaosX command is restricted to the bot operator."
    if not is_allowed_guild(guild_id, allowed_guild_id):
        return "ChaosX is locked to a different guild."
    return None


# Backward-compatible alias used by older tests/imports.
def deny_reason(user_id: int, owner_id: int, guild_id: int | None, allowed_guild_id: int | None) -> str | None:
    return owner_deny_reason(user_id, owner_id, guild_id, allowed_guild_id)
