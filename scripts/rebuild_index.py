#!/usr/bin/env python3
from __future__ import annotations

from chaosx_bot.config import load_settings
from chaosx_bot.indexer import rebuild_index


def main() -> None:
    settings = load_settings()
    stats = rebuild_index(settings.chaos_redux_repo, settings.db_path)
    print(f"indexed docs={stats.docs} events={stats.events} clusters={stats.clusters} commit={stats.commit_sha}")


if __name__ == "__main__":
    main()
