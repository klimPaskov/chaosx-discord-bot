"""Citations must exist; refusals must not contradict what retrieval found."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chaosx_bot.grounding import citations, is_ungrounded_refusal, unverified_citations

STRONG_CONTEXT = "## Event 2: Zombie Outbreak\n" + ("The outbreak starts when the infection spreads. " * 20)


def test_finds_backticked_paths_only():
    answer = "The chain lives in `docs/Events/event_002.md` and also mentions events/zombie.txt."
    assert citations(answer) == ["docs/Events/event_002.md"]


def test_flags_paths_that_are_not_in_the_index():
    answer = "Defined in `docs/Events/event_999.md`."
    assert unverified_citations(answer, {"docs/Events/event_002.md"}) == ["docs/Events/event_999.md"]


def test_accepts_a_path_suffix():
    # Answers often name the short form; a suffix match against an indexed path is good enough.
    answer = "See `Events/event_002.md`."
    assert unverified_citations(answer, {"docs/Events/event_002.md"}) == []


def test_ignores_prose_mentions():
    assert citations("The event file for the outbreak has the triggers.") == []


def test_refusal_is_ungrounded_when_context_is_strong():
    assert is_ungrounded_refusal("The knowledge base has nothing on a zombie outbreak.", STRONG_CONTEXT) is True


def test_thin_context_leaves_an_honest_refusal_alone():
    assert is_ungrounded_refusal("I don't have details on that.", "short context") is False


def test_normal_answer_is_not_flagged():
    assert is_ungrounded_refusal("Event 2 starts when the outbreak begins.", STRONG_CONTEXT) is False


def test_bot_marks_unverified_citations_without_deleting_text():
    source = (Path(__file__).resolve().parents[1] / "src" / "chaosx_bot" / "bot.py").read_text(
        encoding="utf-8"
    )
    assert "unverified_citations(output" in source
    assert "(not found in the index)" in source
    assert "is_ungrounded_refusal(output, reference_context)" in source
