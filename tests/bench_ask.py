"""End-to-end benchmark for public answers: retrieval + one model call per question.

Run it before and after a change. It reports p50/p95 latency and every answer that claimed the bot cannot access
something it can, which is the failure mode this work exists to remove.

    .venv/bin/python tests/bench_ask.py --n 20 --out /tmp/baseline.json
    .venv/bin/python tests/bench_ask.py --n 40 --assert-no-denials

Needs model credentials in the environment (the bot's Hermes profile .env or the process environment).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chaosx_bot.ask_api import direct_chat_completion
from chaosx_bot.config import load_settings
from chaosx_bot.denial_guard import is_denial
from chaosx_bot.hermes_bridge import PUBLIC_ASK_BOUNDARY
from chaosx_bot.knowledge import Knowledge

# Real member questions, kept stable so runs are comparable. Mix of knowledge, catalogue, status and meta.
QUESTIONS = [
    "how does the chaos meter work",
    "explain the zombie outbreak event chain",
    "where is the super event text defined",
    "what are the six faction packages",
    "how do I install the mod",
    "how do events evolve if you ignore them",
    "what is the chaos level cap",
    "explain the holy realm event chain",
    "where are the AI strategy profiles defined",
    "how do I add a new event",
    "what is the current status on things",
    "what is the latest commit about",
    "how many events are in the mod",
    "explain the migration and camps system",
    "what does the biological warfare system do",
    "how do focus trees interact with the chaos meter",
    "what is the world-end scenario",
    "how do I report a bug",
    "what is the mod's release date",
    "can you summarise the project for a new contributor",
]


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round((pct / 100.0) * (len(ordered) - 1)))
    return ordered[max(0, min(len(ordered) - 1, index))]


async def run(n: int) -> dict:
    settings = load_settings()
    knowledge = Knowledge(
        settings.chaos_redux_repo,
        settings.db_path,
        settings.obsidian_vault_path,
        settings.qoder_repowiki_path,
        catalog_repo=settings.focus_tree_repo or settings.chaos_redux_repo,
        autorebuild=settings.index_autorebuild,
    )
    timings: list[float] = []
    denials: list[dict] = []
    for question in QUESTIONS[:n]:
        started = time.perf_counter()
        context = await asyncio.to_thread(knowledge.public_ask_context, question)
        try:
            answer = await direct_chat_completion(
                system=PUBLIC_ASK_BOUNDARY,
                user=f"QUESTION: {question}\n\nCONTEXT:\n{context}",
                model=settings.ask_model,
                provider=settings.ask_provider,
            )
        except Exception as exc:  # a failed call is a result too
            answer = f"__error__: {type(exc).__name__}: {exc}"
        elapsed = time.perf_counter() - started
        timings.append(elapsed)
        if is_denial(answer):
            denials.append({"question": question, "answer": answer[:240]})
        print(f"  {elapsed:5.1f}s  {question[:56]}")
    return {
        "count": len(timings),
        "p50": round(statistics.median(timings), 2) if timings else 0.0,
        "p95": round(_percentile(timings, 95), 2),
        "denials": denials,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--assert-no-denials", action="store_true")
    args = parser.parse_args()

    result = asyncio.run(run(args.n))
    print(json.dumps(result, indent=2))
    if args.out:
        args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if args.assert_no_denials and result["denials"]:
        print(f"FAIL: {len(result['denials'])} capability denial(s)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
