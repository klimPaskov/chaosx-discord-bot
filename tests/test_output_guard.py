"""Outbound text discipline: one icon, no signature ending, no leaked sources (Hoops, 2026-11-05)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from chaosx_bot.output_guard import (
    CannedSuppressor,
    EmojiPolicy,
    count_icons,
    strip_notes_phrasing,
    strip_source_tags,
    sanitize_outbound,
)

NOW = datetime(2026, 11, 5, 21, 0, tzinfo=timezone.utc)
SIP = "<a:pepe_sip:1566816289882644520>"
STARE = "<:pepe_stare:1566817303063167077>"
OK = "<:factory_ok:1566819555836436614>"


def test_at_most_one_icon_survives():
    policy = EmojiPolicy()
    text = policy.finalize(f"Bold take {OK} on that {STARE} honestly", channel_id=1, casual=False)
    assert count_icons(text) == 1


def test_factual_answers_are_never_signed_with_an_icon():
    policy = EmojiPolicy()
    text = policy.finalize("Zombie Outbreak fires on day 30 " + OK, channel_id=1, casual=False)
    assert not text.endswith(">")
    assert text.rstrip() == "Zombie Outbreak fires on day 30"


def test_trailing_icon_is_not_a_signature_on_every_message():
    policy = EmojiPolicy()
    first = policy.finalize("That is the third hi today " + SIP, channel_id=1, casual=True)
    assert first.endswith(SIP)  # a short line may carry one
    second = policy.finalize("And a fourth " + STARE, channel_id=1, casual=True)
    assert not second.endswith(STARE)  # the previous message was already signed
    third = policy.finalize("Fifth one now " + STARE, channel_id=1, casual=True)
    assert not third.endswith(STARE)  # ... and this icon is also a repeat


def test_the_same_icon_is_not_reused_inside_the_window():
    policy = EmojiPolicy(recent_window=4)
    policy.finalize("first " + SIP, channel_id=2, casual=True)
    text = policy.finalize("second " + SIP + " and that is the joke", channel_id=2, casual=True)
    assert SIP not in text


def test_structured_answers_never_get_a_trailing_icon():
    policy = EmojiPolicy()
    text = policy.finalize("## Install\n1. Grab the zip\n2. Run the installer " + OK, channel_id=3, casual=True)
    assert not text.endswith(OK)
    assert "Install" in text


def test_long_answers_never_get_a_trailing_icon():
    policy = EmojiPolicy()
    body = "One sentence here. Two sentences here. Three sentences is already too much for a sign-off. " + SIP
    assert not policy.finalize(body, channel_id=4, casual=True).endswith(SIP)


def test_source_tags_are_removed():
    assert strip_source_tags("Grab the zip anywhere [HOI4 Live Tools readme].") == "Grab the zip anywhere."
    assert strip_source_tags("Two servers exist (HOI4 Agent Tools readme).") == "Two servers exist."
    # Brackets that are not a source note survive.
    assert strip_source_tags("Use [the app] to set it up.") == "Use [the app] to set it up."


def test_notes_phrasing_is_neutralised():
    assert "notes" not in strip_notes_phrasing("I checked the Chaos Redux notes and there's nothing on it.")
    assert "vault" not in strip_notes_phrasing("nothing about that in the Chaos Redux vault")
    assert "reference notes" not in strip_notes_phrasing("that is not in my reference notes")
    assert strip_notes_phrasing("The drawer is full.") == "The drawer is full."


def test_notes_rewrites_still_read_like_sentences():
    # The real 2026-10-25 leak.
    assert (
        strip_notes_phrasing("I checked the Chaos Redux notes and they don't cover specific member identities.")
        == "The Chaos Redux material doesn't cover specific member identities."
    )
    assert (
        strip_notes_phrasing("I checked the Chaos Redux notes, but there's no entry for that country.")
        == "There's no entry for that country."
    )
    assert (
        strip_notes_phrasing("I don't have a record of that in my Chaos Redux notes.")
        == "I don't have a record of that in the Chaos Redux material."
    )


def test_sanitize_outbound_handles_both_leaks_at_once():
    cleaned = sanitize_outbound("I checked the Chaos Redux notes [Mod readme] and there is no entry.")
    assert "notes" not in cleaned and "readme" not in cleaned


def test_canned_suppressor_allows_one_line_per_hour():
    suppressor = CannedSuppressor(window_seconds=3600)
    assert suppressor.allow(5, now=NOW)
    assert not suppressor.allow(5, now=NOW + timedelta(minutes=5))
    assert suppressor.allow(5, now=NOW + timedelta(hours=2))
    assert suppressor.allow(6, now=NOW + timedelta(minutes=5))
