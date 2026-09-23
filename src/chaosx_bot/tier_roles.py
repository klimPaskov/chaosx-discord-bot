"""Chaos-tier roles: the tier a member reaches colours their name in the server.

Discord has no API for a member's profile accent colour (that is client-side and Nitro-only), so the
tier is expressed the way every levels system does it: one role per chaos tier, coloured with the
mod's own tier colours (`activity.TIER_COLORS`), assigned to the member automatically.

Roles are created once and then kept in sync:
- created with `mentionable=False` and `hoist=False` so a tier can never be pinged and never reorders
  the member list on its own (Hoops: the banter message is the ping; rank roles must not ping),
- the bot's own top role must sit above them, otherwise Discord refuses the assignment (403),
- only members with recorded activity get a tier role, and the previous tier role is removed in the
  same pass so nobody wears two tiers.
"""

from __future__ import annotations

import logging
from typing import Iterable, Mapping

import discord

from .activity import TIERS, TIER_COLORS, tier_for_xp

LOGGER = logging.getLogger("chaosx.tier_roles")

TIER_ROLE_PREFIX = ""  # role names are the tier names themselves


def tier_role_name(tier: str) -> str:
    """The Discord role name for a chaos tier (the tier name, unchanged)."""
    return f"{TIER_ROLE_PREFIX}{tier}"


def plan_role_change(
    member_role_names: Iterable[str],
    tier: str,
    known_tiers: Iterable[str] = TIER_COLORS.keys(),
) -> tuple[set[str], set[str]]:
    """(roles to add, roles to remove) for one member to be showing exactly `tier`.

    Pure so it can be tested without a gateway: `member_role_names` is everything the member currently
    has, and only names that belong to the tier ladder are ever touched - unrelated roles are never
    added or removed.
    """
    wanted = tier_role_name(tier)
    ladder = {tier_role_name(name) for name in known_tiers}
    current = set(member_role_names) & ladder
    return ({wanted} - current, current - {wanted})


async def ensure_tier_roles(
    guild: discord.Guild,
    *,
    bot_member: discord.Member | None = None,
) -> tuple[dict[str, discord.Role], list[str]]:
    """Create any missing tier roles. Returns ({tier: role}, notes).

    Colour is re-applied to existing roles only when it differs, so a manual colour edit by an admin is
    respected rather than overwritten every pass.
    """
    existing = {role.name: role for role in guild.roles}
    notes: list[str] = []
    roles: dict[str, discord.Role] = {}
    for tier, colour in TIER_COLORS.items():
        name = tier_role_name(tier)
        role = existing.get(name)
        try:
            if role is None:
                role = await guild.create_role(
                    name=name,
                    colour=discord.Colour(colour),
                    mentionable=False,
                    hoist=False,
                    reason="ChaosX chaos-tier colour",
                )
                notes.append(f"created `{name}`")
            elif role.colour.value != colour:
                await role.edit(colour=discord.Colour(colour), reason="ChaosX chaos-tier colour")
                notes.append(f"recoloured `{name}`")
        except discord.Forbidden:
            notes.append(f"cannot manage `{name}` (missing Manage Roles)")
            continue
        except discord.HTTPException as exc:
            notes.append(f"`{name}` failed: {exc.status}")
            continue
        roles[tier] = role
    if roles and bot_member is not None:
        notes.extend(await _reposition_ladder(guild, roles, bot_member))
        if bot_member.top_role.position <= max(role.position for role in roles.values()):
            notes.append(
                "the bot's role is not above the tier roles, so Discord will refuse assignments - "
                "move the ChaosX role higher in Server Settings > Roles"
            )
    return roles, notes


async def _reposition_ladder(
    guild: discord.Guild,
    roles: Mapping[str, discord.Role],
    bot_member: discord.Member,
) -> list[str]:
    """Park the ladder directly under the bot's own role, in tier order, in one bulk reorder.

    A member's name colour comes from their HIGHEST positioned coloured role, so a tier role sitting at
    the bottom of the list never shows: whatever coloured role sits higher wins. Keeping the ladder at the
    top of the manageable range (just below the bot's role) makes the tier the colour people see.

    Positions are sent in a single `edit_role_positions` call: moving roles one at a time shifts the
    others, and a per-role loop scattered the ladder across the list (2026-09-23).
    """
    ladder = [tier for tier, _ in TIERS if tier in roles]
    bot_position = bot_member.top_role.position
    if not ladder or bot_position <= 1:
        return []
    # Every movable role gets an explicit position in the same request: Discord shifts the other roles to
    # make room for each move, so naming only the six tier roles left them interleaved with Member,
    # supporters and bot roles (2026-09-23). Naming the whole band keeps the ladder contiguous and on top.
    ladder_names = {tier_role_name(tier) for tier in ladder}
    band = [
        role
        for role in guild.roles
        if 0 < role.position < bot_position and role.name not in ladder_names
    ]
    wanted: dict[discord.Role, int] = {}
    position = bot_position - 1
    for tier in reversed(ladder):  # highest tier at the top of the band
        wanted[roles[tier]] = position
        position -= 1
    for role in sorted(band, key=lambda item: -item.position):
        if position < 1:
            break
        wanted[role] = position
        position -= 1
    if all(role.position == pos for role, pos in wanted.items()):
        return []
    try:
        await guild.edit_role_positions(
            positions=wanted, reason="ChaosX chaos-tier colour ordering"
        )
    except discord.Forbidden:
        return ["cannot reorder the tier roles (a role above them is out of reach)"]
    except discord.HTTPException as exc:
        return [f"reorder failed: {exc.status}"]
    return [f"ladder parked at positions {min(wanted.values())}-{max(wanted.values())}"]


async def sync_tier_role(
    member: discord.Member,
    tier: str,
    roles: Mapping[str, discord.Role],
    *,
    known_tiers: Iterable[str] = TIER_COLORS.keys(),
) -> str:
    """Give one member exactly their tier role. Returns a short status string."""
    add_names, remove_names = plan_role_change(
        [role.name for role in member.roles], tier, known_tiers=known_tiers
    )
    try:
        if remove_names:
            await member.remove_roles(
                *[role for role in member.roles if role.name in remove_names],
                reason="ChaosX chaos tier changed",
            )
        if add_names:
            target = roles.get(tier)
            if target is None:
                return "skipped (no role)"
            await member.add_roles(target, reason="ChaosX chaos tier reached")
    except discord.Forbidden:
        return "forbidden (bot role too low or missing Manage Roles)"
    except discord.HTTPException as exc:
        return f"failed ({exc.status})"
    if add_names:
        return f"now {tier}"
    return "unchanged"


def tier_for_member_xp(xp: float) -> str:
    """Convenience wrapper so callers do not import activity directly for one call."""
    return tier_for_xp(xp)
