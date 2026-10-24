"""The bot never names a configured member (Hoops: "holly must never be mentioned")."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chaosx_bot.formatting import scrub_names  # noqa: E402


def test_configured_name_is_replaced():
    text = "Thank you Holly, Hoops McCann and kuzon for keeping the chaos going this week."
    scrubbed = scrub_names(text, ["Holly"])
    assert "Holly" not in scrubbed
    assert scrubbed == "Thank you a member, Hoops McCann and kuzon for keeping the chaos going this week."


def test_whole_names_only():
    # a longer name containing the configured text is untouched
    assert scrub_names("Hollywood and Hollyhock keep going", ["Holly"]) == "Hollywood and Hollyhock keep going"
    assert scrub_names("Holly, and Holly.", ["Holly"]) == "a member, and a member."


def test_mentions_and_empty_names_are_handled():
    assert scrub_names("<@110546365032968192> said hi", ["Holly"]) == "<@110546365032968192> said hi"
    assert scrub_names("Holly is here", []) == "Holly is here"
    assert scrub_names("", ["Holly"]) == ""


def test_multiple_names_and_no_partial_matches():
    text = "Holly and Klim and Holly"
    assert scrub_names(text, ["Holly", "Klim"]) == "a member and a member and a member"
