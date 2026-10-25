#!/usr/bin/env python3
"""Retrieval quality check: 30 questions, each with the kind of document that must show up in the top results.

Today's index regression (the Mod Setup app was indexed for its manifest but not its source) was invisible because
nothing measured retrieval. Run this after any index change: `python scripts/eval_retrieval.py`. A drop in hit rate
means the index lost a source, a scope, or a chunking rule — not that the model got worse.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chaosx_bot.config import load_settings  # noqa: E402
from chaosx_bot.knowledge import Knowledge  # noqa: E402

# (question a member would ask, substring the top result path must contain)
CASES: list[tuple[str, str]] = [
    ("how does testing work for the mod", "test"),
    ("what is a super event", "event"),
    ("how do I set up hoi4 mod setup", "mod-setup"),
    ("codex is not detected on macos", "mod-setup"),
    ("how do focus trees get built", "focus"),
    ("what scenarios exist", "scenario"),
    ("how do I report a bug", "issue"),
    ("what is the agent toolkit", "hoi4-agent-tools"),
    ("how does the live tools mcp work", "live-tools"),
    ("what does agends.md contain", "agentic-modding"),
    ("what is a national focus limit", "focus"),
    ("who is hoops mccann", "vault"),
    ("how do events get triggered", "event"),
    ("what is the mod's release status", "readme"),
    ("how do I install the tooling", "tooling"),
    ("what is mcp", "tooling"),
    ("how are portraits generated", "comfyui"),
    ("what is the modding roadmap", "roadmap"),
    ("how does country selection work", "country"),
    ("what are the community rules", "rules"),
    ("how do I write an event idea", "event"),
    ("what is the acceptance criteria", "acceptance"),
    ("how do I use openrouter with the setup app", "mod-setup"),
    ("what changed in the changelog", "changelog"),
    ("how are assets imported into the mod", "asset"),
    ("what is the mod's tech stack", "cargo"),
    ("how does the app sign in with chatgpt", "mod-setup"),
    ("where do devlogs get posted", "vault"),
    ("how does moderation work", "rule"),
    ("what is the vault schema", "schema"),
]


def path_of(line: str) -> str:
    """Pull the doc path out of a formatted result line like '1. `tooling/x/y.md`'."""
    if "`" in line:
        parts = line.split("`")
        if len(parts) >= 3:
            return parts[1]
    return ""


def main() -> int:
    settings = load_settings()
    knowledge = Knowledge(
        repo=settings.chaos_redux_repo,
        db_path=settings.db_path,
        vault_path=settings.obsidian_vault_path,
        qoder_path=settings.qoder_repowiki_path,
        catalog_repo=settings.focus_tree_repo or settings.chaos_redux_repo,
    )
    hits_at_1 = 0
    hits_at_3 = 0
    misses: list[tuple[str, str, list[str]]] = []
    for question, expect in CASES:
        text = knowledge.search(question, limit=3)
        paths = [path_of(line) for line in text.splitlines() if path_of(line) and not line.startswith("##")]
        top1 = paths[0] if paths else ""
        top3 = paths[:3]
        if expect.lower() in top1.lower():
            hits_at_1 += 1
        if any(expect.lower() in path.lower() for path in top3):
            hits_at_3 += 1
        else:
            misses.append((question, expect, top3))
    total = len(CASES)
    print(f"hit@1 {hits_at_1}/{total} ({hits_at_1 / total:.0%}) | hit@3 {hits_at_3}/{total} ({hits_at_3 / total:.0%})")
    for question, expect, top3 in misses:
        print(f"  MISS  {question!r} expected *{expect}* got {top3}")
    return 0 if hits_at_3 / total >= 0.8 else 1


if __name__ == "__main__":
    raise SystemExit(main())
