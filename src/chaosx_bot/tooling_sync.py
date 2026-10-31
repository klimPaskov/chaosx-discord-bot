"""Keep the tool repositories the bot answers from current, and say how current they are.

The interim host never had cron installed, so the 30-minute `git pull` job for the tooling clones never ran and
Agentic HOI4 Modding sat 10 commits behind GitHub (found 2026-10-31). The bot now pulls its own knowledge roots:
every `tooling_sync_interval_minutes` it fetches each root that tracks a branch and fast-forwards it. A root on a
detached HEAD (the MCP checkout is pinned to a release tag on purpose) is reported as pinned and never moved.
The index then picks the change up through the tooling fingerprint (see `Knowledge._refresh_changed_tooling`).
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SyncResult:
    name: str
    path: str
    state: str = "unknown"  # updated | current | pinned | missing | error
    branch: str = ""
    head: str = ""
    commit_date: str = ""
    pulled: int = 0
    error: str = ""
    checked_at: float = field(default_factory=time.time)


def _git(path: Path, *args: str, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, timeout=timeout,
        env={"GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1", "PATH": "/usr/local/bin:/usr/bin:/bin"},
    )


def sync_root(name: str, path: Path, *, fetch: bool = True) -> SyncResult:
    result = SyncResult(name=name, path=str(path))
    if not (Path(path) / ".git").exists():
        result.state = "missing" if not Path(path).exists() else "not a git checkout"
        return result
    try:
        branch = _git(path, "symbolic-ref", "-q", "--short", "HEAD")
        if branch.returncode != 0:
            result.state = "pinned"
            tag = _git(path, "describe", "--tags", "--exact-match")
            result.branch = tag.stdout.strip() or "detached"
        else:
            result.branch = branch.stdout.strip()
            upstream = _git(path, "rev-parse", "--abbrev-ref", "@{u}")
            if upstream.returncode != 0:
                result.state = "no upstream"
            else:
                if fetch:
                    fetched = _git(path, "fetch", "--quiet", "--prune")
                    if fetched.returncode != 0:
                        raise RuntimeError(f"fetch failed: {fetched.stderr.strip()[:200]}")
                behind = _git(path, "rev-list", "--count", "HEAD..@{u}")
                count = int((behind.stdout or "0").strip() or 0)
                if count:
                    merged = _git(path, "merge", "--ff-only", "--quiet", "@{u}")
                    if merged.returncode != 0:
                        raise RuntimeError(f"fast-forward failed: {merged.stderr.strip()[:200]}")
                    result.state, result.pulled = "updated", count
                else:
                    result.state = "current"
        log = _git(path, "log", "-1", "--format=%h %cs")
        if log.returncode == 0 and log.stdout.strip():
            result.head, _, result.commit_date = log.stdout.strip().partition(" ")
    except Exception as exc:  # reported, never raised: a sync failure must not take the bot down
        result.state, result.error = "error", f"{type(exc).__name__}: {exc}"[:300]
    result.checked_at = time.time()
    return result


def sync_all(roots: list[tuple[str, Path]], *, fetch: bool = True) -> list[SyncResult]:
    return [sync_root(name, Path(path), fetch=fetch) for name, path in roots]


def describe(results: list[SyncResult]) -> list[str]:
    lines = []
    for r in results:
        detail = {
            "updated": f"pulled {r.pulled} commit(s)",
            "current": "up to date",
            "pinned": f"pinned at {r.branch}",
        }.get(r.state, r.state)
        when = f" · commit {r.head} ({r.commit_date})" if r.head else ""
        err = f" · {r.error}" if r.error else ""
        lines.append(f"- `{r.name}`: {detail}{when}{err}")
    return lines
