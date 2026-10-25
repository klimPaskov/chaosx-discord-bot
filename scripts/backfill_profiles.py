#!/usr/bin/env python3
"""Refresh member memory for anyone whose messages have moved on since their last compaction.

Hoops, 2026-10-25: the bot should "occasionally run auto like scans on new messages to save more info on
users". Compaction already fires when a member posts ten new messages, but a member who drifts away, or whose
posts arrive in bursts across channels the bot sees late, can sit stale for weeks. This script finds everyone
with unseen public messages and rebuilds their profile, oldest-stale first, and is safe to run from cron.

Usage:
    .venv/bin/python scripts/backfill_profiles.py                 # everyone due, capped
    .venv/bin/python scripts/backfill_profiles.py --limit 5 --min-new 3
    .venv/bin/python scripts/backfill_profiles.py --force --user 789502982122373150
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chaosx_bot.config import load_settings  # noqa: E402
from chaosx_bot.conversation_memory import run_user_profile_compaction_if_due  # noqa: E402


def candidates(db_path: Path, *, min_new: int, limit: int) -> list[tuple[int, str, int]]:
    """Members with at least `min_new` public messages past their stored checkpoint, stalest first."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT ma.author_id AS author_id,
                   MAX(ma.author_name) AS author_name,
                   COUNT(*) AS new_messages,
                   COALESCE(MAX(up.updated_at), '') AS updated_at
            FROM message_archive ma
            LEFT JOIN user_profiles up ON up.author_id = ma.author_id
            WHERE ma.visibility = 'public' AND ma.author_name != 'ChaosX'
              AND ma.id > COALESCE(up.last_archive_id, 0)
            GROUP BY ma.author_id
            HAVING COUNT(*) >= ?
            ORDER BY updated_at ASC, new_messages DESC
            LIMIT ?
            """,
            (max(1, min_new), max(1, limit)),
        ).fetchall()
        return [(int(row["author_id"]), str(row["author_name"] or ""), int(row["new_messages"])) for row in rows]
    finally:
        conn.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=8, help="how many members to refresh per run")
    parser.add_argument("--min-new", type=int, default=5, help="how many unseen messages make someone due")
    parser.add_argument("--user", type=int, default=0, help="refresh one member id only")
    parser.add_argument("--force", action="store_true", help="refresh even without new messages")
    parser.add_argument("--dry-run", action="store_true", help="only list who would be refreshed")
    args = parser.parse_args()

    settings = load_settings()
    if args.user:
        targets = [(args.user, "", 0)]
    else:
        targets = candidates(settings.db_path, min_new=1 if args.force else args.min_new, limit=args.limit)
    if not targets:
        print("nothing due")
        return 0
    print(f"{len(targets)} member(s) due")
    for author_id, name, new_messages in targets:
        print(f"  {name or author_id} (id {author_id}) — {new_messages} unseen message(s)")
        if args.dry_run:
            continue
        try:
            refreshed = await run_user_profile_compaction_if_due(settings, author_id, force=True)
        except Exception as exc:  # one member must never stop the batch
            print(f"    failed: {type(exc).__name__}: {exc}")
            continue
        print(f"    {'refreshed' if refreshed else 'skipped (no usable history)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
