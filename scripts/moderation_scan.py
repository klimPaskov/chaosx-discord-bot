#!/usr/bin/env python3
"""Scan recent server history with the moderation engine and report — never act.

Hoops, 2026-10-25: "make it scan the server for example, but don't make it take action... the engine should start
only from today, do not take into account past messages right now."

So this script replays what the guard would have decided over the last N messages per channel, prints a table of
findings with the rule that applies, and writes nothing except a report file. The live guard enforces only on
messages that arrive after it booted.

Usage:
    .venv/bin/python scripts/moderation_scan.py --per-channel 300
    .venv/bin/python scripts/moderation_scan.py --per-channel 100 --channel 1395459672055480344
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import discord  # noqa: E402

from chaosx_bot import moderation_guard  # noqa: E402
from chaosx_bot.config import load_settings  # noqa: E402

REPORT = Path("/home/ubuntu/chaosx/moderation-scan.json")


class Scanner(discord.Client):
    def __init__(self, settings, per_channel: int, only_channel: int | None) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(intents=intents)
        self.settings = settings
        self.per_channel = per_channel
        self.only_channel = only_channel
        self.findings: list[dict] = []
        self.scanned = 0

    async def on_ready(self) -> None:
        try:
            await self.scan()
        except Exception as exc:
            print("scan failed:", type(exc).__name__, exc)
        finally:
            await self.close()

    async def scan(self) -> None:
        guild = self.get_guild(int(self.settings.allowed_guild_id))
        if guild is None:
            print("guild not visible")
            return
        by_author: dict[int, list] = defaultdict(list)
        channels = [c for c in guild.text_channels if hasattr(c, "history")]
        if self.only_channel:
            channels = [c for c in channels if c.id == self.only_channel]
        for channel in channels:
            try:
                async for message in channel.history(limit=self.per_channel):
                    if message.author.bot:
                        continue
                    self.scanned += 1
                    by_author[message.author.id].append(
                        moderation_guard.Message(
                            id=message.id,
                            channel_id=channel.id,
                            author_id=message.author.id,
                            content=message.content or "",
                            created_at=message.created_at,
                            attachment_kinds=tuple(
                                "image" if str(getattr(a, "content_type", "")).startswith("image/") else "file"
                                for a in message.attachments
                            ),
                        )
                    )
            except Exception as exc:
                print(f"  {channel.name}: {type(exc).__name__}")

        acting = 0
        for author_id, messages in by_author.items():
            verdict = moderation_guard.analyse(sorted(messages, key=lambda m: m.created_at))
            if verdict.action == "none":
                continue
            member = guild.get_member(author_id)
            name = member.display_name if member else str(author_id)
            would = "would escalate to moderators" if verdict.unsure else f"would {verdict.action}"
            self.findings.append(
                {
                    "author": name,
                    "author_id": author_id,
                    "messages": len(messages),
                    "action": verdict.action,
                    "rule": verdict.rule,
                    "reason": verdict.reason,
                    "confidence": round(verdict.confidence, 2),
                    "unsure": verdict.unsure,
                    "needs_owner_approval": verdict.needs_owner_approval,
                    "sample": (messages[0].content or "")[:120],
                }
            )
            acting += 1
            print(f"- {name}: {would} · § {verdict.rule} · {verdict.reason} · {len(messages)} msg(s)")
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "scanned_messages": self.scanned,
            "members_with_findings": acting,
            "dry_run": True,
            "findings": sorted(self.findings, key=lambda f: f["confidence"], reverse=True),
        }
        try:
            REPORT.write_text(json.dumps(payload, indent=2))
        except OSError:
            pass
        print(f"\nscanned {self.scanned} message(s); {acting} member(s) with findings; report: {REPORT}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-channel", type=int, default=200)
    parser.add_argument("--channel", type=int, default=0)
    args = parser.parse_args()
    settings = load_settings()
    client = Scanner(settings, args.per_channel, args.channel or None)
    asyncio.run(client.start(settings.discord_token))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
