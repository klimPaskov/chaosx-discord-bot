"""Full memory backfill for the ChaosX bot (run on the VPS or locally).

One-shot scan that makes the bot's user memory complete:
1. Registers EVERY guild member into the user registry (silent members too).
2. Reads the FULL readable history of every text channel, thread, and forum
   post in the allowed guild and captures it into the durable message archive
   (deduped by Discord message id — safe to rerun).
3. Force-builds a thorough per-user profile (memory) for every author.

Usage (from the bot repo with the venv active and .env present):

    python scripts/full_memory_backfill.py

The bot must be offline OR the same process must not be capturing at the same
time; sqlite handles concurrent writes but the scan is long, so prefer
stopping the bot for the duration:  systemctl stop chaosx-discord-bot
(when it finishes, start it again).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import discord

from chaosx_bot.config import load_settings
from chaosx_bot.conversation_memory import (
    backfill_capture,
    run_user_profile_compaction_if_due,
    sync_user_registry,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main() -> None:
    settings = load_settings()
    if not settings.discord_token:
        print("Missing CHAOSX_DISCORD_TOKEN in .env", file=sys.stderr)
        raise SystemExit(2)
    guild_id = settings.allowed_guild_id or settings.command_guild_id
    if not guild_id:
        print("No allowed/command guild id configured", file=sys.stderr)
        raise SystemExit(2)

    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True

    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        assert client.user is not None
        me_id = client.user.id
        guild = client.get_guild(guild_id)
        if guild is None:
            print(f"Guild {guild_id} not visible to the bot", file=sys.stderr)
            await client.close()
            return

        print(f"Logged in as {client.user} — scanning guild {guild.name} ({guild.id})")

        # 1) Member registry: ALL members, even silent ones.
        members: list[tuple[int, str, bool]] = []
        try:
            async for member in guild.fetch_members(limit=None):
                if member.bot:
                    continue
                name = (member.display_name or member.name or "").strip()
                if name:
                    members.append((member.id, name, False))
        except Exception as exc:  # noqa: BLE001
            print(f"Member fetch failed (continuing): {type(exc).__name__}: {exc}")
        synced = await sync_user_registry(settings.db_path, members)
        print(f"Registry: {synced} members registered")

        # 2) Full history scan.
        channels: list[discord.abc.Messageable] = []
        seen: set[int] = set()
        for channel in guild.channels:
            if channel.id in seen:
                continue
            if isinstance(channel, (discord.TextChannel, discord.Thread, discord.ForumChannel)):
                channels.append(channel)  # type: ignore[arg-type]
                seen.add(channel.id)
            for thread in getattr(channel, "threads", []):
                if thread.id not in seen:
                    channels.append(thread)  # type: ignore[arg-type]
                    seen.add(thread.id)

        scanned = 0
        captured = 0
        skipped = 0
        errored: list[str] = []
        authors: dict[int, str] = {}
        for channel in channels:
            try:
                async for message in channel.history(limit=None):  # type: ignore[arg-type]
                    scanned += 1
                    author = message.author
                    author_id = author.id if author is not None else 0
                    if author_id == me_id or getattr(author, "bot", False):
                        skipped += 1
                        continue
                    name = (
                        (getattr(author, "display_name", "") or getattr(author, "name", "") or "unknown")
                        if author is not None
                        else "unknown"
                    )
                    ok = await backfill_capture(
                        settings.db_path,
                        guild_id=guild.id,
                        channel_id=channel.id,  # type: ignore[arg-type]
                        author_id=author_id,
                        author_name=name,
                        content=message.content or "",
                        created_at=message.created_at.isoformat(timespec="seconds"),
                        message_id=message.id,
                        allowed_guild_id=guild_id,
                    )
                    if ok:
                        captured += 1
                        authors.setdefault(author_id, name)
            except Exception as exc:  # noqa: BLE001
                errored.append(f"{getattr(channel, 'name', channel.id)} ({type(exc).__name__})")
            if scanned % 2000 == 0 and scanned:
                print(f"  ... {scanned} messages scanned, {captured} new captured")

        print(f"Scan done: {scanned} scanned, {captured} new captured, {skipped} bot/skipped, {len(errored)} unreadable channels")
        if errored:
            print(f"  unreadable: {', '.join(errored[:5])}")

        # 3) Force-build a thorough profile for every author (full archive scan).
        print(f"Building profiles for {len(authors)} users...")
        done = 0
        for author_id in authors:
            try:
                if await run_user_profile_compaction_if_due(settings, author_id, force=True):
                    done += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  profile {author_id} failed: {type(exc).__name__}: {exc}")
            if done % 5 == 0 and done:
                print(f"  ... {done} profiles built")
        print(f"Profiles built: {done}/{len(authors)}")
        print("Backfill complete.")
        await client.close()

    try:
        await client.start(settings.discord_token)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    asyncio.run(main())
