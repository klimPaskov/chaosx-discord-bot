#!/usr/bin/env python3
"""End-to-end proof: build the real answer prompt (live data included) and let the configured model answer.

No Discord I/O — this exercises everything up to and including the model, which is what a member's question gets.
"""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent  # works on any host the bot runs from
sys.path.insert(0, str(ROOT / "src"))

# the bot's own .env drives the model choice, so load it into the process environment
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
from chaosx_bot.knowledge import Knowledge  # noqa: E402

QUESTION = (
    sys.argv[1]
    if len(sys.argv) > 1
    else "How many members do we have and what's the busiest channel this week?"
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


async def main() -> int:
    settings = Settings()
    stub = Stub(settings)
    facts = await stub.facts_block_for_request(QUESTION, requester_id=1)  # a normal member

    # Real retrieval, the same call the bot makes. Passing "" here is what made this harness claim the mod
    # had no zombie outbreak chain while the bot answered it correctly.
    knowledge = Knowledge(
        settings.chaos_redux_repo,
        settings.db_path,
        settings.obsidian_vault_path,
        settings.qoder_repowiki_path,
        catalog_repo=settings.focus_tree_repo or settings.chaos_redux_repo,
        autorebuild=settings.index_autorebuild,
    )
    reference_context = await asyncio.to_thread(knowledge.public_ask_context, QUESTION)

    prompt = build_auto_scan_answer_prompt(
        user_message=QUESTION,
        guild_name="Chaos Redux",
        channel_name="general",
        reference_context=reference_context,
        gate_reason="public question about current server state",
        server_facts=facts,
    )

    print("=== retrieval ===")
    print(f"    {len(reference_context)} chars")
    print("   ", " ".join(reference_context.split())[:200])
    print()
    print("=== the live block the bot injects ===")
    for line in facts.splitlines()[:12]:
        print("   ", line)
    print()
    print("=== feed the assembled prompt to the configured model ===")
    print(f"    model={settings.ask_model} provider={settings.ask_provider}")
    answer = await direct_chat_completion(
        system="You are ChaosX, the Chaos Redux community bot.",
        user=prompt,
        model=settings.ask_model,
        provider=settings.ask_provider,
        reasoning_effort=settings.ask_reasoning_effort,
        timeout_s=90,
    )
    print()
    print("=== model answer ===")
    print(answer)

    lowered = answer.casefold()
    says_it_lacks_access = any(
        phrase in lowered
        for phrase in ("no live", "cannot access live", "don't have live", "do not have live", "no real-time")
    )
    mentions_numbers = any(ch.isdigit() for ch in answer)
    print()
    print("RESULT:", {"still_claims_no_live_access": says_it_lacks_access, "states_numbers": mentions_numbers})
    return 1 if says_it_lacks_access else 0


raise SystemExit(asyncio.run(main()))
