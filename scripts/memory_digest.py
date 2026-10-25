#!/usr/bin/env python3
"""Weekly memory digest for the owner: what the bot learned, what it retired, where it is weak.

Hoops, 2026-10-25: "a weekly memory digest to you — new facts, contradictions, top gaps, and members whose
memory went quiet, so drift is visible instead of discovered by accident."

Sends a DM to the owner through the Discord REST API (no gateway needed, safe from cron). Falls back to printing
when there is no token, so it can also be read from the log.

Usage:
    .venv/bin/python scripts/memory_digest.py             # DM the owner
    .venv/bin/python scripts/memory_digest.py --dry-run   # print only
    .venv/bin/python scripts/memory_digest.py --days 14
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chaosx_bot import knowledge_gaps, member_memory, moderation_cases  # noqa: E402
from chaosx_bot.config import load_settings  # noqa: E402

API = "https://discord.com/api/v10"


def _post(path: str, token: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (chaosx, 1.0)",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def build_report(db_path: Path, *, days: int) -> str:
    data = member_memory.digest(db_path, days=days)
    members, facts = member_memory.stats(db_path)
    lines = [f"## Memory digest — last {days} days", ""]
    lines.append(f"**{data['new_facts']}** new fact(s) · **{data['superseded']}** superseded · "
                 f"**{members}** members with memory · **{facts}** active facts")
    if data["recent_facts"]:
        lines.append("")
        lines.append("**Newest facts**")
        lines.extend(f"- {fact[:160]}" for fact in data["recent_facts"][:6])
    gaps = knowledge_gaps.open_gaps(db_path, limit=5)
    lines.append("")
    if gaps:
        lines.append("**Docs gaps from 👎**")
        lines.extend(f"- “{topic}” — {count} dislike(s) — e.g. {(sample or '')[:80]}" for topic, count, sample in gaps)
    else:
        lines.append("**Docs gaps** — none open.")
    quiet = data["quiet_members"]
    if quiet:
        lines.append("")
        lines.append("**Memory gone quiet (60d+)**")
        lines.extend(f"- <@{author_id}> — {count} fact(s)" for author_id, count in quiet[:6])
    cases = moderation_cases.latest_cases(db_path, limit=3)
    if cases:
        lines.append("")
        lines.append("**Recent evidence cases**")
        lines.extend(f"- case #{case_id} · {created[:10]} · {action} · {count} message(s)" for case_id, created, action, count, _summary in cases)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    report = build_report(settings.db_path, days=args.days)
    print(report)
    if args.dry_run:
        return 0
    token = str(settings.discord_token).strip()
    if not token:
        print("no token configured; printed only")
        return 0
    channel = _post("/users/@me/channels", token, {"recipient_id": str(settings.owner_id)})
    body = report if len(report) <= 1900 else report[:1890] + "\n…"
    _post(f"/channels/{channel['id']}/messages", token, {"content": body, "allowed_mentions": {"parse": []}})
    print(f"sent to DM channel {channel['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
