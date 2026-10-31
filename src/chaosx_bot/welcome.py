"""The welcome experience: a short public announcement and a private DM.

Two surfaces, deliberately different (Hoops, 2026-10-31). The channel post is two or three lines of varied,
HOI4-flavoured server voice. The DM is the orientation: what this community is for, what a member can do here,
and an invitation to explore - friendly, concise, with no account metadata and no wall of promotional text.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import random

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


def _personal_line(member: discord.Member, *, known_facts: list[str] | None = None) -> str:
    """One short line for the member, built only from things this server knows about them.

    No Discord account metadata: not when the account was created, how old it is, or any flag on it (Hoops,
    2026-10-31: "Do not display their Discord account age, account creation date, or other account metadata").
    """
    name = member.display_name
    facts = [fact for fact in (known_facts or []) if fact][:1]
    if facts:
        return f"**{name}** — {facts[0]}."
    if member.bot:
        return f"**{name}** — a bot joining the party."
    return f"**{name}** just walked in."


def build_welcome_text(
    member: discord.Member,
    *,
    member_count: int | None = None,
    known_facts: list[str] | None = None,
    opener: str | None = None,
) -> str:
    """The short public announcement in the welcome channel.

    Hoops (2026-10-31): much shorter than the old card, creative and varied, and no account metadata. The long
    orientation now lives in the private DM, so this is two or three lines that read like the server talking.
    """
    line = opener or pick(member, ANNOUNCEMENTS)
    return f"{line.replace('{user}', f'<@{member.id}>')}"


def build_welcome_dm(
    member: discord.Member,
    *,
    known_facts: list[str] | None = None,
) -> str:
    """The private DM a new member gets: what this place is, what to do, and an invitation."""
    name = member.display_name
    lines = [
        pick(member, DM_OPENERS).format(name=name),
        "",
        "This is the community around **Chaos Redux**, a Hearts of Iron IV mod — plus the wider craft of making "
        "HOI4 mods with AI: focus trees, events, decisions, maps, portraits, 3D models, and code that checks its "
        "own work.",
        "",
        "**What people do here**",
        f"» Learn the agentic HOI4 modding workflow — how to set up a coding agent on a mod and what it can do. {YOUTUBE_URL}",
        "» Ask ChaosX, the server's bot, anything about modding: `@ChaosX how do focus trees handle prerequisites?` "
        "or `/ask`. It answers in the channel, and you can rate the answer.",
        "» Share what you are building, or borrow ideas from what others share — the event ideas forum and the "
        "vibe-coding channels are where that happens.",
        f"» Get help with something broken: ask in <#{ISSUES_CHANNEL_ID}> with the error and the steps.",
        f"» Say who you are in <#{INTRO_CHANNEL_ID}> if you want to be known here.",
        "",
        f"**The only rule that matters up front** — the numbered rules are in <#{RULES_CHANNEL_ID}>, and they are "
        "short: be decent, stay roughly on topic, keep it safe.",
        "",
        "**Your privacy.** ChaosX keeps a short profile of what you talk about here so its answers fit you better. "
        "`/memory` shows exactly what it has and wipes it on request. Anything you say in DMs stays out of the server.",
        "",
        "That's the whole briefing. Look around, lurk if you like, and ask something when you feel like it.",
    ]
    if known_facts and pick(member, (True, False), salt=5):
        lines.insert(1, f"-# {known_facts[0]}")
    return "\n".join(lines).strip()


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

ANNOUNCEMENTS = [
    "Another modder has entered the lobby. Welcome {user} — let's see what you'll create.",
    "Reinforcements have arrived! {user} just joined the modding front.",
    "{user} crossed the border and reported for duty. Welcome to Chaos Redux.",
    "New arrival in the war room: {user}. The map is still red, so there's plenty to do.",
    "{user} just opened the door and let the chaos in. Welcome.",
    "The general staff has been notified: {user} is here.",
    "Fresh boots on the ground — welcome, {user}.",
    "{user} joined the timeline at a delicate moment. Welcome aboard.",
    "A new signature appears on the roster: {user}. Welcome.",
    "Someone handed {user} a modding folder and a map. Good luck, and welcome.",
    "{user} has been assigned to the Chaos Redux theatre. Welcome.",
    "New face, new focuses to write. Welcome, {user}.",
    "The archive has a new reader — {user} just joined.",
    "{user} arrived. Morale unchanged, but noted. Welcome.",
    "Another volunteer for the testing grounds: {user}. Welcome.",
    "Look busy, {user} is here. (Welcome.)",
    "The rebellion has a new face. Welcome, {user}.",
    "{user} stepped out of the briefing tent. Welcome to the front.",
    "One more mind for the world-building — welcome, {user}.",
    "The map room is fuller tonight. Welcome, {user}.",
    "Fresh recruit, no combat record yet: {user}. Welcome.",
    "{user} just walked in and the focus tree got longer. Welcome.",
    "New delegation in the capital — {user} has arrived.",
    "Another commander for a war that never quite ends: {user}. Welcome.",
]

DM_OPENERS = [
    "**Welcome to Chaos Redux, {name}.**",
    "**Hey {name} — welcome in.**",
    "**Good to have you here, {name}.**",
    "**Welcome, {name}. Glad you found the place.**",
    "**Hi {name}, welcome to Chaos Redux.**",
    "**You're in, {name}. Welcome.**",
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


def choose_announcement(recent: list[int] | None = None) -> tuple[int, str]:
    """Pick an announcement that has not been used in the last few joins.

    Varied wording is the point (Hoops, 2026-10-31), and random.choice alone repeats annoyingly often on a small
    pool. `recent` holds the indices used most recently, newest last.
    """
    recent = [index for index in (recent or []) if 0 <= index < len(ANNOUNCEMENTS)]
    fresh = [index for index in range(len(ANNOUNCEMENTS)) if index not in recent[-5:]]
    index = random.choice(fresh or list(range(len(ANNOUNCEMENTS))))
    return index, ANNOUNCEMENTS[index]


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


# Roles granted by onboarding answers. The bot cannot read a member's onboarding replies, but it can read the roles
# those replies awarded, which is enough to write the welcome for what they actually said.
SKILL_ROLES = {
    "Skill · New to modding": (
        "brand new to modding — and that is fine. Nothing here assumes you have ever opened a mod folder. Start by "
        "watching a devlog, ask me anything without worrying about sounding basic, and if you want to try making "
        "something, the modding channels are open to you from day one.",
        "New to HOI4 modding",
    ),
    "Skill · Learning": (
        "learning to mod — so expect tutorials rather than lectures. The agentic setup (Codex, the MCP tools, "
        "AGENTS.md) is the shortest path from an idea to something that runs, and I can walk you through it.",
        "Learning HOI4 modding",
    ),
    "Skill · Ships mods": (
        "experienced at shipping mods — so skip the hand-holding. The dev channels and the tooling threads are "
        "where the real work happens, and testers are always short. If you want to contribute, say so and I will "
        "point you at what is unfinished.",
        "Experienced HOI4 modder",
    ),
}

INTEREST_ROLES = {
    "Interest · Events": (
        "Events and scenarios are your thing, so the event ideas forum is the place to be — one idea per thread, "
        "with the country and the trigger.",
        "Interested in events and scenarios",
    ),
    "Interest · Testing": (
        "Testing and bug reports, which is the most useful thing anyone can do here. Reproduce it twice, grab the "
        "error log, and say what you expected. That makes a bug fixable in minutes instead of weeks.",
        "Interested in testing and bug reports",
    ),
    "Interest · Modding": (
        "Modding and the MCP tooling, so you will want the agentic setup: Agent Tools reads a mod folder, Live "
        "Tools drives the running game. Ask me about either.",
        "Interested in modding and MCP tooling",
    ),
    "Interest · Lore": (
        "Lore and worldbuilding, so the lore channel and the introduction thread are yours. The map is not "
        "finished and the arguments about it are ongoing.",
        "Interested in lore and worldbuilding",
    ),
}


def onboarding_answers(member: discord.Member) -> dict[str, list[str]]:
    """What the member's onboarding roles say about them."""
    names = {role.name for role in getattr(member, "roles", []) or []}
    return {
        "skill": [name for name in SKILL_ROLES if name in names],
        "interest": [name for name in INTEREST_ROLES if name in names],
    }


def personal_paragraph(member: discord.Member) -> str:
    """A paragraph written for the answers they gave, or empty when they skipped onboarding."""
    answers = onboarding_answers(member)
    parts: list[str] = []
    for name in answers["skill"][:1]:
        parts.append(SKILL_ROLES[name][0])
    interests = [INTEREST_ROLES[name][0] for name in answers["interest"][:1]]
    parts.extend(interests)
    return " ".join(parts)


def personal_facts(member: discord.Member) -> list[str]:
    """Durable facts to remember, so the bot knows them before they say a word."""
    answers = onboarding_answers(member)
    facts = [SKILL_ROLES[name][1] for name in answers["skill"][:1]]
    facts += [INTEREST_ROLES[name][1] for name in answers["interest"][:2]]
    return facts
