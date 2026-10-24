"""#9: voice answers are attributed to whoever asked, and never blend two askers."""
from __future__ import annotations

import inspect

from chaosx_bot import bot as bot_module
from chaosx_bot.bot import _voice_speaker_line
from chaosx_bot.ideas import board_line


class _Member:
    def __init__(self, name: str) -> None:
        self.display_name = name


class _Guild:
    def __init__(self, member: _Member | None) -> None:
        self._member = member

    def get_member(self, _user_id: int) -> _Member | None:
        return self._member


class _Bot:
    def __init__(self, guild: _Guild | None) -> None:
        self._guild = guild

    def get_guild(self, _guild_id: int) -> _Guild | None:
        return self._guild


def test_speaker_line_names_the_member() -> None:
    line = _voice_speaker_line(_Bot(_Guild(_Member("Siegfried"))), 123, 456)
    assert "Siegfried" in line
    assert "never blend two people's questions" in line
    assert "use their name once" in line


def test_speaker_line_survives_a_missing_member() -> None:
    for bot in (_Bot(None), _Bot(_Guild(None)), _Bot(_Guild(_Member("")))):
        line = _voice_speaker_line(bot, 123, 456)
        assert line.strip()
        assert "never blend" in line
        assert "None" not in line


def test_both_voice_entry_points_add_the_speaker_line() -> None:
    # structural check: the line is appended to the per-call system prompt in the plain and streaming paths
    for name in ("run_voice_ask", "run_voice_ask_stream"):
        source = inspect.getsource(getattr(bot_module, name))
        assert "_voice_speaker_line(" in source, f"{name} does not attribute the speaker"
        assert "system_prompt += " in source, f"{name} does not extend its system prompt"


def test_board_line_shows_votes_only_when_someone_voted() -> None:
    with_votes = board_line(
        submission_id=4, title="pirate nation", status="filed", author="Hoops", age_days=3, votes=7
    )
    assert "🗳️ 7" in with_votes
    without = board_line(submission_id=5, title="vampires", status="filed", author="Siegfried", age_days=1)
    assert "🗳️" not in without
