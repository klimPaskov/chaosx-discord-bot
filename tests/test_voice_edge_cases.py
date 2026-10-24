"""Edge cases for the voice commands: hijacking, pending ideas, and preference storage.

These codify the rule that a *command* must be an entire utterance: a member who says "stop the black plague
from spreading" is asking a question, not ordering the bot to shut up, and "what did I miss about the zombie
chain" is a content question, not a catch-up request.
"""
from __future__ import annotations

import asyncio
import json
import struct
import time
import wave
from pathlib import Path

import pytest

from chaosx_bot.bot import VoicePreferences, catchup_window_hours
from chaosx_bot.config import Settings
from chaosx_bot.voice_chat import VoiceChatSession, _idea_text, _pref_command

GUILD_ID = 1395459671598436533
CHANNEL_ID = 1395459672055480344


def loud_pcm(seconds: float = 0.8, sample_rate: int = 48000) -> bytes:
    frames = int(seconds * sample_rate)
    return b"".join(struct.pack("<h", int(9000 * (1 if i % 200 < 100 else -1))) for i in range(frames))


class _Prefs(VoicePreferences):
    pass


async def noop_filer(_user_id: int, _idea: str) -> str:
    return "Filed."


def make_session(kind: str) -> VoiceChatSession:
    session = VoiceChatSession.__new__(VoiceChatSession)
    settings = Settings()
    settings.voice_work_dir = Path("/tmp/chaosx-voice-edge")
    session.settings = settings
    session.channel = type("C", (), {"id": CHANNEL_ID, "guild": None, "members": []})()
    session.bot = type("B", (), {"loop": None})()
    session.vc = None
    session.receiver = None
    session._speaking = False
    session._stop_requested = False
    session._pending_idea = None
    session._idea_fn = None
    session._prefs = None
    session._windows = {}
    session._last_spoke = {}
    session._answer_lock = asyncio.Lock()
    session._queue = asyncio.Queue()
    session._worker = None
    session._poller = None
    session._watchdog = None
    session._stop = False
    session._dropped = 0
    session._history = []
    session._memory_recorder = None
    session._transcribe_fn = None
    session._keep_audio = lambda _wav: None
    session.spoken_lines = []

    def transcribe(_path: Path, *_args) -> str:  # the real call is asyncio.to_thread(...)
        return kind

    async def answer(*_args, **_kwargs) -> str:
        session.spoken_lines.append("ANSWERED")
        return "Answered normally."

    async def speak(line: str, _out: Path, *_args) -> None:
        session.spoken_lines.append(line.strip())

    async def play(*_args, **_kwargs) -> None:
        return None

    session._transcribe = transcribe
    session._answer_fn = answer
    session._answer_stream_fn = None
    session._speak_fn = speak
    session._play = play
    return session


def run(coro):
    return asyncio.run(coro)


# --- 1. commands must be whole utterances -----------------------------------------------------------

@pytest.mark.parametrize(
    "utterance",
    [
        "stop the black plague from spreading so fast",
        "slower progression for the zombie chain please",
        "faster than the plague spreads through europe",
        "be quiet about the rat king and answer the question",
        "can you make the answers shorter about the black death",
    ],
)
def test_questions_are_not_mistaken_for_commands(utterance: str) -> None:
    assert _pref_command(utterance) is None


@pytest.mark.parametrize(
    "utterance,expected",
    [
        ("stop", "stop"),
        ("stop talking", "stop"),
        ("speak slower please", "slower"),
        ("can you speak slower", "slower"),
        ("keep it short", "short"),
        ("more detail", "detailed"),
        ("reset my voice settings", "reset"),
    ],
)
def test_real_commands_still_work(utterance: str, expected: str) -> None:
    assert _pref_command(utterance) == expected


# --- 2. catch-up must not swallow content questions -------------------------------------------------

@pytest.mark.parametrize(
    "question",
    [
        "what did I miss about the zombie chain",
        "what happened with the black plague",
        "what happened on the rat king path",
        "anything new about the world end scenario",
    ],
)
def test_content_questions_are_not_catchups(question: str) -> None:
    assert catchup_window_hours(question) is None


@pytest.mark.parametrize("question,window", [("what did I miss", 12.0), ("catch me up", 12.0), ("what happened in the last hour", 1.0)])
def test_real_catchups_still_detected(question: str, window: float) -> None:
    assert catchup_window_hours(question) == window


# --- 3. another member's question must not clear someone else's pending idea ------------------------

def test_another_member_cannot_clear_a_pending_idea() -> None:
    session = make_session("yes")
    session._pending_idea = (42, "a pirate nation", time.time() + 90)
    run(session._handle(99, loud_pcm()))
    assert session._pending_idea is not None, "a bystander's question dropped someone else's pending idea"
    assert session._pending_idea[1] == "a pirate nation"


# --- 4. an idea with no body asks for detail --------------------------------------------------------

def test_idea_without_a_body_asks_for_detail() -> None:
    session = make_session("note this idea")
    run(session._handle(42, loud_pcm()))
    assert session._pending_idea is None
    assert session.spoken_lines, "the bot said nothing at all"
    assert "idea" in session.spoken_lines[-1].lower()
    assert "ANSWERED" not in session.spoken_lines


def test_idea_detection_ignores_empty_bodies() -> None:
    assert _idea_text("note this idea") is None
    assert _idea_text("note this idea:") is None


# --- 5. a very long idea is kept whole but spoken short ---------------------------------------------

def test_long_idea_is_read_back_short() -> None:
    long_idea = "note this idea: " + ("a rat kingdom in the alps with a trade network " * 30)
    session = make_session(long_idea)
    session._idea_fn = noop_filer
    run(session._handle(4201, loud_pcm()))
    assert session._pending_idea is not None
    assert len(session._pending_idea[1]) > 800  # the whole idea is kept
    assert len(session.spoken_lines[-1]) <= 400  # but the readback stays speakable


# --- 6. a second idea replaces the first ------------------------------------------------------------

def test_second_idea_replaces_the_first() -> None:
    first = make_session("note this idea: a pirate nation")
    first._idea_fn = noop_filer
    run(first._handle(4202, loud_pcm()))
    assert first._pending_idea[1] == "a pirate nation"

    second = make_session("note this idea: a vampire count in transylvania")
    second._idea_fn = noop_filer
    second._pending_idea = first._pending_idea
    run(second._handle(4202, loud_pcm()))
    assert second._pending_idea[1] == "a vampire count in transylvania"


# --- 7. preference storage is corruption-proof ------------------------------------------------------

def test_preferences_survive_a_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "voice-prefs.json"
    path.write_text("{ this is not json", encoding="utf-8")
    prefs = _Prefs(path)
    assert prefs.get(42) == {}
    prefs.update(42, pace=1.2)
    assert prefs.get(42)["pace"] == 1.2
    assert json.loads(path.read_text(encoding="utf-8"))["42"]["pace"] == 1.2


def test_preferences_are_per_user(tmp_path: Path) -> None:
    prefs = _Prefs(tmp_path / "voice-prefs.json")
    prefs.update(1, pace=1.4)
    prefs.update(2, verbosity="short")
    assert prefs.get(1) == {"pace": 1.4}
    assert prefs.get(2) == {"verbosity": "short"}
    prefs.update(1, pace=None)  # reset clears the entry entirely
    assert prefs.get(1) == {}


def test_commands_do_not_consume_the_question_budget() -> None:
    """A member saying "keep it short" must not burn one of their five questions for the hour."""
    session = make_session("keep it short")
    session._prefs = VoicePreferences(Path("/tmp/chaosx-voice-edge-prefs.json"))
    for _ in range(8):
        run(session._handle(4300, loud_pcm()))
    assert session.spoken_lines, "preference commands were ignored"
    assert all(line != "ANSWERED" for line in session.spoken_lines[-8:]), "a command reached the model"
