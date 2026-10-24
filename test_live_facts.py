#!/usr/bin/env python3
"""Exercise the live-data collectors against real sources (the extracted DB and the real gh CLI)."""
import asyncio
import sys
from types import SimpleNamespace

sys.path.insert(0, "src")
from chaosx_bot import live_facts  # noqa: E402

DB = "/home/klim/chaosx-extract/chaosx.db"
REPO = "ChaosRedux/chaos_redux"

checks: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    checks.append((label, ok, detail))
    print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail else ""))


print("=== live data is fetched only when the ask is about the present ===")
for question in ("how many members are there?", "what are the open issues?", "whats in testing right now?",
                 "who has been warned?", "what did germany do in 1936?", "explain the focus tree"):
    want = live_facts.wants_live_facts(question)
    print(f"  wants_live={want!s:5} :: {question}")
check("a status question wants live facts", live_facts.wants_live_facts("how many members are there?"))
check("a knowledge question does not", not live_facts.wants_live_facts("what does the focus tree do?"))


async def main() -> None:
    print("=== db collectors ===")
    channels, members = live_facts._archive_counts(DB, 7)
    check("archive counts read", bool(channels), f"{len(channels)} channels, {len(members)} authors")
    scheduled, nominations = live_facts._playtest_rows(DB)
    check("playtest rows read", isinstance(scheduled, list) and isinstance(nominations, list),
          f"scheduled={len(scheduled)} nominations={len(nominations)}")

    print("=== github (real gh call) ===")
    gh_lines = await live_facts._github_section(REPO)
    for line in gh_lines[:6]:
        print("   ", line)
    check("github section has content", bool(gh_lines), f"{len(gh_lines)} lines")

    print("=== rendered block (playtests + github only) ===")
    fake = SimpleNamespace(
        settings=SimpleNamespace(db_path=DB, github_repo=REPO, allowed_guild_id=0, owner_id=123),
        store=SimpleNamespace(list_warned_users=None),
        get_guild=lambda _gid: None,
        get_channel=lambda _cid: None,
        guild_members=SimpleNamespace(_members=[]),
    )
    block = await live_facts.collect(fake, requester_id=999, force=True)
    print(block)
    check("block carries a timestamp header", "LIVE DATA" in block and "UTC" in block)
    check("block states the placeholders rule", "never guess numbers" in block)
    check("moderation failure is reported, not invented", "Moderation" in block or "Unavailable" in block)

    print()
    failed = [c for c in checks if not c[1]]
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        for label, _ok, detail in failed:
            print("  FAILED:", label, detail)
        raise SystemExit(1)


asyncio.run(main())
