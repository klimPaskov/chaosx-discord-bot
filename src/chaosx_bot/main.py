from __future__ import annotations

import sys

import discord

from .bot import ChaosXBot
from .config import load_settings


def main() -> None:
    settings = load_settings()
    if not settings.discord_token:
        print("Missing CHAOSX_DISCORD_TOKEN. Copy .env.example to .env and set the bot token.", file=sys.stderr)
        raise SystemExit(2)
    try:
        ChaosXBot(settings).run(settings.discord_token)
    except discord.PrivilegedIntentsRequired:
        # The members intent is privileged: if it is not enabled in the developer portal, Discord refuses the
        # login outright. A bot without member information is far better than a crash loop.
        print(
            "[chaosx] members intent is not enabled in the Discord developer portal; starting without it "
            "(member directory and digests will be empty until it is enabled)",
            file=sys.stderr,
        )
        ChaosXBot(settings.model_copy(update={"members_intent_enabled": False})).run(
            settings.discord_token
        )


if __name__ == "__main__":
    main()
