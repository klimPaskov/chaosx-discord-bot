"""Chaos-tier colours: the ladder is the mod's, and the role plan only touches tier roles."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from chaosx_bot.activity import TIER_COLORS, TIERS, tier_for_xp
from chaosx_bot.tier_roles import plan_role_change, tier_role_name


def test_the_ladder_is_the_mods_own():
    # common/script_constants/chaos_meter_constants.txt: tier_0..tier_4 min 0/200/400/600/800,
    # tier_final min 1000 - and the names come from chaosx_chaos_meter_l_english.yml.
    assert [name for name, _ in TIERS] == [
        "Calm World",
        "Gathering Storm",
        "Rising Chaos",
        "Chaos Tier",
        "Total Chaos",
        "World Collapse",
    ]
    assert [threshold for _, threshold in TIERS] == [0, 200, 400, 600, 800, 1000]
    assert tier_for_xp(799) == "Chaos Tier"
    assert tier_for_xp(800) == "Total Chaos"
    assert tier_for_xp(999) == "Total Chaos"
    assert tier_for_xp(1000) == "World Collapse"


def test_every_tier_has_a_colour():
    assert set(TIER_COLORS) == {name for name, _ in TIERS}
    assert len(set(TIER_COLORS.values())) == len(TIER_COLORS)  # six distinguishable colours
    assert all(0 < value <= 0xFFFFFF for value in TIER_COLORS.values())


def test_role_names_are_the_tier_names():
    assert tier_role_name("Rising Chaos") == "Rising Chaos"


def test_plan_adds_the_reached_tier_and_drops_the_previous_one():
    add, remove = plan_role_change(["Rising Chaos", "Member"], "Chaos Tier")
    assert add == {"Chaos Tier"}
    assert remove == {"Rising Chaos"}


def test_plan_leaves_unrelated_roles_alone():
    add, remove = plan_role_change(["Member", "Beta Tester", "Moderator"], "Total Chaos")
    assert add == {"Total Chaos"}
    assert remove == set()


def test_plan_is_a_no_op_when_the_member_already_has_that_tier():
    add, remove = plan_role_change(["Chaos Tier", "Member"], "Chaos Tier")
    assert add == set() and remove == set()


def test_plan_can_drop_every_tier_role_when_the_member_moves_to_calm_world():
    add, remove = plan_role_change(["World Collapse", "Member"], "Calm World")
    assert add == {"Calm World"}
    assert remove == {"World Collapse"}


@pytest.mark.asyncio
async def test_storage_remembers_the_last_synced_tier(tmp_path):
    from chaosx_bot.storage import Store

    store = Store(tmp_path / "chaosx.db")
    await store.init()
    assert await store.tier_role_state(7) is None
    await store.set_tier_role_state(7, "Rising Chaos", "2026-09-23T00:00:00+00:00")
    assert await store.tier_role_state(7) == "Rising Chaos"
    await store.set_tier_role_state(7, "Chaos Tier", "2026-09-24T00:00:00+00:00")
    assert await store.tier_role_state(7) == "Chaos Tier"


class _Role:
    def __init__(self, name: str, position: int) -> None:
        self.name = name
        self.position = position
        self.edits: list[dict] = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        self.position = int(kwargs.get("position", self.position))


class _Guild:
    """Records the single bulk reorder the ladder asks for."""

    def __init__(self, roles: dict[str, "_Role"]) -> None:
        self._roles = roles
        self.roles = list(roles.values())
        self.reorder: dict | None = None

    async def fetch_roles(self):
        return list(self._roles.values())

    async def edit_role_positions(self, *, positions, reason=""):
        self.reorder = dict(positions)
        for role, position in positions.items():
            role.position = position


class _BotMember:
    def __init__(self, position: int) -> None:
        self.top_role = SimpleNamespace(position=position)


@pytest.mark.asyncio
async def test_ladder_is_parked_just_below_the_bots_own_role():
    """A tier colour only shows if the role sits above the member's other coloured roles."""
    from chaosx_bot.tier_roles import _reposition_ladder

    names = ["Calm World", "Gathering Storm", "Rising Chaos", "Chaos Tier", "Total Chaos", "World Collapse"]
    roles = {name: _Role(name, 1) for name in names}
    roles["Modder"] = _Role("Modder", 2)  # an unrelated role must not be touched

    guild = _Guild(roles)
    notes = await _reposition_ladder(guild, roles, _BotMember(9))

    # one bulk reorder of the whole band: World Collapse on top under the bot, Calm World last of the six
    assert guild.reorder is not None
    order = [role.name for role, _ in sorted(guild.reorder.items(), key=lambda item: -item[1])]
    assert order[:6] == ["World Collapse", "Total Chaos", "Chaos Tier", "Rising Chaos", "Gathering Storm", "Calm World"]
    assert "Modder" in order  # unrelated roles are renumbered too, but keep their place below the ladder
    assert any("ladder parked" in note for note in notes)


@pytest.mark.asyncio
async def test_ladder_is_left_alone_when_it_already_sits_right():
    from chaosx_bot.tier_roles import _reposition_ladder

    names = ["Calm World", "Gathering Storm", "Rising Chaos", "Chaos Tier", "Total Chaos", "World Collapse"]
    roles = {name: _Role(name, 3 + index) for index, name in enumerate(names)}
    guild = _Guild(roles)
    notes = await _reposition_ladder(guild, roles, _BotMember(9))
    assert notes == []
    assert guild.reorder is None
    assert all(not role.edits for role in roles.values())
