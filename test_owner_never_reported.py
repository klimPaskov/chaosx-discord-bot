"""The owner is the reader of the bot's reporting, never one of its subjects.

Hoops: "the bot should never report on me. I am the owner."

The weekly digest had been quoting his own questions back at him as "what people wanted" — in the live database
76 of 331 recorded asks are his, and 1943 of 5106 archived messages. So the digest was largely a report on the
operator rather than the community, and the activity figures were inflated by his own posting.

This test runs against the real database: it first proves the owner's data IS there (so the check has something
to catch), then proves collect_intel and live_facts drop every trace of it.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from chaosx_bot.live_facts import _archive_counts  # noqa: E402
from chaosx_bot.server_intel import collect_intel  # noqa: E402


def _env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def main() -> int:
    env = _env(ROOT / ".env")
    owner_id = int(env.get("CHAOSX_OWNER_ID") or 789502982122373150)
    # The live database is the one the bot writes; a repo-local file is a stub. Prefer the configured path and
    # only fall back to a local file when it actually holds data.
    db_path = Path(env.get("CHAOSX_DB_PATH") or "")
    if not db_path.exists():
        db_path = ROOT / "chaosx.db"
    if not db_path.exists() or db_path.stat().st_size == 0:
        print(f"RESULT: FAILED — no usable database at {db_path} (is CHAOSX_DB_PATH right?)")
        return 1
    print(f"owner id resolved: {'yes' if owner_id else 'no'} | db: {db_path.name} ({db_path.stat().st_size // 1048576} MB)")

    raw = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    owner_asks = raw.execute("SELECT COUNT(*) FROM message_ask_memory WHERE actor_id = ?", (owner_id,)).fetchone()[0]
    owner_msgs = raw.execute("SELECT COUNT(*) FROM message_archive WHERE author_id = ?", (owner_id,)).fetchone()[0]
    raw.close()
    print(f"in the raw data: {owner_asks} owner asks, {owner_msgs} owner messages (this is what must not surface)")

    problems: list[str] = []

    # 1. the digest must contain no trace of the owner
    facts = collect_intel(db_path, owner_id=owner_id)
    leaked = [r for r in facts.asks if int(r.get("actor_id") or 0) == owner_id]
    leaked += [r for r in facts.refused if int(r.get("actor_id") or 0) == owner_id]
    leaked += [r for r in facts.shadow if int(r.get("actor_id") or 0) == owner_id]
    leaked += [r for r in facts.warnings if int(r.get("actor_id") or 0) == owner_id]
    if leaked:
        problems.append(f"{len(leaked)} owner row(s) still reach the digest")
    print(f"-> digest asks: {len(facts.asks)} (owner rows: {len(leaked)})")

    # 2. the unfiltered read must still show him — otherwise this test proves nothing
    unfiltered = collect_intel(db_path)
    if owner_asks and not [r for r in unfiltered.asks if int(r.get("actor_id") or 0) == owner_id]:
        problems.append("the unfiltered digest does not contain the owner — the check cannot prove anything")

    # 3. activity figures must be the community's, and smaller without him
    channels, members = _archive_counts(str(db_path), 7, owner_id=owner_id)
    if [m for m in members if int(m[0]) == owner_id]:
        problems.append("the owner is still ranked in the 7d active-member list")
    plain_channels, plain_members = _archive_counts(str(db_path), 7)
    plain_total = sum(n for _, n in plain_channels)
    filtered_total = sum(n for _, n in channels)
    if owner_msgs and filtered_total >= plain_total:
        problems.append(f"owner messages still counted: with={plain_total} without={filtered_total}")
    print(f"-> 7d messages: with owner {plain_total} -> without owner {filtered_total}")
    if members:
        print(f"-> most active member now: {members[0][1]} ({members[0][2]})")

    if problems:
        print("RESULT: FAILED")
        for problem in problems:
            print("  ✗", problem)
        return 1
    print("RESULT: the owner never appears in the digest or the activity figures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
