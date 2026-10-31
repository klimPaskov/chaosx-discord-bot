"""Link policy: official links only when asked for, never repeated, never unfurled."""

from __future__ import annotations

import asyncio

import discord
import pytest

from chaosx_bot import link_policy
from chaosx_bot.link_policy import apply_link_policy, asks_for_links, install_plain_message_defaults

KIT = "https://github.com/klimPaskov/Agentic-HOI4-Modding"
SETUP = "https://github.com/klimPaskov/HOI4-Mod-Setup"
PLAYLIST = "https://www.youtube.com/playlist?list=PLh6JmuEabQioc4V8IYGEsMtqiw-xemeX3"


@pytest.fixture(autouse=True)
def _clean():
    link_policy.forget_links()
    yield
    link_policy.forget_links()


def test_feature_question_loses_the_promotional_tail():
    # Real answer from #jazz-and-conversation, 2026-10-29: a question about a submod got two links appended.
    answer = (
        "Then work part by part — units first, decisions second, focuses last.\n\n"
        f"The agent kit itself is here — {KIT} — and the step-by-step series is in the playlist: {PLAYLIST}"
    )
    out = apply_link_policy(answer, question="could the kit handle a TFR submod with new units?", channel_id=1)
    assert "http" not in out
    assert out.startswith("Then work part by part")
    assert "here" not in out  # the pointer sentence went with its link


def test_inline_link_is_removed_without_breaking_the_sentence():
    answer = f"- **Agentic HOI4 Modding** (the skills, rules and workflows) is an open GitHub repo — {KIT}"
    out = apply_link_policy(answer, question="is the kit free?", channel_id=1)
    assert out == "- **Agentic HOI4 Modding** (the skills, rules and workflows) is an open GitHub repo"


def test_link_is_kept_when_asked_for_and_not_repeated_afterwards():
    first = apply_link_policy(f"HOI4 Mod Setup is the installer: {SETUP}", question="where can I download the setup app?", channel_id=7)
    assert SETUP in first
    # The follow-up asks for a link-ish thing again but not that resource explicitly: no repeat.
    second = apply_link_policy(
        f"It also has a browser edition. Grab it from the same repo: {SETUP}",
        question="does the setup app have a guide for the browser version?",
        channel_id=7,
    )
    assert SETUP not in second
    assert "browser edition" in second


def test_explicit_repeat_request_gets_the_link_again():
    apply_link_policy(f"Here: {KIT}", question="send me the kit repo", channel_id=9)
    again = apply_link_policy(f"Sure — {KIT}", question="can you send that link again?", channel_id=9)
    assert KIT in again


def test_other_channels_are_independent_and_web_sources_untouched():
    apply_link_policy(f"Repo: {KIT}", question="link to the kit?", channel_id=1)
    other = apply_link_policy(f"Repo: {KIT}", question="link to the kit?", channel_id=2)
    assert KIT in other
    web = "https://hoi4.paradoxwikis.com/Event_modding"
    kept = apply_link_policy(f"The wiki covers it: {web}", question="how do events fire?", channel_id=3)
    assert web in kept


def test_intro_question_about_a_tool_counts_as_asking_for_it():
    assert asks_for_links("what is HOI4 Agent Tools?")
    assert asks_for_links("where do I download it")
    assert not asks_for_links("does it support maps?")
    assert not asks_for_links("how does the zombie outbreak spread?")


def test_markdown_link_keeps_its_label():
    out = apply_link_policy(f"Install [HOI4 Mod Setup]({SETUP}) and open your mod.", question="how do I start?", channel_id=4)
    assert out == "Install HOI4 Mod Setup and open your mod."


def test_answer_without_official_links_is_unchanged():
    text = "Zombie Outbreak spreads state by state; containment buys time."
    assert apply_link_policy(text, question="how does it spread", channel_id=1) == text


def test_window_expires():
    apply_link_policy(f"Repo: {KIT}", question="kit link?", channel_id=5, now=1000.0)
    later = 1000.0 + link_policy.RECENT_LINK_WINDOW_SECONDS + 1
    assert KIT in apply_link_policy(f"Repo: {KIT}", question="kit repo?", channel_id=5, now=later)


def test_plain_message_defaults_suppress_previews_but_keep_explicit_embeds():
    calls: list[dict] = []

    async def fake_send(self, content=None, **kwargs):
        calls.append(kwargs)
        return None

    original = discord.abc.Messageable.send
    original_installed = link_policy._INSTALLED
    try:
        discord.abc.Messageable.send = fake_send  # type: ignore[method-assign]
        link_policy._INSTALLED = False
        install_plain_message_defaults()

        class Chan(discord.abc.Messageable):
            async def _get_channel(self):  # pragma: no cover - not used
                return self

        chan = Chan()
        asyncio.run(chan.send("plain text with a link https://example.com"))
        asyncio.run(chan.send("status", embed=discord.Embed(title="Status")))
        asyncio.run(chan.send("caller decides", suppress_embeds=False))
    finally:
        discord.abc.Messageable.send = original  # type: ignore[method-assign]
        link_policy._INSTALLED = original_installed
    assert calls[0]["suppress_embeds"] is True
    assert "suppress_embeds" not in calls[1]
    assert calls[2]["suppress_embeds"] is False
