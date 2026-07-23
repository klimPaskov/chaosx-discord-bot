from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

_SAFE_TEXT_RE = re.compile(r"[^A-Za-z0-9 _./:+()\-]+")


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
