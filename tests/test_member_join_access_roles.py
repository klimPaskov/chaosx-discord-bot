"""Both access roles are default on join (Hoops 2026-11-06).

The "Here for Chaos Redux only!" / "Also interested in modding!" onboarding question and the reaction-role
panel are gone, so the join has to hand out the community (Member) and mod-development (Modder) roles itself.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chaosx_bot import bot as botmod  # noqa: E402

MEMBER_ROLE_ID = 1526507892310675539
MODDER_ROLE_ID = 1526507893837529138


class FakeRole:
    def __init__(self, role_id, name):
        self.id = role_id
        self.name = name


MEMBER = FakeRole(MEMBER_ROLE_ID, "Member")
MODDER = FakeRole(MODDER_ROLE_ID, "Modder")


class FakeStore:
    async def member_tier(self, user_id):
        return (690.0, "Chaos Tier")  # already on the ladder, so the tier step stays out of the way

    async def set_tier_role_state(self, user_id, tier, when):  # pragma: no cover - not reached
        raise AssertionError("tier state should not change")


class FakeGuild:
    id = 1
    me = SimpleNamespace()

    def get_role(self, role_id):
        return {MEMBER_ROLE_ID: MEMBER, MODDER_ROLE_ID: MODDER}.get(role_id)


class FakeMember:
    def __init__(self, member_id=42, roles=(), bot=False):
        self.id = member_id
        self.display_name = "Newcomer"
        self.guild = FakeGuild()
        self.roles = list(roles)
        self.bot = bot
        self.added = []

    async def add_roles(self, *roles, reason=None):
        self.added.extend(roles)

    async def send(self, *_args, **_kwargs):  # pragma: no cover - DMs are not under test
        pass


def _bot(*, enabled=True, roles_enabled=True):
    settings = SimpleNamespace(
        allowed_guild_id=1,
        tier_roles_enabled=roles_enabled,
        access_reaction_member_role_id=MEMBER_ROLE_ID,
        access_reaction_modder_role_id=MODDER_ROLE_ID,
        default_access_roles_enabled=enabled,
    )
    bot = SimpleNamespace(settings=settings, store=FakeStore())
    bot._send_welcome = lambda member: _coro(None)
    return bot


def test_join_grants_both_access_roles(monkeypatch):
    member = FakeMember()
    monkeypatch.setattr(botmod, "ensure_tier_roles", lambda guild, bot_member=None: _coro(({}, "")))
    asyncio.run(botmod.ChaosXBot.on_member_join(_bot(), member))
    assert member.added == [MEMBER, MODDER]


def test_disabled_tier_roles_still_grant_access(monkeypatch):
    member = FakeMember()
    monkeypatch.setattr(
        botmod,
        "ensure_tier_roles",
        lambda guild, bot_member=None: (_ for _ in ()).throw(AssertionError("tier step must not run")),
    )
    asyncio.run(botmod.ChaosXBot.on_member_join(_bot(roles_enabled=False), member))
    assert member.added == [MEMBER, MODDER]


def test_roles_are_not_re_added_when_already_held(monkeypatch):
    member = FakeMember(roles=[MEMBER, MODDER])
    monkeypatch.setattr(botmod, "ensure_tier_roles", lambda guild, bot_member=None: _coro(({}, "")))
    asyncio.run(botmod.ChaosXBot.on_member_join(_bot(), member))
    assert member.added == []


def test_bots_and_disabled_flag_are_skipped(monkeypatch):
    bot_member = FakeMember(bot=True)
    monkeypatch.setattr(botmod, "ensure_tier_roles", lambda guild, bot_member=None: _coro(({}, "")))
    asyncio.run(botmod.ChaosXBot.on_member_join(_bot(), bot_member))
    assert bot_member.added == []

    off = FakeMember()
    asyncio.run(botmod.ChaosXBot.on_member_join(_bot(enabled=False), off))
    assert off.added == []


def test_default_access_role_ids_reads_both_roles():
    settings = SimpleNamespace(
        default_access_roles_enabled=True,
        access_reaction_member_role_id=MEMBER_ROLE_ID,
        access_reaction_modder_role_id=MODDER_ROLE_ID,
    )
    assert botmod.default_access_role_ids(settings) == [MEMBER_ROLE_ID, MODDER_ROLE_ID]
    settings.default_access_roles_enabled = False
    assert botmod.default_access_role_ids(settings) == []


def _coro(value):
    async def inner():
        return value

    return inner()
