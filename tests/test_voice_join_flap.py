"""A failing voice join must not flap: no half-open connection, no join/leave loop.

Reported from the server as "the bot keeps appearing and reappearing": discord.py hands a fresh
VoiceClient a MISSING transport key, `bytes(MISSING)` raised TypeError inside VoiceReceiver, and
every retry showed the bot joining and immediately leaving again.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from chaosx_bot import bot as bot_module
from chaosx_bot.bot import ChaosXBot
from chaosx_bot.voice_chat import VoiceChatSession, VoiceReceiver


class _Conn:
    secret_key = discord.utils.MISSING
    dave_session = None
    ssrc = 4242


class _VC:
    _connection = _Conn()

    def is_connected(self) -> bool:
        return True


def test_receiver_tolerates_a_missing_transport_key():
    receiver = VoiceReceiver(_VC())
    key, dave = receiver._current_keys()
    assert key == b""
    assert dave is None
    assert receiver._bot_ssrc == 4242


def test_receiver_reads_the_key_once_discord_fills_it():
    class _Late:
        secret_key = [1, 2, 3, 4]
        dave_session = None
        ssrc = 7

    class _LateVC:
        _connection = _Late()

    receiver = VoiceReceiver(_LateVC())
    assert receiver._current_keys()[0] == b"\x01\x02\x03\x04"


class FakeVoiceChannel(discord.VoiceChannel):
    def __init__(self, members):
        self._members = list(members)
        self.name = "chaosx-live"
        self.id = 555
        self.connect_calls = 0
        self.vc = None

    @property
    def members(self):
        return self._members

    async def connect(self, **kwargs):  # noqa: D401 - test double
        self.connect_calls += 1
        return self.vc


def _settings(tmp_path: Path):
    return SimpleNamespace(
        voice_enabled=True,
        voice_channel_id=555,
        voice_dave_mode="off",
        voice_max_queue=3,
        voice_tts_length_scale=1.30,
        voice_work_dir=str(tmp_path),
        voice_stt_model="base",
        voice_stt_provider="local",
        voice_stt_groq_model="whisper-large-v3-turbo",
        voice_tts_provider="piper",
        voice_tts_edge_voice="en-GB-RyanNeural",
        voice_tts_voice="/nonexistent.onnx",
        voice_tts_speaker="p254",
    )


def test_a_failed_receiver_leaves_no_half_open_connection(monkeypatch, tmp_path):
    channel = FakeVoiceChannel([SimpleNamespace(bot=False)])

    class _DeadVC:
        def __init__(self):
            self.disconnected = False

        def is_connected(self):
            return True

        async def disconnect(self):
            self.disconnected = True

    dead = _DeadVC()
    channel.vc = dead

    def boom(*args, **kwargs):
        raise TypeError("cannot convert '_MissingSentinel' object to bytes")

    monkeypatch.setattr("chaosx_bot.voice_chat.VoiceReceiver", boom)

    session = VoiceChatSession(
        bot=None, channel=channel, answer_fn=lambda *a: None, settings=_settings(tmp_path)
    )

    with pytest.raises(TypeError):
        asyncio.run(session.start())
    assert dead.disconnected, "a failed start must disconnect, or the bot flickers in the channel"


def test_a_failing_join_backs_off_instead_of_retrying_on_every_event(monkeypatch, tmp_path):
    attempts: list[int] = []

    class _FailingSession:
        def __init__(self, *args, **kwargs):
            attempts.append(1)
            self.vc = None

        async def start(self):
            raise RuntimeError("voice handshake failed")

    monkeypatch.setattr(bot_module, "VoiceChatSession", _FailingSession)

    channel = FakeVoiceChannel([SimpleNamespace(bot=False)])
    bot = object.__new__(ChaosXBot)
    bot.settings = _settings(tmp_path)
    bot.get_channel = lambda _cid: channel
    bot.voice_memory_recorder = lambda _ch: None
    bot.voice_idea_recorder = lambda _ch: None
    bot.voice_prefs = None
    bot._voice_session = None

    asyncio.run(bot.start_voice_chat())
    assert len(attempts) == 1
    assert bot._voice_fails == 1
    assert bot._voice_fail_until > time.time()

    # The voice-state event fires constantly; inside the backoff window nothing may retry.
    asyncio.run(bot.start_voice_chat())
    assert len(attempts) == 1, "a retry inside the backoff window would flap the member list"
    assert bot._voice_fail_until - time.time() <= 20.0


def test_backoff_grows_and_a_successful_join_clears_it(monkeypatch, tmp_path):
    class _Flaky:
        attempts = 0

        def __init__(self, *args, **kwargs):
            self.vc = None

        async def start(self):
            _Flaky.attempts += 1
            if _Flaky.attempts < 3:
                raise RuntimeError("voice handshake failed")
            return True

    monkeypatch.setattr(bot_module, "VoiceChatSession", _Flaky)

    channel = FakeVoiceChannel([SimpleNamespace(bot=False)])
    bot = object.__new__(ChaosXBot)
    bot.settings = _settings(tmp_path)
    bot.get_channel = lambda _cid: channel
    bot.voice_memory_recorder = lambda _ch: None
    bot.voice_idea_recorder = lambda _ch: None
    bot.voice_prefs = None
    bot._voice_session = None

    asyncio.run(bot.start_voice_chat())
    first = bot._voice_fail_until - time.time()
    bot._voice_fail_until = 0.0  # pretend the cooldown elapsed
    asyncio.run(bot.start_voice_chat())
    second = bot._voice_fail_until - time.time()
    assert second > first, "the second failure must wait longer than the first"

    bot._voice_fail_until = 0.0
    asyncio.run(bot.start_voice_chat())
    assert bot._voice_fails == 0
    assert bot._voice_fail_until == 0.0
    assert bot._voice_session is not None
