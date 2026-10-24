"""Rebuild the weekly digest without logging the bot in.

A second gateway connection invalidates the live bot's session, so this only constructs the bot object (no
login), collects the same signals the scheduled post uses, and prints the rendered text to stdout.
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "src")

from chaosx_bot.ask_api import direct_chat_completion
from chaosx_bot.bot import ChaosXBot
from chaosx_bot.config import load_settings
from chaosx_bot.routine_posts import build_digest_prompt, digest_fallback, last_complete_week, strip_online_count, with_window_note


async def main() -> int:
    settings = load_settings()
    bot = ChaosXBot(settings)
    signals = await bot._collect_digest_signals()
    commits = signals.get("commits") or {}
    print(f"[signals] commits={commits.get('count')} latest={commits.get('latest', '')[:70]!r}", file=sys.stderr)
    print(f"[signals] areas={[a.get('area') for a in (signals.get('change_areas') or [])][:5]}", file=sys.stderr)
    prompt = build_digest_prompt(signals=signals)
    try:
        text = await direct_chat_completion(
            system="You are ChaosX, the Chaos Redux community bot.",
            user=prompt,
            model=settings.ask_model,
            provider=settings.ask_provider,
            reasoning_effort=settings.ask_reasoning_effort,
            timeout_s=120,
        )
    except Exception as exc:
        print(f"[model] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        text = digest_fallback(signals)
    text = strip_online_count(text)
    start, end = last_complete_week()
    text = with_window_note(text, window_start=start, window_end=end)
    print(text)
    return 0


raise SystemExit(asyncio.run(main()))
