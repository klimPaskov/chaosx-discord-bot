"""Personalised welcome card and DM for new members.

Two surfaces, one voice: a welcome card in the welcome channel with action buttons, and a short DM that tells a
new member what this place is, where to start, and how to talk to the bot. Both use what is actually known about
the member (name, when the account was made) instead of a fixed template.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import discord

RULES_CHANNEL_ID = 1395464062367698974
INTRO_CHANNEL_ID = 1523951448063479930
WELCOME_CHANNEL_ID = 1395470053574574202
ISSUES_CHANNEL_ID = 1490285093955309850
YOUTUBE_URL = "https://www.youtube.com/@ChaosRedux"

HELP_EPHEMERAL = (
    "**How to ask me things.**\n\n"
    "» Mention me — `@ChaosX how do I test an event?` — or use `/ask` with your question.\n"
    "» Ask about events, scenarios, mechanics, testing, the mod, or agentic HOI4 modding and MCP tools.\n"
    "» I answer in the channel, and you can rate the answer with the thumbs if it helped or missed.\n"
    "» `/memory` shows what I remember about you, and lets you wipe it."
)


def _account_age(member: discord.Member) -> str:
    created = member.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    days = max((datetime.now(timezone.utc) - created).days, 0)
    if days < 1:
        return "made today"
    if days < 30:
        return f"{days} day{'s' if days != 1 else ''} old"
    if days < 365:
        months = days // 30
        return f"{months} month{'s' if months != 1 else ''} old"
    years = days // 365
    return f"{years} year{'s' if years != 1 else ''} old"


def _personal_line(member: discord.Member, *, known_facts: list[str] | None = None) -> str:
    """One line that could only have been written for this member."""
    name = member.display_name
    age = _account_age(member)
    facts = [fact for fact in (known_facts or []) if fact][:1]
    if facts:
        return f"{name} — {facts[0]}."
    if member.bot:
        return f"{name} — another bot joining the party."
    if "new" in age or "today" in age:
        return f"{name} — a brand new account, so welcome twice."
    return f"{name} — account {age}, good to have you here."


def build_welcome_text(
    member: discord.Member,
    *,
    member_count: int | None = None,
    known_facts: list[str] | None = None,
) -> str:
    """The message posted in the welcome channel."""
    name = member.display_name
    lines = [
        f"**Welcome, {name}.**",
        "",
        _personal_line(member, known_facts=known_facts),
        "",
        "This server is for **Chaos Redux**, a mod for Hearts of Iron IV — its events, scenarios, mechanics and "
        "testing, plus agentic HOI4 modding and the MCP tooling that goes with it.",
        "",
        "**Where to start**",
        f"» Read the rules in <#{RULES_CHANNEL_ID}> — they are numbered (§ 1–§ 8), and they are short.",
        f"» Say hello in <#{INTRO_CHANNEL_ID}> if you want to be known here.",
        f"» Ask me anything with `@ChaosX` or `/ask` — mechanics, testing, a bug you hit, or how the mod is built.",
        f"» Bugs and test notes go in <#{ISSUES_CHANNEL_ID}> so they do not get lost in chat.",
        f"» Devlogs and showcases: {YOUTUBE_URL}",
        "",
        "I watch the channels and keep this place tidy, so rule-breaking gets handled whether or not a "
        "moderator is around — and every action is explained to the person it affects.",
    ]
    if member_count:
        lines.append("")
        lines.append(f"-# Member #{member_count:,} · Chaos Redux · Hearts of Iron IV")
    return "\n".join(lines)


def build_welcome_dm(
    member: discord.Member,
    *,
    known_facts: list[str] | None = None,
) -> str:
    """The DM a new member gets. Short, useful, no wall of text."""
    name = member.display_name
    lines = [
        f"**Welcome to Chaos Redux, {name}.**",
        "",
        "You just joined the server for **Chaos Redux**, a mod for Hearts of Iron IV. No forms, no "
        "interrogation — here is the whole of what you need:",
        "",
        f"**Rules** — <#{RULES_CHANNEL_ID}>. Numbered § 1 to § 8, readable in a minute. The short version: be "
        "decent, stay on topic, keep it safe.",
        f"**Say hi** — <#{INTRO_CHANNEL_ID}> if you want people to know who you are.",
        f"**Ask anything** — mention `@ChaosX` in a channel, or use `/ask`. Events, scenarios, mechanics, "
        "testing, the mod itself, or agentic HOI4 modding and MCP tools. It answers in the channel, and you can "
        "rate the answer.",
        "**The mod** — coming soon.",
        f"**Videos and devlogs** — {YOUTUBE_URL}",
        "",
        "**About me, since I will be around.** I answer questions, and I also keep the server safe: I remove "
        "spam, gore, doxxing and slurs, and I can warn, time out or ban on my own. Anything I do comes with an "
        "explanation, by DM and in the channel, and you can appeal to Hoops McCann if you think I got it wrong.",
        "",
        "**Your privacy.** I keep a short profile of what you talk about here so answers fit you better. "
        "`/memory` shows exactly what I have, and wipes it if you ask. Anything you tell me in DMs stays out "
        "of the server.",
        "",
        "That's it. Ask something when you feel like it.",
    ]
    return "\n".join(lines)


class WelcomeView(discord.ui.View):
    """Action buttons under the welcome card."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(label="Read the rules", emoji="📕", style=discord.ButtonStyle.link,
                              url=f"https://discord.com/channels/1395459671598436533/{RULES_CHANNEL_ID}")
        )
        self.add_item(
            discord.ui.Button(label="Introduce yourself", emoji="🙋", style=discord.ButtonStyle.link,
                              url=f"https://discord.com/channels/1395459671598436533/{INTRO_CHANNEL_ID}")
        )
        self.add_item(
            discord.ui.Button(label="Watch the devlogs", emoji="▶️", style=discord.ButtonStyle.link, url=YOUTUBE_URL)
        )
        # The "How to ask me" button is declared below as a decorated callback, so it is not added here.

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True

    @discord.ui.button(label="How to ask me", emoji="🤖", style=discord.ButtonStyle.secondary,
                       custom_id="chaosx_welcome_help", row=1)
    async def help_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(HELP_EPHEMERAL, ephemeral=True)


def welcome_facts(state: Any, member: discord.Member) -> list[str]:
    """Anything already known about the member, if the memory layer has it."""
    try:
        facts = state.durable_facts(int(member.id), limit=1) if hasattr(state, "durable_facts") else []
    except Exception:
        facts = []
    return [str(fact) for fact in (facts or [])]
