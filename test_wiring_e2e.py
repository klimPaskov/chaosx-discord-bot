#!/usr/bin/env python3
"""Exercise the real bot method (not just the module) with a stub client.

Calls ChaosXBot.facts_block_for_request for a live-state question and for an ordinary knowledge question, using
the real database and the real gh CLI, to prove the wiring works and that gating holds.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, "src")
from chaosx_bot.bot import ChaosXBot  # noqa: E402

OWNER = 789502982122373150


class Stub:
    """Only the attributes the methods under test actually touch."""

    # use the real methods under test on the stub instance
    live_facts_for_request = ChaosXBot.live_facts_for_request
    facts_block_for_request = ChaosXBot.facts_block_for_request

    def __init__(self) -> None:
        # take the paths from the host's own .env so the test works on any host
        env = {}
        for line in (Path(__file__).parent / ".env").read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
        self.settings = SimpleNamespace(
            db_path=env.get("CHAOSX_DB_PATH", "/home/klim/chaosx-extract/chaosx.db"),
            github_repo=env.get("CHAOSX_GITHUB_REPO", "klimPaskov/Chaos-Redux"),
            allowed_guild_id=0,
            owner_id=OWNER,
        )
        self.guild_members = SimpleNamespace(_members=[])
        self.store = SimpleNamespace(list_warned_users=self._warned)
        self.get_guild = lambda _gid: None
        self.get_channel = lambda _cid: None
        self.identity_called = 0

    async def _warned(self, **_kwargs):
        return [(OWNER, 1, "2026-10-01T10:00:00+00:00", "mass pings")]

    def server_facts_for_request(self, request: str) -> str:
        self.identity_called += 1
        return "Server facts:\n- Server owner: Hoops McCann"

    def server_facts_block(self) -> str:
        return "Server facts:\n- Server owner: Hoops McCann"


async def main() -> None:
    stub = Stub()  # owner-first ordering is the risky one

    print("=== live-state question ===")
    block = await stub.facts_block_for_request( "how many members are there and what are the open issues?", requester_id=999)
    print(block)
    ok_live = "LIVE DATA" in block and ("Members:" in block or "Busiest channels" in block)
    ok_gh = "Open issues" in block or "Recent commits" in block
    ok_names_withheld = "names withheld" in block
    print("-> live block present:", ok_live, "| github present:", ok_gh, "| names withheld for non-owner:", ok_names_withheld)

    print()
    print("=== knowledge question (must NOT carry live data: fetch only when needed) ===")
    block2 = await stub.facts_block_for_request( "explain how the focus tree works", requester_id=999)
    has_live = "LIVE DATA" in block2
    print(f"-> returned {len(block2)} chars, live block present: {has_live}")

    print()
    print("=== owner asking (names allowed) ===")
    block3 = await stub.facts_block_for_request( "who has been warned?", requester_id=OWNER, always_identity=True)
    owner_sees_name = "names withheld" not in block3
    print(f"-> owner sees names: {owner_sees_name}")

    failures = []
    if not ok_live:
        failures.append("live block missing")
    if not ok_gh:
        failures.append("github section missing")
    if not ok_names_withheld:
        failures.append("moderation names leaked to a non-owner")
    if has_live:
        failures.append("live data fetched for a knowledge question (fetch only when the ask is about the present)")
    if not owner_sees_name:
        failures.append("owner view withheld names")
    print()
    print("RESULT:", "all checks passed" if not failures else f"FAILED: {failures}")
    raise SystemExit(1 if failures else 0)


asyncio.run(main())
