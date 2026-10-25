#!/usr/bin/env python3
"""Scan recent server history for rule violations and report — never act. Model-screened, not keyword-matched.

The first version of this script missed doxxing and insults because it only matched a small word list; a
misspelling ("rasist") or a casually dropped real name sailed straight past it. This version sends batches of real
messages to the configured model with the rule set, merges that with the deterministic heuristics, and writes a
report. Nothing is deleted, warned or banned.

Usage:
    .venv/bin/python scripts/moderation_scan.py --per-channel 400
    .venv/bin/python scripts/moderation_scan.py --per-channel 200 --channel 1395459672055480344 --no-model
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import discord  # noqa: E402

from chaosx_bot import ask_api, moderation_guard  # noqa: E402
from chaosx_bot.config import load_settings  # noqa: E402

REPORT = Path("/home/ubuntu/chaosx/moderation-scan.json")
BATCH = 20


class Scanner(discord.Client):
    def __init__(self, settings, per_channel: int, only_channel: int | None, use_model: bool) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(intents=intents)
        self.settings = settings
        self.per_channel = per_channel
        self.only_channel = only_channel
        self.use_model = use_model
        self.collected: list[dict] = []

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
        channels = [c for c in guild.text_channels if hasattr(c, "history")]
        if self.only_channel:
            channels = [c for c in channels if c.id == self.only_channel]
        for channel in channels:
            try:
                async for message in channel.history(limit=self.per_channel):
                    if message.author.bot or not (message.content or "").strip():
                        continue
                    member = guild.get_member(message.author.id)
                    perms = getattr(member, "guild_permissions", None)
                    if perms and (perms.manage_messages or perms.kick_members or perms.ban_members):
                        continue
                    if message.author.id == self.settings.owner_id:
                        continue
                    self.collected.append(
                        {
                            "id": str(message.id),
                            "channel": f"#{channel.name}",
                            "author": message.author.display_name,
                            "author_id": message.author.id,
                            "content": (message.content or "")[:400],
                            "created_at": message.created_at.isoformat(),
                        }
                    )
            except Exception as exc:
                print(f"  {channel.name}: {type(exc).__name__}")

        heuristics = self._heuristics()
        model_findings = await self._model_findings()
        merged = heuristics + model_findings
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "scanned_messages": len(self.collected),
            "dry_run": True,
            "heuristic_findings": len(heuristics),
            "model_findings": len(model_findings),
            "findings": merged,
        }
        REPORT.write_text(json.dumps(report, indent=2))
        print(
            f"\nscanned {len(self.collected)} message(s): "
            f"{len(heuristics)} heuristic, {len(model_findings)} model finding(s)"
        )
        for finding in merged[:40]:
            print(
                f"- {finding.get('rule')} · {finding['author']} · {str(finding.get('reason'))[:50]} "
                f"· “{finding['content'][:70]}”"
            )
        print(f"report: {REPORT}")

    def _heuristics(self) -> list[dict]:
        out: list[dict] = []
        for item in self.collected:
            verdict = moderation_guard.analyse(
                [
                    moderation_guard.Message(
                        id=int(item["id"]),
                        channel_id=0,
                        author_id=item["author_id"],
                        content=item["content"],
                        created_at=datetime.fromisoformat(item["created_at"]),
                    )
                ],
                member_is_established=True,
            )
            if verdict.action != "none" and not verdict.unsure:
                out.append({**item, "rule": verdict.rule, "reason": verdict.reason, "source": "heuristic"})
        return out

    async def _model_findings(self) -> list[dict]:
        if not self.use_model:
            return []
        out: list[dict] = []
        for start in range(0, len(self.collected), BATCH):
            batch = self.collected[start : start + BATCH]
            findings = await ask_api.classify_messages_batch([item["content"] for item in batch])
            for finding in findings:
                try:
                    index = int(finding.get("index"))
                    confidence = float(finding.get("confidence") or 0)
                except (TypeError, ValueError):
                    continue
                if confidence < 0.5 or not (0 <= index < len(batch)):
                    continue
                item = batch[index]
                out.append(
                    {
                        **item,
                        "rule": str(finding.get("rule") or "?"),
                        "reason": str(finding.get("reason") or ""),
                        "source": "model",
                        "confidence": confidence,
                    }
                )
        return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-channel", type=int, default=300)
    parser.add_argument("--channel", type=int, default=0)
    parser.add_argument("--no-model", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    client = Scanner(settings, args.per_channel, args.channel or None, use_model=not args.no_model)
    asyncio.run(client.start(settings.discord_token))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
