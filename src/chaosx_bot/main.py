from __future__ import annotations

import sys

from .bot import ChaosXBot
from .config import load_settings


def main() -> None:
    settings = load_settings()
    if not settings.discord_token:
        print("Missing CHAOSX_DISCORD_TOKEN. Copy .env.example to .env and set the bot token.", file=sys.stderr)
        raise SystemExit(2)
    bot = ChaosXBot(settings)
    bot.run(settings.discord_token)


if __name__ == "__main__":
    main()
