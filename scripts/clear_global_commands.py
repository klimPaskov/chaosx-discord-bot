#!/usr/bin/env python3
from __future__ import annotations

import asyncio

import discord

from chaosx_bot.config import load_settings


async def main_async() -> None:
    settings = load_settings()
    if not settings.discord_token:
        raise SystemExit("Missing CHAOSX_DISCORD_TOKEN")
    client = discord.Client(intents=discord.Intents.default())

    @client.event
    async def on_ready() -> None:
        assert client.application_id is not None
        tree = discord.app_commands.CommandTree(client)
        tree.clear_commands(guild=None)
        await tree.sync(guild=None)
        print(f"Cleared global application commands for application_id={client.application_id}")
        await client.close()

    await client.start(settings.discord_token)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
