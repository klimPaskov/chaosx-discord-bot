"""The bot must never claim it cannot access something it can.

Reported repeatedly by Hoops: answers that say "I can't pull that video", "I don't have access to live data",
when the capability exists and only a specific tool failed (or nothing failed at all).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chaosx_bot.denial_guard import corrective_prompt, is_denial


def test_detects_capability_denials():
    assert is_denial("I can't access live data, so I can't check that.")
    assert is_denial("I don't have access to YouTube videos.")
    assert is_denial("I am unable to fetch that page.")
    assert is_denial("I can’t read that channel.")  # typographic apostrophe


def test_does_not_flag_ordinary_negative_answers():
    assert not is_denial("There are no open issues right now.")
    assert not is_denial("The mod does not have a zombie event in the current build.")
    assert not is_denial("I can't find that event in the catalog.")
    assert not is_denial("")


def test_corrective_prompt_names_the_capability():
    prompt = corrective_prompt("video downloads")
    assert "you can" in prompt.lower()
    assert "video downloads" in prompt.lower()
    assert "again" in prompt.lower()


def test_guard_is_wired_into_the_answer_path():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "src" / "chaosx_bot" / "bot.py").read_text(
        encoding="utf-8"
    )
    assert "is_denial(output)" in source
    assert "denial retry" in source
    assert "denial_guard_enabled" in source
