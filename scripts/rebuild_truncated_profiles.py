"""One-shot: rebuild profiles that hit the 4000-char hard slice (mid-word
truncation) plus the restricted-author profile, so they end cleanly and are
sanitized. Runs with the bot online — profile builds use the public model
bridge and only write their own rows (sqlite WAL-safe)."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

from chaosx_bot.config import load_settings
from chaosx_bot.conversation_memory import run_user_profile_compaction_if_due

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main() -> None:
    settings = load_settings()
    db = Path(settings.db_path)
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT author_id, LENGTH(profile) FROM user_profiles WHERE LENGTH(profile) >= 3995 ORDER BY author_id"
    ).fetchall()
    con.close()
    ids = [int(r[0]) for r in rows]
    print(f"rebuilding {len(ids)} profiles: {ids}", flush=True)
    done = 0
    for author_id in ids:
        try:
            if await run_user_profile_compaction_if_due(settings, author_id, force=True):
                done += 1
                print(f"  rebuilt {author_id}", flush=True)
            else:
                print(f"  skipped/no-op {author_id}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  failed {author_id}: {type(exc).__name__}: {exc}", flush=True)
    print(f"done: {done}/{len(ids)}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
