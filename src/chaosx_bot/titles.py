"""Deliberately ridiculous member titles (Hoops 2026-09-23).

"users should be given like titles (based on their activity and personality, etc). So in my case (they should
be deliberately a little ridiculous and over the top) ... I should be like the royalty, because i am the
creator of everything."

Rules baked in here:
- a title is a *display* thing: it never grants authority, never pings anyone, and never names another member;
- it is generated from facts the bot already holds (tier, chaos, messages, active days, contributions) plus
  the member's own public chat style, and falls back to a deterministic silly title when no model is available;
- the owner does not get a generated title at all: his is configured, royal and stable.
"""
from __future__ import annotations

import re

MAX_TITLE_WORDS = 12

TITLE_PROMPT = """You are naming one member of a Hearts of Iron IV modding community called Chaos Redux.

Write ONE title for {name}, deliberately over-the-top and a little ridiculous, in the style of a fantasy
court: for example "Keeper of Six Messages a Day", "Duke of the Diminishing Returns", "Warden of the
Convoy Payouts", "Baron of the Late-Night Questions". It must feel earned from the facts below, not random.

Facts:
{facts}

Rules:
- exactly one title, {words} words or fewer, no quotes, no emoji, no trailing punctuation;
- the member's activity, interests and tone should be recognisable in it;
- never mention any other member by name, never mention Discord, roles, tiers or points;
- playfully pompous, never insulting: no insults about intelligence, body, race, religion, gender or age.

Answer with the title alone, nothing else."""


def fallback_title(*, tier: str, messages: int, contributions: int, active_days: int) -> str:
    """A deterministic pompous title when no model is available (or declined)."""
    if contributions >= 3:
        return "Grand Architect of Ideas"
    if messages >= 200:
        return "Warden of the Endless Conversation"
    if active_days >= 30:
        return "Steward of the Long Watch"
    if tier not in ("", "Calm World"):
        return f"Keeper of the {tier}"
    return "Heir Apparent to the Chaos Ladder"


def clean_title(text: str) -> str:
    """Tidy a model answer into a bare title (keeps it from smuggling emoji, quotes or a second line)."""
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    line = re.sub(r"^[^0-9A-Za-z]+", "", line)  # drop leading markdown/quote/bullet noise
    line = re.sub(r"[^0-9A-Za-z)]+$", "", line).strip()  # no trailing punctuation/padding
    line = re.sub(r"\s{2,}", " ", line)
    words = line.split()
    if len(words) > MAX_TITLE_WORDS:
        line = " ".join(words[:MAX_TITLE_WORDS]).rstrip(" ,;:-")
    if len(line) < 3:
        return ""
    if "@" in line or "<@" in line:  # a title must never carry a ping
        return ""
    return line


def title_facts_line(*, name: str, tier: str, chaos: int, messages: int, active_days: int,
                     contributions: list[str], style: str = "") -> str:
    """The facts block handed to the model (public activity only, no private context)."""
    parts = [
        f"member: {name}",
        f"chaos tier: {tier} with {int(chaos)} chaos",
        f"messages: {int(messages)} over {int(active_days)} active days",
    ]
    if contributions:
        parts.append("contributions: " + ", ".join(contributions[:6]))
    if style:
        parts.append(f"their own words in public chat: {style[:280]}")
    return "\n".join(parts)
