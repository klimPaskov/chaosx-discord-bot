"""Autonomous moderation: decide, cite the rule, and explain.

Hoops, 2026-10-25: the bot should protect the server on its own — catch spam and raids, warn, time out, and ban
obvious spam accounts, with every action explained to the member by DM and to the channel. Established members'
bans go to the owner; unsure cases go to #moderator-only.

This module is deliberately pure: no Discord imports, no I/O. It turns a batch of recent messages into a
verdict with a rule citation from § 7 of the server rules, so the decision can be tested and audited. The wiring
lives in bot.py; the vision check for NSFW/gore is a separate call that feeds `has_sexual_or_gore`.

Blocking is not implementable here: Discord's API lets a *user* block another user, never a bot on their behalf.
A permanent ban is the bot-side equivalent, and the owner gets a one-tap reminder to block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

# § 7 ladder, in order. `warnings` counts previous warnings on record for the member.
LADDER: tuple[tuple[int, str, str], ...] = (
    (1, "warn", "7.1"),
    (2, "timeout", "7.2"),
    (3, "timeout_and_kick", "7.3"),
    (4, "ban", "7.4"),
)

IMMEDIATE_BAN_RULES = ("2.2", "4.1", "5.1", "4.3", "5.4")  # slurs, NSFW, doxxing, malware, ban evasion

_LINK_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
_INVITE_RE = re.compile(r"(?:discord\.gg|discord(?:app)?\.com/invite)/\S+", re.I)
_SHORTENER_RE = re.compile(r"(?:bit\.ly|tinyurl\.com|t\.co|goo\.gl|is\.gd|cutt\.ly|shorturl\.at)/\S+", re.I)
_NITRO_RE = re.compile(r"(?i)free\s+nitro|steam[\s-]?gift|nitro\s+gift|airdrop")

# Blunt on purpose: a false positive here is a warning that a human can undo, a false negative is a member left
# with a slur in the channel.
_SLUR_RE = re.compile(
    r"(?i)\b(n[i1]gg(?:er|a)|f[a4]gg?[o0]t|k[i1]ke|sp[i1]c|ch[i1]nk|tr[a4]nny|ret[a4]rd|"
    r"r[a4]pe\s+(?:you|her|him|them))\b"
)
_THREAT_RE = re.compile(
    r"(?i)\b(?:i(?:'m| am)? (?:going to|gonna) (?:kill|hurt|find) you|kill yourself|kys|"
    r"i will (?:dox|swat|raid) (?:you|your))\b"
)
# Personal data: phone numbers, addresses with a house number, email addresses, ID-like numbers.
_PHONE_RE = re.compile(r"(?<!\d)(?:\+\d{1,3}[\s-]?)?(?:\d[\s-]?){9,14}\d(?!\d)")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")
_ADDRESS_RE = re.compile(
    r"(?i)\b\d{1,4}\s+[A-Za-z][A-Za-z'.-]*(?:\s+[A-Za-z][A-Za-z'.-]*){0,3}\s+"
    r"(?:street|st|avenue|ave|road|rd|lane|ln|drive|dr|boulevard|blvd|court|ct|way)\b"
)

# Reputation cost per action, mirroring activity.MODERATION_XP_PENALTY so the member is told the same number the
# tier calculation charges.
XP_COST: dict[str, int] = {"warn": 40, "timeout": 100, "timeout_and_kick": 250, "kick": 250, "ban": 1000}

INSULT_WORDS = {
    "idiot", "moron", "retard", "dumbass", "imbecile", "faggot", "nigger", "kys", "loser", "clown",
    "worthless", "pathetic", "trash", "scum",
}


@dataclass
class Message:
    id: int
    channel_id: int
    author_id: int
    content: str
    created_at: datetime
    attachment_kinds: tuple[str, ...] = ()  # "image", "video", "file"
    has_sexual_or_gore: bool | None = None  # filled by the image check


@dataclass
class Verdict:
    """What the bot should do, why, and which rule says so."""

    action: str  # "none" | "delete" | "warn" | "timeout" | "timeout_and_kick" | "ban" | "escalate"
    rule: str = ""
    reason: str = ""
    confidence: float = 0.0
    delete_message_ids: list[int] = field(default_factory=list)
    needs_owner_approval: bool = False
    unsure: bool = False


def _is_raid(messages: Iterable[Message], *, window_minutes: int = 10, channels: int = 3) -> bool:
    """Same author, similar content, many channels, minutes."""
    by_author: dict[int, list[Message]] = {}
    for message in messages:
        by_author.setdefault(message.author_id, []).append(message)
    for group in by_author.values():
        if len(group) < channels:
            continue
        window = timedelta(minutes=window_minutes)
        group = sorted(group, key=lambda m: m.created_at)
        for start in group:
            recent = [m for m in group if start.created_at <= m.created_at <= start.created_at + window]
            distinct = {m.channel_id for m in recent}
            if len(distinct) >= channels and len({m.content[:80] for m in recent}) <= max(1, len(recent) // 2):
                return True
    return False


def _link_spam(messages: list[Message], *, window_minutes: int = 10, channels: int = 2) -> bool:
    window = timedelta(minutes=window_minutes)
    with_links = [m for m in messages if _LINK_RE.search(m.content)]
    if len(with_links) < channels:
        return False
    for start in with_links:
        recent = [m for m in with_links if start.created_at <= m.created_at <= start.created_at + window]
        if len({m.channel_id for m in recent}) >= channels:
            return True
    return False


def analyse(
    messages: list[Message],
    *,
    warnings_on_record: int = 0,
    member_is_established: bool = True,
    staff: bool = False,
) -> Verdict:
    """Turn recent activity by one member into an action, a rule and an explanation."""
    if staff or not messages:
        return Verdict(action="none", confidence=1.0, reason="staff or nothing to judge")
    messages = sorted(messages, key=lambda m: m.created_at, reverse=True)
    latest = messages[0]

    if _is_raid(messages) or _link_spam(messages):
        return Verdict(
            action="ban",
            rule="3.4",
            reason="posting the same link across several channels in minutes",
            confidence=0.95,
            delete_message_ids=[m.id for m in messages if _LINK_RE.search(m.content)][:100],
            needs_owner_approval=member_is_established,
        )

    if _INVITE_RE.search(latest.content) or _SHORTENER_RE.search(latest.content):
        return Verdict(
            action="delete",
            rule="3.5" if _INVITE_RE.search(latest.content) else "4.2",
            reason="unsolicited invite or shortened link",
            confidence=0.8,
            delete_message_ids=[latest.id],
        )

    if _NITRO_RE.search(latest.content):
        return Verdict(
            action="ban",
            rule="4.3",
            reason="scam link (free Nitro / fake gift)",
            confidence=0.97,
            delete_message_ids=[latest.id],
            needs_owner_approval=member_is_established,
        )

    if _SLUR_RE.search(latest.content) or _THREAT_RE.search(latest.content):
        return Verdict(
            action="ban",
            rule="2.2",
            reason="slur or threat aimed at a member",
            confidence=0.96,
            delete_message_ids=[latest.id],
            needs_owner_approval=member_is_established,
        )

    if _PHONE_RE.search(latest.content) or _EMAIL_RE.search(latest.content) or _ADDRESS_RE.search(latest.content):
        return Verdict(
            action="delete",
            rule="5.1",
            reason="looks like someone's personal information",
            confidence=0.75,
            delete_message_ids=[latest.id],
            unsure=True,
        )

    if latest.has_sexual_or_gore:
        return Verdict(
            action="ban",
            rule="4.1",
            reason="sexual or gory attachment",
            confidence=0.95,
            delete_message_ids=[latest.id],
            needs_owner_approval=member_is_established,
        )

    lowered = f" {latest.content.lower()} "
    insults = [word for word in INSULT_WORDS if f" {word} " in lowered]
    if insults:
        # Friendly banter and jokes are the whole reason this path stays at warn level.
        return Verdict(
            action="warn",
            rule="2.1",
            reason=f"insulting another member ({insults[0]})",
            confidence=0.6,
            unsure=len(insults) < 2 and len(latest.content) < 120,
        )

    return Verdict(action="none", confidence=1.0, reason="nothing actionable")


def next_step(warnings_on_record: int) -> tuple[str, str]:
    """§ 7 ladder: which action and which rule follows the Nth warning."""
    for count, action, rule in LADDER:
        if warnings_on_record + 1 <= count:
            return action, rule
    return "ban", "7.4"


def explain(
    verdict: Verdict, *, author_id: int | None = None, rules_channel_mention: str = "#📕┃rules"
) -> tuple[str, str]:
    """(DM to the member, channel notice). Both cite the rule, so nobody has to guess."""
    if verdict.action == "none":
        return "", ""
    citation = f"§ {verdict.rule}" if verdict.rule else "the server rules"
    cost = XP_COST.get(verdict.action, 1)
    reputation = (
        f"This also costs **{cost} XP** on your chaos tier — warnings, timeouts and kicks lower your tier value.\n"
        if cost
        else ""
    )
    dm = (
        f"**{verdict.action.replace('_', ' ').title()}** — {verdict.reason}.\n"
        f"Rule: **{citation}** in {rules_channel_mention}.\n"
        f"{reputation}"
        "If you think this is wrong, reply here or appeal to a moderator."
    )
    who = f"<@{author_id}> — " if author_id else ""
    channel = f"{who}message removed: {verdict.reason} ({citation})."
    return dm, channel


def approval_request(verdict: Verdict, *, author: str, author_id: int, sample: str = "") -> str:
    """The escalation the owner reads in #moderator-only."""
    verb = verdict.action.replace("_", " ")
    text = (
        f"**Approval needed** — `{author}` (<@{author_id}>) looks like **{verb}**.\n"
        f"Rule: **§ {verdict.rule}** · confidence {verdict.confidence:.0%}\n"
        f"Why: {verdict.reason}\n"
    )
    if sample:
        text += f"Sample: `{sample[:200]}`\n"
    text += "Press ✅ to approve, ❌ to leave them alone."
    return text


def unsure_request(verdict: Verdict, *, author: str, author_id: int, sample: str = "") -> str:
    return (
        f"**Unsure — no action taken** — `{author}` (<@{author_id}>).\n"
        f"Possible rule: **§ {verdict.rule or '?'}** · confidence {verdict.confidence:.0%}\n"
        f"What I saw: {verdict.reason}\n"
        f"Sample: `{sample[:200]}`\n"
        "I did not want to punish a joke. Tell me if this needs action."
    )
