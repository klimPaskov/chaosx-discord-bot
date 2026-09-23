"""A new member starts on the ladder at Calm World (Hoops 2026-09-23)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chaosx_bot import bot as botmod  # noqa: E402


class FakeStore:
    def __init__(self, tier_row=None):
        self._tier_row = tier_row
        self.role_state = []

    async def member_tier(self, user_id):
        return self._tier_row

    async def set_tier_role_state(self, user_id, tier, when):
        self.role_state.append((user_id, tier, when))


class FakeMember:
    def __init__(self, member_id=42, guild_id=1):
        self.id = member_id
        self.display_name = "Newcomer"
        self.guild = SimpleNamespace(id=guild_id, me=SimpleNamespace())
        self.added = []

    async def add_roles(self, role, reason=None):
        self.added.append((role, reason))


def _bot(store, *, enabled=True, guild_id=1, roles=None):
    bot = SimpleNamespace(
        settings=SimpleNamespace(allowed_guild_id=guild_id, tier_roles_enabled=enabled),
        store=store,
    )
    bot._member_has_no_tier = lambda member: botmod.ChaosXBot._member_has_no_tier(bot, member)
    return bot


def test_new_member_gets_calm_world(monkeypatch):
    store = FakeStore(tier_row=None)
    member = FakeMember(member_id=99, guild_id=1)
    calmer = object()
    monkeypatch.setattr(
        botmod, "ensure_tier_roles", lambda guild, bot_member=None: _coro(({"Calm World": calmer}, ""))
    )
    bot = _bot(store)
    _run(botmod.ChaosXBot.on_member_join(bot, member))
    assert member.added and member.added[0][0] is calmer
    assert store.role_state and store.role_state[0][1] == "Calm World"


def test_member_with_a_tier_is_left_alone(monkeypatch):
    store = FakeStore(tier_row=(690.0, "Chaos Tier"))
    member = FakeMember(member_id=99, guild_id=1)
    monkeypatch.setattr(
        botmod, "ensure_tier_roles", lambda guild, bot_member=None: _coro(({"Calm World": object()}, ""))
    )
    bot = _bot(store)
    _run(botmod.ChaosXBot.on_member_join(bot, member))
    assert member.added == []
    assert store.role_state == []


def test_other_guilds_and_disabled_feature_are_ignored(monkeypatch):
    store = FakeStore(tier_row=None)
    calls = []
    monkeypatch.setattr(
        botmod,
        "ensure_tier_roles",
        lambda guild, bot_member=None: calls.append(guild) or _coro(({}, "")),
    )
    _run(botmod.ChaosXBot.on_member_join(_bot(store, guild_id=1), FakeMember(guild_id=2)))
    _run(botmod.ChaosXBot.on_member_join(_bot(store, enabled=False), FakeMember(guild_id=1)))
    assert calls == []


def _coro(value):
    async def inner():
        return value

    return inner()


def _run(coro):
    import asyncio

    return asyncio.run(coro)
