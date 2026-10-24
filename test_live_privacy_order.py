#!/usr/bin/env python3
"""Privacy ordering test: the owner asks FIRST, then a public ask must not inherit the owner's block.

Covers the leak the audience-keyed cache exists to prevent, without importing any test module that runs on import.
"""
import asyncio
import sys
from types import SimpleNamespace

sys.path.insert(0, "/home/klim/chaosx-extract/chaosx-discord-bot/src")
from chaosx_bot.bot import ChaosXBot  # noqa: E402

OWNER = 789502982122373150
PUBLIC = 999999999999999999


class Stub:
    live_facts_for_request = ChaosXBot.live_facts_for_request
    facts_block_for_request = ChaosXBot.facts_block_for_request

    def __init__(self) -> None:
        self.settings = SimpleNamespace(
            db_path="/home/klim/chaosx-extract/chaosx.db",
            github_repo="klimPaskov/Chaos-Redux",
            allowed_guild_id=0,
            owner_id=OWNER,
        )
        self.guild_members = SimpleNamespace(_members=[])
        self.store = SimpleNamespace(list_warned_users=self._warned)
        self.get_guild = lambda _gid: None
        self.get_channel = lambda _cid: None

    async def _warned(self, **_kwargs):
        return [(OWNER, 1, "2026-10-01T10:00:00+00:00", "mass pings")]

    def server_facts_for_request(self, request: str) -> str:
        return "Server facts:\n- Server owner: Hoops McCann"

    def server_facts_block(self) -> str:
        return "Server facts:\n- Server owner: Hoops McCann"


async def main() -> int:
    stub = Stub()
    owner_block = await stub.facts_block_for_request("who has been warned?", requester_id=OWNER, always_identity=True)
    public_block = await stub.facts_block_for_request("who has been warned?", requester_id=PUBLIC)

    owner_has_detail = ("789502982122373150" in owner_block) or ("mass pings" in owner_block)
    public_has_detail = ("789502982122373150" in public_block) or ("mass pings" in public_block)
    public_marked = "names withheld" in public_block

    print("owner block carries the member detail :", owner_has_detail)
    print("public block withholds it             :", public_marked)
    print("PUBLIC LEAK (owner detail in public)  :", public_has_detail)
    print()
    print(public_block.split("Moderation:")[-1].strip()[:300])
    return 1 if (public_has_detail or not public_marked) else 0


raise SystemExit(asyncio.run(main()))
