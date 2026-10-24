#!/usr/bin/env python3
"""The answer must fit the question — live data is available, not obligatory.

Hoops, after a reply to a video link that opened with "Couldn't pull that video" and then recited a full server
status report plus unsolicited legal commentary: "fix the bot just completely giving unrelated answers and just
giving me data that was never asked."

The live block rides on every ask by design, so the answer can never claim it lacks current values. This test
pins the other half of that rule: unasked data must not be recited, and asked-for data must still arrive.

    python3 test_relevance_e2e.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

env_file = ROOT / ".env"
for line in env_file.read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip())

from chaosx_bot.ask_api import direct_chat_completion  # noqa: E402
from chaosx_bot.bot import ChaosXBot  # noqa: E402
from chaosx_bot.config import Settings  # noqa: E402
from chaosx_bot.hermes_bridge import build_auto_scan_answer_prompt  # noqa: E402

# Phrases that only appear when the bot recites the status block instead of answering.
STATUS_MARKERS = (
    "open issues",
    "playtests:",
    "latest commits",
    "members:",
    "busiest channel",
    "locked in on",
)

# Phrases that deny a capability the bot has. Without the live block in context the model may invent one of these —
# that was the original bug, and gating the fetch again is what could bring it back.
DENIAL_MARKERS = (
    "no live",
    "can't pull current",
    "cannot pull current",
    "can't access live",
    "cannot access live",
    "don't have live",
    "do not have live",
    "no real-time",
)

# (question, must the answer carry status figures?)
CASES: tuple[tuple[str, bool], ...] = (
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ what do you think of this video?", False),
    ("where is the super event text defined?", False),
    ("what is the current status on things?", True),
)


class Stub:
    live_facts_for_request = ChaosXBot.live_facts_for_request
    facts_block_for_request = ChaosXBot.facts_block_for_request

    def __init__(self, settings) -> None:
        self.settings = settings
        self.guild_members = SimpleNamespace(_members=[])
        self.store = SimpleNamespace(list_warned_users=self._warned)
        self.get_guild = lambda _gid: None
        self.get_channel = lambda _cid: None

    async def _warned(self, **_kw):
        return []

    def server_facts_for_request(self, request: str) -> str:
        return "Server facts:\n- Server owner: Hoops McCann\n- ChaosX bot maker: Hoops McCann"

    def server_facts_block(self) -> str:
        return self.server_facts_for_request("")


async def answer_for(stub, settings, question: str) -> str:
    facts = await stub.facts_block_for_request(question, requester_id=1)
    prompt = build_auto_scan_answer_prompt(
        user_message=question,
        guild_name="Chaos Redux",
        channel_name="general",
        reference_context="",
        gate_reason="public question",
        server_facts=facts,
    )
    return await direct_chat_completion(
        system="You are ChaosX, the Chaos Redux community bot.",
        user=prompt,
        model=settings.ask_model,
        provider=settings.ask_provider,
        reasoning_effort=settings.ask_reasoning_effort,
        timeout_s=90,
    )


async def main() -> int:
    settings = Settings()
    stub = Stub(settings)
    problems: list[str] = []

    for question, wants_status in CASES:
        answer = await answer_for(stub, settings, question)
        lowered = answer.casefold()
        dumped = [marker for marker in STATUS_MARKERS if marker in lowered]
        denied = [marker for marker in DENIAL_MARKERS if marker in lowered]
        has_digits = any(ch.isdigit() for ch in answer)
        print(f"\n=== {question[:60]}")
        print(answer[:420])
        print(f"    status markers: {dumped or 'none'}")
        print(f"    capability denials: {denied or 'none'}")
        if not wants_status and dumped:
            problems.append(f"unasked status dump for {question[:40]!r}: {dumped}")
        if denied:
            problems.append(f"denied a live capability for {question[:40]!r}: {denied}")
        if wants_status and not has_digits:
            problems.append(f"a status question got no figures: {question[:40]!r}")

    print()
    if problems:
        print("RESULT: FAILED")
        for problem in problems:
            print("  ✗", problem)
        return 1
    print("RESULT: answers fit the question — no unasked data, status still live when asked")
    return 0


raise SystemExit(asyncio.run(main()))
