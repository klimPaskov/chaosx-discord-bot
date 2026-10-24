"""The bot must leave the voice channel as soon as the last human leaves."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord

from chaosx_bot.bot import ChaosXBot


class FakeVoiceChannel(discord.VoiceChannel):
    def __init__(self, members):
        self._members = list(members)

    @property
    def members(self):
        return self._members


class FakeVC:
    def __init__(self) -> None:
        self.stopped = False

    def is_connected(self) -> bool:
        return True


class FakeSession:
    def __init__(self, connected: bool = True) -> None:
        self.vc = FakeVC() if connected else None
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


def run(coro):
    return asyncio.run(coro)


def _bot(channel, session):
    bot = object.__new__(ChaosXBot)
    bot.settings = SimpleNamespace(voice_channel_id=555, voice_enabled=True)
    bot.get_channel = lambda _cid: channel
    bot._voice_session = session
    bot.started = 0

    async def start_voice_chat() -> None:
        bot.started += 1

    bot.start_voice_chat = start_voice_chat
    return bot


def test_last_human_leaving_makes_the_bot_leave() -> None:
    channel = FakeVoiceChannel([])
    session = FakeSession()
    bot = _bot(channel, session)
    member = SimpleNamespace(bot=False)
    run(bot.on_voice_state_update(member, None, None))
    assert session.stopped is True, "the bot stayed in an empty channel"
    assert bot._voice_session is None


def test_bot_stays_while_a_human_remains() -> None:
    channel = FakeVoiceChannel([SimpleNamespace(bot=False)])
    session = FakeSession()
    bot = _bot(channel, session)
    run(bot.on_voice_state_update(SimpleNamespace(bot=False), None, None))
    assert session.stopped is False


def test_a_join_makes_the_bot_join() -> None:
    channel = FakeVoiceChannel([SimpleNamespace(bot=False)])
    bot = _bot(channel, None)
    run(bot.on_voice_state_update(SimpleNamespace(bot=False), None, None))
    assert bot.started == 1


def test_other_bots_never_trigger_anything() -> None:
    channel = FakeVoiceChannel([])
    session = FakeSession()
    bot = _bot(channel, session)
    run(bot.on_voice_state_update(SimpleNamespace(bot=True), None, None))
    assert session.stopped is False


def test_a_dead_session_is_treated_as_not_present() -> None:
    channel = FakeVoiceChannel([SimpleNamespace(bot=False)])
    session = FakeSession(connected=False)
    session.vc = None
    bot = _bot(channel, session)
    run(bot.on_voice_state_update(SimpleNamespace(bot=False), None, None))
    # the dead session is cleared first, then a fresh join starts
    assert bot._voice_session is None and bot.started == 1, "a fresh join must follow the idle leave"


def test_voice_disabled_is_inert() -> None:
    channel = FakeVoiceChannel([])
    session = FakeSession()
    bot = _bot(channel, session)
    bot.settings.voice_enabled = False
    run(bot.on_voice_state_update(SimpleNamespace(bot=False), None, None))
    assert session.stopped is False
