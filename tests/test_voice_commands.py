"""Voice commands: barge-in, stop, and per-user preferences.

The command routing is exercised by driving a real VoiceChatSession with stubbed audio I/O, so the paths
that decide "is this a command or a question" are covered rather than just their helpers.
"""
from __future__ import annotations

import asyncio
import math
import struct
from pathlib import Path

import pytest

from chaosx_bot.bot import VoicePreferences
from chaosx_bot.voice_chat import VoiceChatSession, _pref_command

STAMP = "2026-09-27T12:00:00+00:00"


def run(coro):
    return asyncio.run(coro)


def loud_pcm(seconds: float = 1.6, freq: int = 220, rate: int = 48000) -> bytes:
    """Speech-like audio that passes the usability gate (not silent, not clipped)."""
    frames = int(seconds * rate)
    return b"".join(
        struct.pack("<h", int(9000 * math.sin(2 * math.pi * freq * i / rate))) for i in range(frames)
    )


class FakeReceiver:
    def __init__(self, buffers: dict[int, bytearray] | None = None) -> None:
        self._buffers = buffers or {}

    def check_silence(self):
        return []


class FakeVoiceClient:
    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


async def stub_answer(*_args, **_kwargs) -> str:
    return ""


def make_session(tmp_path: Path, *, heard: str, prefs: VoicePreferences | None = None, speaking: bool = False):
    session = VoiceChatSession.__new__(VoiceChatSession)
    from chaosx_bot.config import Settings

    settings = Settings()
    settings.voice_work_dir = str(tmp_path)  # keep scratch files in the test dir
    session.settings = settings
    session.bot = type("B", (), {"loop": None})()  # only used by the real _play, which is stubbed here
    session.channel = type("C", (), {"name": "chaosx-live"})()
    session.vc = FakeVoiceClient()
    session.receiver = FakeReceiver({999: bytearray(b"x")} if speaking else {})
    session._queue = asyncio.Queue()
    session._worker = None
    session._poller = None
    session._last_spoke = {}
    session._dropped = 0
    session._history = []
    session._memory_recorder = None
    session._answer_lock = asyncio.Lock()
    session._transcribe_fn = None
    session._speak_fn = None
    session._prefs = prefs
    session._speaking = speaking
    session._stop_requested = False
    session._pending_idea = None
    session._idea_fn = None
    session._answer_fn = None
    session._answer_stream_fn = None
    spoken: list[str] = []

    def transcribe(_wav, _unfiltered=False):
        return heard

    async def speak(text, out_path, pace=None):
        spoken.append(text)
        Path(out_path).write_bytes(b"RIFF")

    async def play(_wav):
        return None

    session._transcribe = transcribe
    session._speak_fn = speak
    session._play = play
    session._keep_audio = lambda _wav: None
    session.spoken_lines = spoken
    return session


def test_command_classification() -> None:
    assert _pref_command("Stop.") == "stop"
    assert _pref_command("be quiet") == "stop"
    assert _pref_command("speak slower please") == "slower"
    assert _pref_command("keep it short") == "short"
    assert _pref_command("reset my voice settings") == "reset"
    # ordinary questions must not be mistaken for commands
    assert _pref_command("what are the zombie super events") is None
    assert _pref_command("stop talking about the black plague and tell me about the rat king") is None
    assert _pref_command("") is None


def test_preferences_persist_and_reset(tmp_path: Path) -> None:
    path = tmp_path / "voice-prefs.json"
    prefs = VoicePreferences(path)
    prefs.update(42, pace=1.6, verbosity="short")
    assert prefs.pace(42, 1.3) == 1.6
    assert prefs.verbosity(42) == "short"

    reloaded = VoicePreferences(path)  # survives a restart
    assert reloaded.pace(42, 1.3) == 1.6
    assert reloaded.verbosity(42) == "short"

    reloaded.update(42, pace=None, verbosity=None)  # reset
    assert VoicePreferences(path).get(42) == {}

    path.write_text("{not json", encoding="utf-8")  # corrupt file degrades, never raises
    assert VoicePreferences(path).pace(42, 1.3) == 1.3


def test_preference_limits_are_clamped(tmp_path: Path) -> None:
    prefs = VoicePreferences(tmp_path / "p.json")
    prefs.update(1, pace=99)
    assert prefs.pace(1, 1.3) == 2.0
    prefs.update(1, pace=0.1)
    assert prefs.pace(1, 1.3) == 0.6
    prefs.update(1, verbosity="nonsense")
    assert prefs.verbosity(1) == "normal"


def test_slower_command_changes_pace_and_confirms(tmp_path: Path) -> None:
    prefs = VoicePreferences(tmp_path / "p.json")
    session = make_session(tmp_path, heard="speak slower", prefs=prefs)
    run(session._handle(42, loud_pcm()))
    assert prefs.pace(42, 1.3) > 1.3
    assert session.spoken_lines and "Slower" in session.spoken_lines[0]


def test_short_command_sets_verbosity(tmp_path: Path) -> None:
    prefs = VoicePreferences(tmp_path / "p.json")
    session = make_session(tmp_path, heard="keep it short", prefs=prefs)
    run(session._handle(42, loud_pcm()))
    assert prefs.verbosity(42) == "short"
    assert session.spoken_lines and "Short answers" in session.spoken_lines[0]


def test_stop_command_cuts_audio_and_does_not_answer(tmp_path: Path) -> None:
    session = make_session(tmp_path, heard="stop", speaking=True)
    answered = {"n": 0}

    async def answer(*_args, **_kwargs):
        answered["n"] += 1
        return "should not be used"

    session._answer_fn = answer
    run(session._handle(42, loud_pcm()))
    assert session.vc.stop_calls == 1
    assert answered["n"] == 0
    assert session.spoken_lines == []


def test_play_is_skipped_once_stopped(tmp_path: Path) -> None:
    session = make_session(tmp_path, heard="x")
    session._stop_requested = True
    run(session._play(tmp_path / "nothing.wav"))  # returns without touching the voice client
    assert session.vc.stop_calls == 0


def test_barge_in_sees_a_filling_buffer(tmp_path: Path) -> None:
    quiet = make_session(tmp_path, heard="x")
    assert quiet.anyone_speaking() is False
    talking = make_session(tmp_path, heard="x", speaking=True)
    assert talking.anyone_speaking() is True
    talking._speaking = True
    talking.stop_talking("someone started talking")
    assert talking.vc.stop_calls == 1


def test_catchup_phrases() -> None:
    from chaosx_bot.bot import catchup_window_hours

    assert catchup_window_hours("what did I miss") == 12.0
    assert catchup_window_hours("Catch me up") == 12.0
    assert catchup_window_hours("what happened in the last hour") == 1.0
    assert catchup_window_hours("what did I miss today") == 12.0
    assert catchup_window_hours("what happened while I was away") == 12.0
    # ordinary knowledge questions must not become catch-up requests
    assert catchup_window_hours("what are the zombie super events") is None
    assert catchup_window_hours("what's new in the mod") is None


def test_idea_utterance_detection() -> None:
    from chaosx_bot.voice_chat import _idea_text

    assert _idea_text("note this idea: a pirate nation in the caribbean") == "a pirate nation in the caribbean"
    assert _idea_text("idea: rat nations get a navy") == "rat nations get a navy"
    assert _idea_text("log an idea - a chaos meter event in the arctic") == "a chaos meter event in the arctic"
    assert _idea_text("ChaosX, note this idea: vampires in transylvania") == "vampires in transylvania"
    # normal questions are not ideas
    assert _idea_text("what are the zombie super events") is None
    assert _idea_text("do you have any ideas about the black plague") is None


def test_confirmation_parsing() -> None:
    from chaosx_bot.voice_chat import _confirmation

    assert _confirmation("yes") is True
    assert _confirmation("Yeah, send it.") is True
    assert _confirmation("no") is False
    assert _confirmation("never mind") is False
    assert _confirmation("what are the zombie super events") is None  # a new question, not a verdict


def test_spoken_idea_needs_a_confirmation_before_filing(tmp_path: Path) -> None:
    filed: list[tuple[int, str]] = []

    async def idea_fn(user_id: int, idea: str) -> str:
        filed.append((user_id, idea))
        return "Filed - that is idea number 7 in the event ideas forum."

    session = make_session(tmp_path, heard="note this idea: a pirate nation in the caribbean")
    session._idea_fn = idea_fn
    run(session._handle(42, loud_pcm()))
    assert filed == []  # nothing is filed on the first utterance
    assert session._pending_idea is not None
    assert "Say yes to send it" in session.spoken_lines[-1]

    session2 = make_session(tmp_path, heard="yes", prefs=None)
    session2._idea_fn = idea_fn
    session2._answer_fn = stub_answer
    session2._pending_idea = (42, "a pirate nation in the caribbean", __import__("time").time() + 90)
    run(session2._handle(42, loud_pcm()))
    assert filed == [(42, "a pirate nation in the caribbean")]
    assert "idea number 7" in session2.spoken_lines[-1]


def test_spoken_idea_can_be_cancelled(tmp_path: Path) -> None:
    filed: list[tuple[int, str]] = []

    async def idea_fn(user_id: int, idea: str) -> str:
        filed.append((user_id, idea))
        return "Filed."

    session = make_session(tmp_path, heard="no")
    session._idea_fn = idea_fn
    session._pending_idea = (42, "a pirate nation", __import__("time").time() + 90)
    run(session._handle(42, loud_pcm()))
    assert filed == []
    assert session._pending_idea is None
    assert session.spoken_lines[-1] == "Cancelled."


def test_another_member_cannot_confirm_someone_elses_idea(tmp_path: Path) -> None:
    filed: list[tuple[int, str]] = []

    async def idea_fn(user_id: int, idea: str) -> str:
        filed.append((user_id, idea))
        return "Filed."

    session = make_session(tmp_path, heard="yes")
    session._idea_fn = idea_fn
    session._answer_fn = stub_answer
    session._pending_idea = (42, "a pirate nation", __import__("time").time() + 90)
    # a different speaker answers the readback: the idea must not be filed, and their own words must not
    # wipe the pending idea belonging to the member who actually asked
    run(session._handle(99, loud_pcm()))
    assert filed == []
    assert session._pending_idea is not None
    assert session._pending_idea[0] == 42


def test_expired_idea_is_not_filed(tmp_path: Path) -> None:
    filed: list[tuple[int, str]] = []

    async def idea_fn(user_id: int, idea: str) -> str:
        filed.append((user_id, idea))
        return "Filed."

    session = make_session(tmp_path, heard="yes")
    session._idea_fn = idea_fn
    session._answer_fn = stub_answer
    session._pending_idea = (42, "a pirate nation", 0.0)  # long expired
    run(session._handle(42, loud_pcm()))
    assert filed == []
