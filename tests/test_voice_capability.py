"""The bot must know its own capabilities — especially that it has a voice presence.

Regression guard for a reply that told a member "I don't sit in voice channels at all... voice is
off-limits to me" while the bot was, in fact, sitting in the voice channel.
"""
from __future__ import annotations

from pathlib import Path

from chaosx_bot.bot import ChaosXBot, request_needs_server_facts
from chaosx_bot.hermes_bridge import AUTO_SCAN_BANTER_BOUNDARY, PUBLIC_ASK_BOUNDARY

SOURCE = Path(__file__).resolve().parent.parent / "src" / "chaosx_bot" / "bot.py"


def test_voice_questions_pull_the_server_facts() -> None:
    """Asking about voice must fetch the facts block that describes the voice presence."""
    for question in (
        "were you in the vc?",
        "can you join voice?",
        "is voice off-limits to you?",
        "do you listen in the voice chat",
        "Chaos bot was in the vc",
    ):
        assert request_needs_server_facts(question, ChaosXBot._SERVER_FACTS_LOOKUP_TERMS), question


def test_server_facts_state_the_voice_presence() -> None:
    text = SOURCE.read_text(encoding="utf-8")
    assert "ChaosX is not a text-only bot" in text
    assert "chaosx-live" in text


def test_boundaries_forbid_denying_a_capability() -> None:
    """Both boundaries can answer a message about the bot being in voice, so both must carry the rule."""
    for boundary in (PUBLIC_ASK_BOUNDARY, AUTO_SCAN_BANTER_BOUNDARY):
        assert "you DO have a voice presence" in boundary
        assert "never say you are text-only" in boundary.lower()


def test_bot_does_not_recommend_ask_or_claim_humans_help() -> None:
    """Unchanged behaviour, asserted together with the capability rule so both stay put."""
    assert "Never claim a human will help" in PUBLIC_ASK_BOUNDARY
