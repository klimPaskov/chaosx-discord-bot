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
        f"**{pick(member, OPENERS)}**",
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
    lines.append("")
    lines.append("**Your first moves**")
    offset = int(member.id) % len(CHECKLIST)
    for step in range(3):
        lines.append(f"☐ {CHECKLIST[(offset + step) % len(CHECKLIST)]}")
    lines.append("")
    lines.append(f"-# {pick(member, CLOSERS)}")
    if member_count:
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
        "**Three things to do today**",
        f"☐ Read <#{RULES_CHANNEL_ID}> — numbered § 1 to § 8.",
        f"☐ Say hi in <#{INTRO_CHANNEL_ID}>.",
        "☐ Ask me one question.",
        "",
        "**Your privacy.** I keep a short profile of what you talk about here so answers fit you better. "
        "`/memory` shows exactly what I have, and wipes it if you ask. Anything you tell me in DMs stays out "
        "of the server.",
        "",
        "That's it. Ask something when you feel like it.",
    ]
    return "\n".join(lines)


class WelcomeViewBase(discord.ui.View):
    """Shared error handling: a broken button must say so rather than appear to do nothing."""

    async def on_error(self, interaction, error, item) -> None:  # pragma: no cover - Discord only
        import logging

        logging.getLogger("chaosx.welcome").exception("welcome item %s failed", getattr(item, "label", item))
        try:
            if interaction.response.is_done():
                await interaction.followup.send("That did not go through — try again.", ephemeral=True)
            else:
                await interaction.response.send_message("That did not go through — try again.", ephemeral=True)
        except Exception:
            pass


class WelcomeActionButton(discord.ui.Button):
    """One themed action: scenario roll, lore fragment, nation pick, bug checklist or event idea."""

    def __init__(self, key: str) -> None:
        spec = ACTIONS[key]
        super().__init__(
            label=spec["label"],
            emoji=spec["emoji"],
            style=discord.ButtonStyle.secondary,
            custom_id=f"chaosx_welcome_{key}",
            row=1,
        )
        self.key = key

    async def callback(self, interaction: discord.Interaction) -> None:
        spec = ACTIONS[self.key]
        item = pick(interaction.user, spec["items"], salt=3)
        await interaction.response.send_message(f"{spec['title']}\n\n{item}", ephemeral=True)


class WelcomeHelpButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="How to ask me",
            emoji="🤖",
            style=discord.ButtonStyle.secondary,
            custom_id="chaosx_welcome_help",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(HELP_EPHEMERAL, ephemeral=True)


class WelcomeView(WelcomeViewBase):
    """Links to the essentials, plus two rotating Chaos Redux actions."""

    def __init__(self, member: discord.Member | None = None) -> None:
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
        self.add_item(WelcomeHelpButton())
        offset = (int(member.id) % len(ACTION_ORDER)) if member is not None else 0
        for step in range(2):
            self.add_item(WelcomeActionButton(ACTION_ORDER[(offset + step) % len(ACTION_ORDER)]))


def welcome_facts(state: Any, member: discord.Member) -> list[str]:
    """Anything already known about the member, if the memory layer has it."""
    try:
        facts = state.durable_facts(int(member.id), limit=1) if hasattr(state, "durable_facts") else []
    except Exception:
        facts = []
    return [str(fact) for fact in (facts or [])]


# --- Chaos Redux flavour -------------------------------------------------------------------------------------
# A long list, picked by member id, so two people joining back to back do not read the same greeting.

OPENERS = [
    "A new delegation has arrived in the capital.",
    "Fresh boots on the ground, and the map is still red.",
    "Another commander steps out of the briefing tent.",
    "The war room just gained a chair.",
    "New arrival — the front line is that way.",
    "Somebody new just crossed the border.",
    "A new name enters the ledger.",
    "The general staff has been notified of your arrival.",
    "New blood for the cause, and the cause needs it.",
    "You have been assigned to the Chaos Redux theatre.",
    "A new player joins the long game.",
    "The archive has a new reader.",
    "New arrival: please report to the situation room.",
    "One more mind for the world-building.",
    "The scenario writers have a new colleague.",
    "Fresh recruit, no combat record yet.",
    "You have entered the timeline at a delicate moment.",
    "A new signature appears on the roster.",
    "Someone opened the door and let the chaos in.",
    "The map room is fuller tonight.",
    "Another volunteer for the testing grounds.",
    "New member detected. Morale unchanged, but noted.",
    "The rebellion has a new face.",
    "Welcome to the part of the war that gets written down.",
]

CLOSERS = [
    "Ask something when you are ready — the archive is open.",
    "Lurk if you like. Just read the rules first.",
    "No forms here. Say hi, or don't.",
    "The devlogs explain most of what this is.",
    "Nothing here is finished, which is the interesting part.",
    "Opinions welcome. Numbers welcome more.",
    "If you break something, tell us how.",
    "Questions are cheaper than assumptions.",
    "You will be asked for specifics eventually. Start early.",
    "Bring screenshots and error logs, not vibes.",
    "The scenario list is longer than the changelog.",
    "Everything here is a draft, including the rules.",
]

ACTIONS = {
    "scenario": {
        "label": "Roll a scenario",
        "emoji": "🎲",
        "title": "**Scenario roll**",
        "items": [
            "A neutral nation with one port and no navy declares war on a major.",
            "Three civil wars fire in the same month, all in the same faction.",
            "A puppet state gains independence during a peace conference.",
            "The AI abandons a front line to garrison a single victory point.",
            "A focus tree completes two mutually exclusive branches at once.",
            "A landlocked country attempts a naval invasion.",
            "A faction leader capitulates and its members keep fighting.",
            "Supply collapses for an army standing still in friendly territory.",
            "Two wars merge into one after a white peace that should not have been possible.",
            "An event fires for a country that no longer exists.",
            "A guarantee drags a neutral into a war started on another continent.",
            "A division teleports across a strait it does not border.",
            "A releasable nation appears with the wrong ideology.",
            "A country joins a faction it is at war with.",
            "A peace deal gives a nation territory it never occupied.",
            "A civil war starts in a country with no organised opposition.",
            "An expeditionary force is sent to a country being annexed.",
            "A naval invasion lands on a province with no beach.",
            "A puppet declares war on its overlord's ally.",
            "Two scripted offensives trigger on the same tick and cancel each other.",
        ],
    },
    "lore": {
        "label": "Lore fragment",
        "emoji": "📜",
        "title": "**From the archive**",
        "items": [
            "The capital was moved twice before anyone agreed on the border.",
            "Every faction claims the same founding document, differently edited.",
            "The old rail network still defines where armies can actually go.",
            "Three states share a single river and none of them admit it.",
            "The treaty that ended the last war was never signed, only announced.",
            "The northern ports were built for a navy that no longer exists.",
            "The official history has two endings, both in print.",
            "The disputed province has changed hands six times, twice in one week.",
            "The army's doctrine manual is older than the state it serves.",
            "One nation's founding myth is another's war crime.",
            "The industrial belt was planned as a defensive line.",
            "The currency is backed by a resource nobody can transport.",
            "The last census was taken during a war and counted both sides.",
            "The alliance charter has a clause nobody has ever invoked.",
            "Two dialects, one border, no agreement on which came first.",
            "The capital's garrison answers to a different ministry than the army.",
            "The ceasefire line was drawn by someone who had never seen the terrain.",
            "The oldest unit in the army has never fought.",
            "The railway gauge changes at the most inconvenient border possible.",
            "Every map of the region is wrong about the same lake.",
        ],
    },
    "nation": {
        "label": "Pick my nation",
        "emoji": "🗺️",
        "title": "**Try one of these**",
        "items": [
            "A small industrial power with no coastline and hostile neighbours.",
            "A naval nation with a large fleet and nothing to fuel it.",
            "A neutral state sitting on the only pass through the mountains.",
            "A puppet with more factories than its overlord.",
            "A country that starts with a civil war it cannot win quickly.",
            "A one-province minor with a claim on a major's capital.",
            "A nation whose entire army starts as reserves.",
            "An agrarian state with three rivers and one railway.",
            "A faction leader whose members do not border it.",
            "A country that begins in a war it did not declare.",
            "A buffer state that both sides guarantee.",
            "A minor with a focus tree longer than its industry can support.",
            "A state with ports but no access to open sea.",
            "A nation that starts demobilised in a hostile region.",
            "A country with two cores and one of them contested.",
            "A small state with a single resource the world wants.",
            "A nation with a large army and no supply network.",
            "A minor whose only advantage is terrain.",
            "A country that shares a border with everyone it distrusts.",
            "A puppet that can break free within a year if played well.",
        ],
    },
    "testing": {
        "label": "Testing checklist",
        "emoji": "🧪",
        "title": "**Before you report a bug**",
        "items": [
            "Reproduce it twice, then write down the exact steps.",
            "Check the error log after the moment it breaks, not before.",
            "Note the date in-game and the save version.",
            "Say which mods were loaded, in order.",
            "Screenshot the tooltip, not just the map.",
            "Test with a fresh save before blaming the scenario.",
            "Check whether the same thing happens in vanilla.",
            "Write what you expected, then what happened.",
            "Mention the country you were playing.",
            "Include whether debug mode was on.",
        ],
    },
    "suggest": {
        "label": "Suggest an event",
        "emoji": "🎯",
        "title": "**Event ideas**",
        "items": [
            "Post it in the event ideas forum with a title that says what happens.",
            "Say which country it fires for and roughly when.",
            "State the trigger, the effect, and what makes it interesting.",
            "One event per thread beats a list of twenty ideas.",
            "If it needs a new mechanic, say so up front.",
        ],
    },
}

ACTION_ORDER = ["scenario", "lore", "nation", "testing", "suggest"]


CHECKLIST = [
    f"Skim the rules in <#{RULES_CHANNEL_ID}> — § 1 to § 8, a minute at most.",
    f"Say hi in <#{INTRO_CHANNEL_ID}> so people know who arrived.",
    "Ask @ChaosX one question — events, scenarios, testing, whatever you are curious about.",
    "Watch one devlog so you know what actually exists yet.",
    f"Report the first bug you find in <#{ISSUES_CHANNEL_ID}> — specifics, not vibes.",
    "Suggest an event in the event-ideas forum, one idea per thread.",
    "Open the modding channels if you want the dev side of this.",
    "Read the pinned message in #📕┃rules so you know what gets you banned.",
]


def pick(member: discord.Member, items: list, salt: int = 0) -> Any:
    """Deterministic per member, different for each surface."""
    return items[(int(member.id) // (salt + 1) + salt * 7) % len(items)]


def persistent_view() -> WelcomeViewBase:
    """Every action button at once, so any welcome card's buttons keep working after a restart."""
    view = WelcomeViewBase(timeout=None)
    items: list[discord.ui.Button] = [WelcomeHelpButton()]
    items.extend(WelcomeActionButton(key) for key in ACTION_ORDER)
    for index, item in enumerate(items):
        item.row = 1 + index // 4  # two rows of at most four, so the row limit is never hit
        view.add_item(item)
    return view
