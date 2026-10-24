#!/usr/bin/env python3
"""Full knowledge-index rebuild: mod repo + Obsidian vault + Qoder repowiki.

Passes every root the bot itself indexes. Calling this with the repo alone
(which this script used to do) silently drops all vault and Qoder documents
from the index, so keep the roots in sync with ChaosXBot's Knowledge
construction and ChaosXBot.knowledge.
"""
from __future__ import annotations

from chaosx_bot.config import load_settings
from chaosx_bot.indexer import parse_extra_roots, rebuild_index


def main() -> None:
    settings = load_settings()
    visual_repo = settings.focus_tree_repo or settings.chaos_redux_repo
    extra_roots = parse_extra_roots(settings.extra_knowledge_roots)
    stats = rebuild_index(
        settings.chaos_redux_repo,
        settings.db_path,
        settings.obsidian_vault_path,
        catalog_repo=visual_repo,
        qoder_path=settings.qoder_repowiki_path,
        extra_roots=extra_roots,
    )
    print(
        f"indexed docs={stats.docs} events={stats.events} scenarios={stats.scenarios} "
        f"clusters={stats.clusters} tooling_roots={len(extra_roots)} commit={stats.commit_sha}"
    )


if __name__ == "__main__":
    main()
