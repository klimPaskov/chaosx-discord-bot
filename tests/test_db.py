"""Every sidecar writer opens the same kind of connection, with the same lock tolerance."""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chaosx_bot.db import connect, retry_write


def test_connect_sets_wal_and_busy_timeout(tmp_path):
    db = connect(tmp_path / "t.db")
    try:
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] >= 30000
    finally:
        db.close()


def test_connect_creates_the_parent_directory(tmp_path):
    db = connect(tmp_path / "nested" / "dir" / "t.db")
    try:
        assert db.execute("SELECT 1").fetchone()[0] == 1
    finally:
        db.close()


def test_retry_write_survives_a_short_lock(tmp_path):
    path = tmp_path / "t.db"
    connect(path).close()  # create the file in WAL mode
    holder = sqlite3.connect(str(path), timeout=0.1, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")  # take the write lock

    def release():
        time.sleep(0.3)
        holder.execute("ROLLBACK")
        holder.close()

    threading.Thread(target=release, daemon=True).start()
    wrote = retry_write(path, "CREATE TABLE IF NOT EXISTS x (id INTEGER)")
    assert wrote is True
    conn = connect(path)
    try:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='x'"
        ).fetchone() == ("x",)
    finally:
        conn.close()


def test_retry_write_reports_a_real_failure_instead_of_raising(tmp_path):
    # An invalid statement is not a lock; it must surface as False, never as an exception.
    assert retry_write(tmp_path / "t.db", "NOT VALID SQL") is False
