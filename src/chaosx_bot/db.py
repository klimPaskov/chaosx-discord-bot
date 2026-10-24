"""One way to open the bot's SQLite file.

The database is a busy ~520 MB archive with maintenance writers. Two rules keep writers from losing rows:
WAL (already set on the file, asserted here so a copied database cannot silently regress) and a 30 s busy
timeout on every connection, because sqlite's 5 s default is what produced `database is locked` under the
startup burst.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

BUSY_TIMEOUT_MS = 30_000


def connect(db_path: Path | str, *, timeout: float = 30.0) -> sqlite3.Connection:
    """Open the bot database with the settings every writer needs."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=timeout)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def retry_write(
    db_path: Path | str, statement: str, params: tuple = (), *, attempts: int = 4
) -> bool:
    """Run one write, backing off through a transient lock.

    Returns True when it committed, False on any failure. Never raises: these are sidecar writes (timings,
    cache, telemetry) and a failed one must not cost a member their answer.
    """
    delay = 0.15
    for attempt in range(attempts):
        try:
            conn = connect(db_path)
            try:
                conn.execute(statement, params)
                conn.commit()
            finally:
                conn.close()
            return True
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                return False  # a broken statement will not fix itself by retrying
            if attempt == attempts - 1:
                return False
            time.sleep(delay)
            delay *= 2
        except Exception:
            return False
    return False
