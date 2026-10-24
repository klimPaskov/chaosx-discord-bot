from __future__ import annotations

import os
import re
import statistics
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .db import connect
from typing import Any, Sequence

_SAFE_TEXT_RE = re.compile(r"[^A-Za-z0-9 _./:+()\-]+")


# Command latency: what the bot spent, per command, so "why is this slow" is answerable from the bot
# itself instead of from guesswork (Hoops 2026-09-23). Kept in memory, newest first, ring-buffered.
_COMMAND_TIMINGS: deque[tuple[str, float, str]] = deque(maxlen=60)

_TIMING_SCHEMA = """
CREATE TABLE IF NOT EXISTS command_timing (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    seconds REAL NOT NULL,
    path TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_ANSWER_SCHEMA = """
CREATE TABLE IF NOT EXISTS answer_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    path TEXT NOT NULL DEFAULT '',
    seconds REAL NOT NULL DEFAULT 0,
    denial INTEGER NOT NULL DEFAULT 0,
    refusal_retry INTEGER NOT NULL DEFAULT 0,
    cache_hit INTEGER NOT NULL DEFAULT 0,
    citations INTEGER NOT NULL DEFAULT 0,
    unverified INTEGER NOT NULL DEFAULT 0
)
"""


def record_answer_event(
    *,
    db_path: Path | None = None,
    path: str = "",
    seconds: float = 0.0,
    denial: bool = False,
    refusal_retry: bool = False,
    cache_hit: bool = False,
    citations: int = 0,
    unverified: int = 0,
) -> None:
    """One row per answer. Never raises: measurement must not cost a member their answer."""
    if db_path is None:
        return
    from .db import retry_write

    retry_write(db_path, _ANSWER_SCHEMA)
    retry_write(
        db_path,
        "INSERT INTO answer_event (path, seconds, denial, refusal_retry, cache_hit, citations, unverified) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            str(path),
            float(seconds),
            int(bool(denial)),
            int(bool(refusal_retry)),
            int(bool(cache_hit)),
            int(citations),
            int(unverified),
        ),
    )


def answer_quality_lines(*, db_path: Path | None = None, days: int = 7) -> list[str]:
    """One line summarising answer health over the last ``days``."""
    if db_path is None:
        return []
    try:
        conn = connect(db_path)
        try:
            conn.execute(_ANSWER_SCHEMA)
            row = conn.execute(
                "SELECT COUNT(*), SUM(denial), SUM(refusal_retry), SUM(cache_hit), SUM(unverified) "
                "FROM answer_event WHERE at >= datetime('now', ?)",
                (f"-{int(days)} days",),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return []
    total, denial, refusal, cache, unverified = row or (0, 0, 0, 0, 0)
    if not total:
        return []
    share = 100.0 * (cache or 0) / total
    return [
        f"answers {days}d: {total} (denial {denial or 0}, refusal-retry {refusal or 0}, "
        f"cache {share:.0f}%, unverified citations {unverified or 0})"
    ]


def record_command_timing(
    name: str, seconds: float, *, path: str = "", db_path: Path | None = None
) -> None:
    """Remember how long one command took and which model path it used.

    Kept in memory for the live panel and, when ``db_path`` is given, appended to the bot database so the numbers
    survive a restart and "is it faster now?" can be answered with data instead of impressions.
    """
    try:
        _COMMAND_TIMINGS.appendleft((str(name), float(seconds), str(path)))
    except Exception:  # instrumentation must never break a command
        pass
    if db_path is None:
        return
    try:
        conn = connect(db_path)
        try:
            conn.execute(_TIMING_SCHEMA)
            conn.execute(
                "INSERT INTO command_timing (name, seconds, path) VALUES (?, ?, ?)",
                (str(name), float(seconds), str(path)),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # a missing directory or locked DB must not break the command
        pass


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    index = int(round((percentile / 100.0) * (len(values) - 1)))
    return values[max(0, min(len(values) - 1, index))]


def timing_stats(
    *, db_path: Path | None = None, name: str | None = None, limit: int = 500
) -> dict[str, float | int]:
    """p50/p95/max for one command (or all) over the last ``limit`` recorded runs."""
    if db_path is None:
        return {"count": 0}
    try:
        conn = connect(db_path)
        try:
            conn.execute(_TIMING_SCHEMA)
            rows = conn.execute(
                "SELECT seconds FROM (SELECT seconds FROM command_timing "
                "WHERE (? IS NULL OR name = ?) ORDER BY id DESC LIMIT ?)",
                (name, name, int(limit)),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return {"count": 0}
    values = sorted(float(row[0]) for row in rows)
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "p50": float(statistics.median(values)),
        "p95": _percentile(values, 95),
        "max": values[-1],
    }


def timing_lines(*, db_path: Path | None = None, limit: int = 5) -> list[str]:
    """Readable per-command latency summary for the owner panel."""
    if db_path is None:
        return ["No command timings recorded yet."]
    try:
        conn = connect(db_path)
        try:
            conn.execute(_TIMING_SCHEMA)
            names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM command_timing GROUP BY name ORDER BY COUNT(*) DESC LIMIT ?",
                    (int(limit),),
                )
            ]
        finally:
            conn.close()
    except Exception:
        return ["No command timings recorded yet."]
    if not names:
        return ["No command timings recorded yet."]
    lines = ["## Command latency"]
    for name in names:
        stats = timing_stats(db_path=db_path, name=name)
        lines.append(
            f"- `{name}`: p50 {stats['p50']:.1f}s, p95 {stats['p95']:.1f}s, "
            f"max {stats['max']:.1f}s ({stats['count']} run{'s' if stats['count'] != 1 else ''})"
        )
    return lines


def command_timings_lines(*, slow_seconds: float = 5.0, limit: int = 5) -> list[str]:
    """A short latency summary: the slowest recent commands and the count over the threshold."""
    if not _COMMAND_TIMINGS:
        return ["No command timings recorded yet."]
    per_command: dict[str, list[float]] = {}
    for name, seconds, _path in _COMMAND_TIMINGS:
        per_command.setdefault(name, []).append(seconds)
    ranked = sorted(per_command.items(), key=lambda item: -max(item[1]))
    lines = []
    for name, values in ranked[:limit]:
        last = values[0]
        worst = max(values)
        lines.append(
            f"- `{name}`: last {last:.1f}s, slowest {worst:.1f}s "
            f"({len(values)} run{'s' if len(values) != 1 else ''})"
        )
    slow = sum(1 for _name, seconds, _path in _COMMAND_TIMINGS if seconds >= slow_seconds)
    lines.append(
        f"-# {len(_COMMAND_TIMINGS)} recent command(s) timed; {slow} took {slow_seconds:.0f}s or more."
    )
    return lines


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int
    name: str
    state: str
    rss_kib: int


@dataclass(frozen=True)
class ProcessTreeSnapshot:
    launcher: ProcessInfo | None
    bot: ProcessInfo
    descendants: tuple[tuple[int, ProcessInfo], ...]


def _safe_text(value: object, *, fallback: str = "unknown", limit: int = 80) -> str:
    cleaned = _SAFE_TEXT_RE.sub("", str(value or "").replace("\n", " ")).strip()
    return (cleaned or fallback)[:limit]


def _read_process(pid: int, proc_root: Path) -> ProcessInfo | None:
    try:
        text = (proc_root / str(pid) / "status").read_text(
            encoding="utf-8", errors="replace"
        )
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return None
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key] = value.strip()
    try:
        ppid = int(fields.get("PPid", "0").split()[0])
    except (ValueError, IndexError):
        ppid = 0
    try:
        rss_kib = int(fields.get("VmRSS", "0 kB").split()[0])
    except (ValueError, IndexError):
        rss_kib = 0
    return ProcessInfo(
        pid=pid,
        ppid=ppid,
        name=_safe_text(fields.get("Name"), fallback="process"),
        state=_safe_text(fields.get("State"), fallback="unknown"),
        rss_kib=max(0, rss_kib),
    )


def collect_process_tree(
    *, root_pid: int | None = None, proc_root: Path = Path("/proc"), limit: int = 64
) -> ProcessTreeSnapshot:
    """Read a safe process-name-only snapshot for ChaosX and its descendants.

    Command lines are intentionally never read because a Hermes process receives
    the full prompt as an argument. Exposing `/proc/<pid>/cmdline` would leak the
    owner's request into Discord diagnostics.
    """

    root_pid = int(root_pid or os.getpid())
    processes: dict[int, ProcessInfo] = {}
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        entries = ()
    for entry in entries:
        if not entry.name.isdigit():
            continue
        process = _read_process(int(entry.name), proc_root)
        if process is not None:
            processes[process.pid] = process
    bot = processes.get(root_pid) or _read_process(root_pid, proc_root)
    if bot is None:
        bot = ProcessInfo(root_pid, os.getppid(), "chaosx-bot", "unknown", 0)
    launcher = processes.get(bot.ppid) or _read_process(bot.ppid, proc_root)
    children: dict[int, list[ProcessInfo]] = {}
    for process in processes.values():
        children.setdefault(process.ppid, []).append(process)
    for items in children.values():
        items.sort(key=lambda item: item.pid)

    descendants: list[tuple[int, ProcessInfo]] = []
    queue = [(1, child) for child in children.get(bot.pid, ())]
    while queue and len(descendants) < max(1, limit):
        depth, process = queue.pop(0)
        descendants.append((depth, process))
        queue[0:0] = [
            (depth + 1, child) for child in children.get(process.pid, ())
        ]
    return ProcessTreeSnapshot(
        launcher=launcher,
        bot=bot,
        descendants=tuple(descendants),
    )


def _memory_text(rss_kib: int) -> str:
    if rss_kib <= 0:
        return "memory unknown"
    return f"{rss_kib / 1024:.1f} MiB"


def _duration_text(seconds: int | float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, remainder = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def format_process_panel(
    snapshot: ProcessTreeSnapshot | Any, activities: Sequence[Any]
) -> str:
    lines = ["## ChaosX processes"]
    bot = snapshot.bot
    lines.append(
        f"- Bot: `{_safe_text(bot.name)}` PID `{bot.pid}` · `{_safe_text(bot.state)}` · `{_memory_text(bot.rss_kib)}`"
    )
    launcher = snapshot.launcher
    if launcher is not None:
        lines.append(
            f"- Launcher: `{_safe_text(launcher.name)}` PID `{launcher.pid}` · `{_safe_text(launcher.state)}`"
        )
    lines.append(f"- Child processes: `{len(snapshot.descendants)}`")
    if snapshot.descendants:
        lines.append("\n### Process tree")
        for depth, process in snapshot.descendants:
            indent = "  " * max(0, int(depth) - 1)
            lines.append(
                f"- {indent}`{_safe_text(process.name)}` PID `{process.pid}` · `{_safe_text(process.state)}` · `{_memory_text(process.rss_kib)}`"
            )

    lines.append("\n### Active model work")
    if not activities:
        lines.append("- No active Hermes reasoning/tool processes.")
    for activity in activities:
        pid = f"PID `{activity.pid}`" if getattr(activity, "pid", None) else "PID pending"
        effort = _safe_text(getattr(activity, "reasoning_effort", ""), fallback="default")
        digest = _safe_text(getattr(activity, "prompt_hash", ""), fallback="unknown")[:12]
        lines.append(
            "- "
            f"`{_safe_text(getattr(activity, 'label', 'Hermes task'))}` · "
            f"`{_safe_text(getattr(activity, 'stage', 'queued'))}` · {pid} · "
            f"`{_safe_text(getattr(activity, 'model', ''), fallback='default model')}` via "
            f"`{_safe_text(getattr(activity, 'provider', ''), fallback='default provider')}` · "
            f"reasoning `{effort}` · `{_duration_text(getattr(activity, 'elapsed_seconds', 0))}` · "
            f"run `{digest}`"
        )
    lines.append(
        "\nProcess names are shown without command-line arguments, prompts, tokens, or secrets."
    )
    return "\n".join(lines)


def format_hermes_progress(command_name: str, activity: Any | None = None) -> str:
    stage = _safe_text(
        getattr(activity, "stage", "preparing context") if activity else "preparing context"
    )
    model = _safe_text(
        getattr(activity, "model", "") if activity else "", fallback="selecting model"
    )
    provider = _safe_text(
        getattr(activity, "provider", "") if activity else "", fallback="pending"
    )
    effort = _safe_text(
        getattr(activity, "reasoning_effort", "") if activity else "",
        fallback="pending",
    )
    pid = getattr(activity, "pid", None) if activity else None
    elapsed = _duration_text(
        getattr(activity, "elapsed_seconds", 0) if activity else 0
    )
    digest = _safe_text(
        getattr(activity, "prompt_hash", "") if activity else "", fallback="pending"
    )[:12]
    process = f"PID `{pid}`" if pid else "starting"
    command = _safe_text(command_name, fallback="admin command")
    return "\n".join(
        [
            "## ChaosX live progress",
            f"- Command: `/{command}`",
            f"- Phase: `{stage}`",
            f"- Model: `{model}` via `{provider}`",
            f"- Reasoning effort: `{effort}`",
            f"- Process: {process}",
            f"- Elapsed: `{elapsed}`",
            f"- Run: `{digest}`",
            "- View: safe execution phases and process state; private chain-of-thought is not exposed.",
        ]
    )
