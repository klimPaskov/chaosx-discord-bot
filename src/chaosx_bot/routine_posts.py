"""Autonomous routine posts: weekly dev digest and release announcements.

These are the first ChaosX posts that go out on their own, so the module is built
around a few hard rules:

- **Kill switch per post type.** Each post type is a named automation entry, so
  ``/admin automation disable routine_dev_digest`` stops it immediately.
- **One post per period.** The ``routine_posts`` table stores a period key per post
  type; restarts, ticks and catch-up runs cannot double-post.
- **Catch-up, not drop.** A weekly post whose slot passed while the bot was down
  fires on the next tick, but never twice in the same ISO week.
- **No pings, ever.** Builders emit plain text and every post is sanitized: model
  output cannot smuggle an ``@everyone``/``@here``/role mention into a channel.
- **Real signals only.** Git history of the live mod checkout, ``descriptor.mod``
  version, ``gh`` issue/release data, and counts from the bot's own DB. If a signal
  is unavailable it is reported as unavailable, never invented.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------------------------
# Post types
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutinePostSpec:
    """One autonomous post type."""

    name: str  # automation name; also the /admin automation handle
    label: str
    description: str
    kind: str  # "weekly" | "release"
    weekday: int = 0  # 0 = Monday (for weekly posts)
    hour_utc: int = 12  # slot hour in UTC
    interval_hours: int = 6  # how often to look for new signal (release posts)
    channel_setting: str = "routine_posts_channel_id"
    delivery: str = "channel"  # "channel" | "owner_dm"


DEV_DIGEST = RoutinePostSpec(
    name="routine_dev_digest",
    label="Weekly dev digest",
    description=(
        "Weekly dev/community digest built from the live mod repo (commits, catalog changes, "
        "version), GitHub issue activity, and server Q&A stats. Posts once a week."
    ),
    kind="weekly",
    weekday=0,
    hour_utc=12,
)

RELEASE_POSTS = RoutinePostSpec(
    name="routine_release_posts",
    label="Release announcements",
    description=(
        "Posts a short announcement when the mod version changes in descriptor.mod (or a GitHub "
        "release/tag appears). First run records the current version as the baseline and stays quiet."
    ),
    kind="release",
    interval_hours=6,
    channel_setting="routine_release_channel_id",
)

SERVER_INTEL = RoutinePostSpec(
    name="routine_server_intel",
    label="Weekly server intel digest",
    description=(
        "Private weekly briefing to Hoops: what people asked, where ChaosX could not help, "
        "activity, moderation, and his own admin actions. Delivered by DM."
    ),
    kind="weekly",
    weekday=6,  # Sunday
    hour_utc=18,
    channel_setting="owner_dm",
    delivery="owner_dm",
)

ROUTINE_POSTS: tuple[RoutinePostSpec, ...] = (DEV_DIGEST, RELEASE_POSTS, SERVER_INTEL)

DIGEST_WINDOW_DAYS = 7
MAX_NOTABLE_COMMITS = 12
MAX_ISSUE_TITLES = 8
MAX_POST_CHARS = 1800


# --------------------------------------------------------------------------------------
# Time / scheduling helpers (pure)
# --------------------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def weekly_period_key(now: datetime) -> str:
    iso = now.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def weekly_slot(spec: RoutinePostSpec, now: datetime) -> datetime:
    """The scheduled slot for ``now``'s ISO week (always <= 7 days in the past)."""
    start_of_week = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return start_of_week + timedelta(days=spec.weekday, hours=spec.hour_utc)


def is_weekly_due(spec: RoutinePostSpec, *, now: datetime, last_period_key: str | None) -> bool:
    """Due when this week's slot has passed and this week has no recorded post."""
    if weekly_period_key(now) == (last_period_key or ""):
        return False
    return now >= weekly_slot(spec, now)


def is_interval_due(spec: RoutinePostSpec, *, now: datetime, checked_at: str | None) -> bool:
    last = parse_iso(checked_at)
    if last is None:
        return True
    return now - last >= timedelta(hours=max(1, spec.interval_hours))


def plan_due_posts(
    specs: Iterable[RoutinePostSpec],
    *,
    now: datetime,
    states: dict[str, dict[str, Any]],
    enabled: dict[str, bool],
) -> list[RoutinePostSpec]:
    """Pure decision: which post types should run on this tick.

    ``states`` maps post name -> stored state row (period_key/checked_at/status/detail).
    Release posts are *always* returned when their poll interval is due; the caller
    then decides whether the version actually changed.
    """
    due: list[RoutinePostSpec] = []
    for spec in specs:
        if not enabled.get(spec.name, False):
            continue
        state = states.get(spec.name) or {}
        if spec.kind == "weekly":
            if is_weekly_due(spec, now=now, last_period_key=str(state.get("period_key") or "")):
                due.append(spec)
        else:
            if is_interval_due(spec, now=now, checked_at=str(state.get("checked_at") or "")):
                due.append(spec)
    return due


# --------------------------------------------------------------------------------------
# Text hygiene
# --------------------------------------------------------------------------------------

_MENTION_RE = re.compile(r"<@[!&]?\d+>|@everyone|@here", re.IGNORECASE)


def sanitize_post(text: str, *, max_chars: int = MAX_POST_CHARS) -> str:
    """Strip pings from generated text and cap length (hooked to a line boundary)."""
    cleaned = _MENTION_RE.sub("", text or "").strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if len(cleaned) <= max_chars:
        return cleaned
    cut = cleaned[: max(0, max_chars - 1)]
    if "\n" in cut[-120:]:
        cut = cut[: cut.rfind("\n")]
    return cut.rstrip() + "…"


# --------------------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------------------


async def _run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 90) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", f"timeout after {timeout}s"
    return proc.returncode or 0, out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")


async def git_commit_summary(repo: Path, *, since_days: int = DIGEST_WINDOW_DAYS) -> dict[str, Any]:
    """Commit activity in the live mod checkout (real history, not a cache)."""
    code, out, err = await _run(
        ["git", "log", f"--since={since_days} days ago", "--pretty=%h|%ad|%an|%s", "--date=short"],
        cwd=repo,
    )
    if code != 0:
        return {"available": False, "count": 0, "subjects": [], "authors": [], "error": (err or out)[:200]}
    rows = [line for line in out.splitlines() if line.strip()]
    subjects = [row.split("|", 3)[3].strip() for row in rows if row.count("|") >= 3]
    authors = sorted({row.split("|", 3)[2].strip() for row in rows if row.count("|") >= 3})
    notable = [s for s in subjects if s and s.lower() not in {"update", "fix", "wip", "cleanup"}]
    return {
        "available": True,
        "count": len(rows),
        "subjects": subjects,
        "notable": (notable or subjects)[:MAX_NOTABLE_COMMITS],
        "authors": authors,
        "latest": subjects[0] if subjects else "",
    }


async def git_files_touched(repo: Path, *, since_days: int = DIGEST_WINDOW_DAYS, prefix: str = "") -> int:
    args = ["git", "log", f"--since={since_days} days ago", "--name-only", "--pretty=format:"]
    if prefix:
        args += ["--", prefix]
    code, out, _ = await _run(args, cwd=repo)
    if code != 0:
        return 0
    return len({line.strip() for line in out.splitlines() if line.strip()})


async def git_commits_between(repo: Path, *, old_sha: str, new_sha: str = "HEAD") -> list[str]:
    if not old_sha:
        return []
    code, out, _ = await _run(["git", "log", f"{old_sha}..{new_sha}", "--pretty=%s", "--no-merges"], cwd=repo)
    if code != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


async def git_head_sha(repo: Path) -> str:
    code, out, _ = await _run(["git", "rev-parse", "--short", "HEAD"], cwd=repo)
    return out.strip() if code == 0 else ""


async def descriptor_version(repo: Path) -> str:
    """Mod version from descriptor.mod (the real release signal for this project)."""
    path = repo / "descriptor.mod"
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = re.search(r'^\s*version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1).strip() if match else ""


async def github_issue_activity(repo_slug: str, *, since_days: int = DIGEST_WINDOW_DAYS) -> dict[str, Any]:
    """Issues touched in the window, via the authenticated gh CLI (same path as /issue)."""
    since = (utcnow() - timedelta(days=since_days)).strftime("%Y-%m-%d")
    code, out, err = await _run(
        [
            "gh", "issue", "list", "--repo", repo_slug, "--state", "all",
            "--search", f"updated:>={since}", "--limit", str(MAX_ISSUE_TITLES * 3),
            "--json", "number,title,state,updatedAt",
        ]
    )
    if code != 0:
        return {"available": False, "opened": [], "closed": [], "error": (err or out)[:200]}
    try:
        items = json.loads(out or "[]")
    except json.JSONDecodeError:
        return {"available": False, "opened": [], "closed": [], "error": "unparseable gh output"}
    opened = [f"#{i['number']} {i['title']}" for i in items if str(i.get("state", "")).upper() == "OPEN"]
    closed = [f"#{i['number']} {i['title']}" for i in items if str(i.get("state", "")).upper() == "CLOSED"]
    return {
        "available": True,
        "opened": opened[:MAX_ISSUE_TITLES],
        "closed": closed[:MAX_ISSUE_TITLES],
        "opened_count": len(opened),
        "closed_count": len(closed),
    }


async def github_latest_release(repo_slug: str) -> dict[str, Any] | None:
    """Latest published GitHub release, or None when the repo has none (it currently has none)."""
    code, out, _ = await _run(["gh", "api", f"repos/{repo_slug}/releases?per_page=1"])
    if code != 0:
        return None
    try:
        items = json.loads(out or "[]")
    except json.JSONDecodeError:
        return None
    if not items:
        return None
    release = items[0]
    return {
        "tag": str(release.get("tag_name") or ""),
        "name": str(release.get("name") or ""),
        "url": str(release.get("html_url") or ""),
        "published_at": str(release.get("published_at") or ""),
        "body": str(release.get("body") or "")[:1200],
    }


# --------------------------------------------------------------------------------------
# Prompt + fallback builders (pure)
# --------------------------------------------------------------------------------------


def _bullet(lines: Iterable[str], limit: int = MAX_NOTABLE_COMMITS) -> str:
    out = [f"- {line}" for line in list(lines)[:limit] if line]
    return "\n".join(out) if out else "- (none)"


def server_facts_line(server: dict[str, Any]) -> str:
    """Server-side facts, skipping zeros so a quiet week does not read as a wall of 0s."""
    parts: list[str] = []
    if server.get("answers"):
        parts.append(f"{server['answers']} questions auto-answered")
    if server.get("qa_saved"):
        parts.append(f"{server['qa_saved']} stored asks")
    if server.get("warnings"):
        parts.append(f"{server['warnings']} soft moderation warnings")
    if server.get("playtests"):
        parts.append(f"{server['playtests']} playtests logged")
    if server.get("members"):
        parts.append(f"{server['members']} members in the server")
    return ", ".join(parts) if parts else "no tracked server activity in this window"


def issues_facts_line(issues: dict[str, Any]) -> str:
    if not issues.get("available"):
        return "GitHub issue data unavailable for this window"
    opened = issues.get("opened") or []
    closed = issues.get("closed") or []
    if not opened and not closed:
        return "no GitHub issue activity"
    parts: list[str] = []
    if opened:
        parts.append(f"{len(opened)} opened/touched ({', '.join(opened)})")
    if closed:
        parts.append(f"{len(closed)} closed ({', '.join(closed)})")
    return "; ".join(parts)


def build_digest_prompt(*, signals: dict[str, Any], max_chars: int = MAX_POST_CHARS) -> str:
    commits = signals.get("commits") or {}
    issues = signals.get("issues") or {}
    server = signals.get("server") or {}
    return f"""Write the weekly Chaos Redux dev digest for the community.

Facts (use only these, invent nothing, no pings/mentions):
- Mod version: {signals.get('version') or 'unknown'}
- Repo HEAD: {signals.get('head') or 'unknown'}
- Commits in the last {signals.get('window_days', DIGEST_WINDOW_DAYS)} days: {commits.get('count', 0)}
- Event/catalog files touched: {signals.get('event_files', 0)}
- Notable commit subjects:
{_bullet(commits.get('notable') or [])}
- GitHub issues: {issues_facts_line(issues)}
- Server activity: {server_facts_line(server)}

Write it as a forum-style post:
1. A short title line starting with "**Weekly dev digest**" and the version.
2. "What moved" — 3-6 bullets taken from the facts above (group related commits, no file paths).
3. "Community" — issues/answers in one or two short lines.
4. "What's next" — only if the facts imply it; otherwise say testing focus stays open.

Keep it under {max_chars} characters, plain Discord markdown, no code blocks, no pings,
no invented features, versions, dates or dates of future releases. Never claim something shipped
unless the commit/issue facts above say so. If a fact line says there was no activity, say the rest
of the week was quiet (in one short clause) instead of printing zeros or empty bullets."""


def digest_fallback(signals: dict[str, Any]) -> str:
    commits = signals.get("commits") or {}
    issues = signals.get("issues") or {}
    server = signals.get("server") or {}
    return sanitize_post(
        "\n".join(
            [
                f"**Weekly dev digest** — Chaos Redux {signals.get('version') or 'in development'}",
                "",
                f"**What moved** ({commits.get('count', 0)} commits in {signals.get('window_days', DIGEST_WINDOW_DAYS)} days, "
                f"{signals.get('event_files', 0)} event/catalog files touched)",
                _bullet(commits.get("notable") or []),
                "",
                "**Community**",
                f"- Issues: {issues_facts_line(issues)}",
                f"- Server: {server_facts_line(server)}",
                "",
                f"Repo HEAD `{signals.get('head') or 'unknown'}` — testing focus stays open.",
            ]
        )
    )


def build_release_prompt(*, signals: dict[str, Any], max_chars: int = MAX_POST_CHARS) -> str:
    return f"""Write a short Chaos Redux release announcement for the community.

Facts (use only these, invent nothing, no pings/mentions):
- New version: {signals.get('version') or 'unknown'} (previous: {signals.get('previous_version') or 'not recorded'})
- Repo HEAD: {signals.get('head') or 'unknown'}
- GitHub release: {signals.get('release_tag') or 'none published'}
- Commits since the previous version ({signals.get('commit_count', 0)}):
{_bullet(signals.get('commits') or [])}

Write:
1. A title line: "**Chaos Redux {signals.get('version')}**".
2. Two to five bullets of what changed, grouped from the commit subjects (no file paths, no invented features).
3. One line telling testers what to focus on, plus that bug reports are welcome in the issues channel.

Keep it under {max_chars} characters, plain Discord markdown, no pings, no promises about dates."""


def release_fallback(signals: dict[str, Any]) -> str:
    return sanitize_post(
        "\n".join(
            [
                f"**Chaos Redux {signals.get('version') or 'update'}**",
                "",
                _bullet(signals.get("commits") or []),
                "",
                "Bug reports and test notes are welcome in the issues channel.",
            ]
        )
    )


def release_signal_changed(*, state: dict[str, Any], version: str, tag: str) -> bool:
    """True when the version or release tag differs from what was last recorded."""
    if not version and not tag:
        return False
    detail = state.get("detail")
    if not detail:
        # First observation: record the baseline, do not announce history.
        return False
    try:
        previous = json.loads(str(detail))
    except (json.JSONDecodeError, TypeError):
        return False
    previous_version = str(previous.get("version") or "")
    previous_tag = str(previous.get("tag") or "")
    version_changed = bool(version) and version != previous_version
    tag_changed = bool(tag) and tag != previous_tag
    return version_changed or tag_changed


def release_state_detail(*, version: str, tag: str, head: str, commits: list[str]) -> str:
    return json.dumps(
        {"version": version, "tag": tag, "head": head, "commits": commits[:MAX_NOTABLE_COMMITS]},
        ensure_ascii=False,
    )


@dataclass
class RoutinePostResult:
    name: str
    action: str  # "posted" | "skipped" | "disabled" | "error" | "baseline"
    detail: str = ""
    channel_id: int | None = None
    message_id: int | None = None
    text: str = ""
    facts: dict[str, Any] = field(default_factory=dict)
