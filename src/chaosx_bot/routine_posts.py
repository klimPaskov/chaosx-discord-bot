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
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .formatting import fix_duplicate_emoji

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
MAX_PLAYTEST_NOTES = 4
MAX_ISSUE_TITLES = 8
MAX_POST_CHARS = 1800
# The digest is a community post, not a report: Hoops asked for a shorter one (2026-09-23).
DIGEST_MAX_CHARS = 900
# Directories that hold internal working material (audits, plans, agent tooling). Work there is real
# but it is not player-facing, so the digest neither counts nor describes it.
_INTERNAL_PREFIXES = ("docs/", ".agents/", ".qoder/", ".github/", ".vscode/", "tools/", "mod/")
# Changed-file paths -> the thing a player would recognise. First match wins.
_AREA_RULES: tuple[tuple[str, str], ...] = (
    ("gfx/interface/goals", "focus tree icons"),
    ("gfx/interface/decisions", "decision icons"),
    ("gfx/interface/formables", "formable nation art"),
    ("gfx/interface/ideas", "idea icons"),
    ("gfx/interface/technologies", "technology icons"),
    ("gfx/interface/counters", "counter art"),
    ("gfx/interface/chaos_meter", "the chaos meter"),
    ("gfx/interface/camp_repression", "camp repression art"),
    ("gfx/interface", "interface art"),
    ("gfx/flags", "flags"),
    ("gfx/achievements", "achievements"),
    ("gfx/models", "3D models"),
    ("gfx/entities", "3D models"),
    ("gfx/particles", "effects and particles"),
    ("gfx/event_pictures", "event pictures"),
    ("gfx/leaders", "leader portraits"),
    ("gfx/texticons", "text icons"),
    ("gfx/super_events", "super-event art"),
    ("gfx/loadingscreens", "loading screens"),
    ("gfx", "other art"),
    ("events/", "event content"),
    ("common/decisions/", "decisions"),
    ("common/focus_trees/", "focus trees"),
    ("common/national_focus/", "focus trees"),
    ("common/scripted_effects/", "game scripting"),
    ("common/scripted_triggers/", "game scripting"),
    ("common/script_constants/", "game scripting"),
    ("common/on_actions/", "game scripting"),
    ("common/scripted_localisation/", "game scripting"),
    ("common/ideas/", "national ideas"),
    ("common/dynamic_modifiers/", "national ideas"),
    ("common/characters/", "characters"),
    ("common/countries/", "countries"),
    ("common/units/", "units"),
    ("common/abilities/", "units"),
    ("common", "game rules"),
    ("localisation/", "text and tooltips"),
    ("history/", "starting setup"),
    ("sound/", "sound"),
    ("music/", "music"),
    ("map/", "map"),
    ("interface/", "interface files"),
    ("descriptor.mod", "mod metadata"),
)
# Commit subjects that describe internal record-keeping rather than a change to the mod itself.
_BOOKKEEPING_RE = re.compile(
    r"^(docs(\(|:)|doc |record |document |align |clarify |reconcile |guard |gate |link |keep |promote |"
    r"preserve |refresh |crosswalk |accept |audit |research |describe |rename |point the |fold the |"
    r"scope the |merge the |update$|wip$|cleanup$|typo)",
    re.IGNORECASE,
)


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
    # A heading emoji must not be repeated by the bullet right under it.
    cleaned = fix_duplicate_emoji(cleaned)
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
    notable = [s for s in subjects if s and not _BOOKKEEPING_RE.match(s)]
    return {
        "available": True,
        "count": len(rows),
        "subjects": subjects,
        "notable": (notable or subjects)[:MAX_NOTABLE_COMMITS],
        "authors": authors,
        "latest": subjects[0] if subjects else "",
    }


def area_label(path: str) -> str | None:
    """Player-facing area for a changed file, or None when the path is internal working material."""
    cleaned = (path or "").strip().lstrip("/")
    if not cleaned or cleaned.startswith(_INTERNAL_PREFIXES):
        return None
    for prefix, label in _AREA_RULES:
        if cleaned.startswith(prefix):
            return label
    return "other content"


def size_band(files: int) -> str:
    """Plain-language weight of an area, so the model can order emphasis without quoting statistics."""
    if files >= 1000:
        return "took most of the week's work"
    if files >= 250:
        return "took a large share of the week"
    if files >= 50:
        return "saw solid work"
    if files >= 10:
        return "saw a few changes"
    return "was touched lightly"


async def git_change_areas(
    repo: Path, *, since_days: int = DIGEST_WINDOW_DAYS, limit: int = 8
) -> list[dict[str, Any]]:
    """Where the week's changes actually landed, biggest first.

    Commit subjects skew towards whatever the committer documented most (this project's log is full of
    internal audit commits naming one event), so the digest weights its story by changed files instead.
    """
    code, out, _ = await _run(
        ["git", "log", f"--since={since_days} days ago", "--name-only", "--pretty=format:"], cwd=repo
    )
    if code != 0:
        return []
    counts: dict[str, int] = {}
    for line in out.splitlines():
        label = area_label(line)
        if label:
            counts[label] = counts.get(label, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[: max(1, limit)]
    return [{"area": label, "files": files, "band": size_band(files)} for label, files in ranked]


def change_area_facts_line(areas: list[dict[str, Any]]) -> str:
    if not areas:
        return "change areas unavailable"
    return "; ".join(f"{row.get('area')} ({row.get('band')})" for row in areas)


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
    open_total = 0
    code, out, _ = await _run(
        ["gh", "issue", "list", "--repo", repo_slug, "--state", "open", "--limit", "200", "--json", "number"]
    )
    if code == 0:
        try:
            open_total = len(json.loads(out or "[]"))
        except json.JSONDecodeError:
            open_total = 0
    return {
        "available": True,
        "opened": opened[:MAX_ISSUE_TITLES],
        "closed": closed[:MAX_ISSUE_TITLES],
        "opened_count": len(opened),
        "closed_count": len(closed),
        "open_total": open_total,
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


def playtest_observation(report_json: str | None) -> str:
    """The human observation text from a stored playtest report (skips empty/unreadable reports)."""
    raw = (report_json or "").strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("observation") or "").strip()


def playtest_facts_line(playtests: list[dict[str, Any]]) -> str:
    if not playtests:
        return "no playtest observations were recorded this week"
    lines: list[str] = []
    for row in playtests[:MAX_PLAYTEST_NOTES]:
        observation = str(row.get("observation") or "").strip()
        target = str(row.get("target") or "playtest").strip()
        if observation:
            lines.append(f"{target}: {observation[:200]}")
    return " | ".join(lines) if lines else f"{len(playtests)} playtest observations recorded"


def _capture_name(summary: str) -> str:
    """Note name from a `vault event-idea` / `vault suggestion` audit row (path -> stem)."""
    text = (summary or "").strip()
    if not text:
        return ""
    stem = text.rsplit("/", 1)[-1]
    if stem.endswith(".md"):
        stem = stem[:-3]
    return stem.strip()


def community_facts_line(captures: list[dict[str, Any]]) -> str:
    """Community ideas/suggestions written up through ChaosX this week.

    Sourced from the bot's own audit rows, so the count is exactly what members submitted and the bot
    wrote up. Vault file mtimes are never used — the vault syncs in bulk and rewrites them.
    """
    ideas: list[str] = []
    suggestions: list[str] = []
    for row in captures:
        name = _capture_name(str(row.get("summary") or ""))
        if not name:
            continue
        if str(row.get("command") or "") == "vault suggestion":
            suggestions.append(name)
        else:
            ideas.append(name)
    parts: list[str] = []
    if ideas:
        parts.append(f"{len(ideas)} event idea(s) written up: {', '.join(ideas[:3])}")
    if suggestions:
        parts.append(f"{len(suggestions)} suggestion(s) captured: {', '.join(suggestions[:3])}")
    return "; ".join(parts) if parts else "no member event ideas or suggestions were submitted this week"


def server_facts_line(server: dict[str, Any], *, include_online: bool = True) -> str:
    """Server-side facts, skipping zeros so a quiet week does not read as a wall of 0s.

    The member count comes from Discord (`GuildCounts`), never from the bot's `users` table: that table
    counts people who talked to the bot (36) and is not the server's size (69). When Discord did not
    answer, the count is left out entirely rather than replaced with a plausible-looking local number.
    """
    parts: list[str] = []
    if server.get("answers"):
        parts.append(f"{server['answers']} questions auto-answered by the bot")
    if server.get("qa_saved"):
        # "asked", not "answered": the model must not upgrade a question into a resolved answer.
        parts.append(f"{server['qa_saved']} questions asked in the server")
    if server.get("warnings"):
        parts.append(f"{server['warnings']} soft moderation warnings")
    if server.get("playtests"):
        parts.append(f"{server['playtests']} playtests logged")
    members = server.get("members")
    if members and str(server.get("members_source") or "discord") == "discord":
        online = server.get("online")
        label = f"{members} members in the server"
        # Public posts never name the online count: Hoops, 2026-09-23 - "the how many online information
        # will get outdated very quick so it shouldnt be included." The member count moves slowly; the
        # online count is stale within minutes.
        if include_online and isinstance(online, int) and online >= 0:
            label += f" ({online} online right now)"
        parts.append(label)
    return ", ".join(parts) if parts else "no tracked server activity in this window"


def issues_facts_line(issues: dict[str, Any]) -> str:
    if not issues.get("available"):
        return "GitHub issue data unavailable for this window"
    opened = issues.get("opened") or []
    closed = issues.get("closed") or []
    open_total = int(issues.get("open_total") or 0)
    if not opened and not closed:
        return f"no issue movement ({open_total} open)" if open_total else "no GitHub issue activity"
    parts: list[str] = []
    if opened:
        parts.append(f"{len(opened)} opened/touched ({', '.join(opened)})")
    if closed:
        parts.append(f"{len(closed)} closed ({', '.join(closed)})")
    if open_total:
        parts.append(f"{open_total} still open")
    return "; ".join(parts)


_ONLINE_RE = re.compile(
    r"(?P<pre>[,.\s]|\band\b|\bwith\b|\bwhile\b)*(?P<count>\d[\d,]*)\s*(?:members?|people|users)?\s*"
    r"online(?:\s+right\s+now)?(?P<post>[).,])?",
    re.IGNORECASE,
)


def strip_online_count(text: str) -> str:
    """Remove any "N online right now" figure from a public post.

    Belt-and-braces for Hoops' rule that the online count is too volatile for a weekly digest: the facts
    line no longer carries it and the prompt forbids it, and this catches the model adding it anyway.
    """
    lines_out: list[str] = []
    for line in str(text or "").splitlines():
        cleaned = _ONLINE_RE.sub("", line)
        # tidy what the removal leaves behind: stray brackets, doubled separators, dangling dashes
        if cleaned.count("(") != cleaned.count(")"):
            excess = cleaned.count("(") - cleaned.count(")")
            if excess > 0:
                cleaned = cleaned.replace("(", "", excess)
            else:
                cleaned = cleaned.replace(")", "", -excess)
        cleaned = re.sub(r"\(\s*\)", "", cleaned)
        cleaned = re.sub(r"\s+([,.!?;:])", r"\1", cleaned)
        cleaned = re.sub(r"([,;:])\s*(?=[,;:.!?])", "", cleaned)
        cleaned = re.sub(r"\s+-\s*-\s+", " - ", cleaned)
        cleaned = re.sub(r"\s+-\s+(?=[.,;:]|$)", "", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        lines_out.append(cleaned)
    return "\n".join(lines_out)


def digest_window_note(*, window_days: int = DIGEST_WINDOW_DAYS, now: datetime | None = None) -> str:
    """The small-print line naming the dates a digest actually covers.

    Hoops (2026-09-23): "what week? What dates does it span to?" The window is a rolling N days ending when
    the post is built, while the once-a-week guard keys off the ISO week, so the post states its own range
    and the reader never has to guess.
    """
    end = now or utcnow()
    start = end - timedelta(days=window_days)
    if start.year == end.year and start.month == end.month:
        span = f"{start.day}–{end.day} {end.strftime('%B %Y')}"
    elif start.year == end.year:
        span = f"{start.strftime('%-d %B')} – {end.strftime('%-d %B %Y')}"
    else:
        span = f"{start.strftime('%-d %B %Y')} – {end.strftime('%-d %B %Y')}"
    return f"-# Covering {span} (the last {window_days} days)"


def with_window_note(text: str, *, window_days: int = DIGEST_WINDOW_DAYS, now: datetime | None = None) -> str:
    """Insert the window note directly under the digest title line."""
    lines = str(text or "").splitlines()
    note = digest_window_note(window_days=window_days, now=now)
    if note in lines:
        return text
    for index, line in enumerate(lines):
        if line.strip():
            lines.insert(index + 1, "")
            lines.insert(index + 2, note)
            break
    else:
        return note
    return "\n".join(lines)


def build_digest_prompt(*, signals: dict[str, Any], max_chars: int = DIGEST_MAX_CHARS) -> str:
    commits = signals.get("commits") or {}
    issues = signals.get("issues") or {}
    server = signals.get("server") or {}
    playtests = signals.get("playtests") or []
    areas = signals.get("change_areas") or []
    return f"""Write the weekly Chaos Redux community digest. It is a short community post, not a report.

Facts (use only these, invent nothing, no pings/mentions):
- Mod version: {signals.get('version') or 'unknown'}
- Work completed in the last {signals.get('window_days', DIGEST_WINDOW_DAYS)} days: {commits.get('count', 0)} changes
- Where that work landed, biggest share first: {change_area_facts_line(areas)}
- Work worth naming, in the committer's own shorthand (translate it, never quote it): 
{_bullet(commits.get('notable') or [])}
- GitHub issues: {issues_facts_line(issues)}
- Playtest observations recorded this week: {playtest_facts_line(playtests)}
- Community ideas/suggestions submitted this week: {community_facts_line(signals.get('community_captures') or [])}
- Server activity: {server_facts_line(server, include_online=False)}

Audience: players, testers and friends of the mod — not programmers. Someone who has never opened the
repo must understand every line.

Hard rules:
- Weight the post by where the work landed, not by which subject lines are noisiest. If a large share of
  the week went into art, sound, text or scripting, say that. NEVER describe the whole week as one event
  or one task unless the facts really are that narrow. Spread the bullets over the areas that saw work.
- Describe what a player would notice ("the zombie outbreak now…", "convoys pay out correctly now").
- NEVER use file names, paths, repo hashes, branch names, commit counts as the headline, code blocks, or
  internal shorthand/acronyms (FSM, SCN-xxx, IW-xxx, FORM-xx, MCP, receipts, audits, crosswalks). If a term
  is internal, describe the effect instead. Event names and their numbers are fine — players know those.
- No statistics, no repository metrics, no counts of files or commits. Hoops does not want numbers he
  cannot verify from the post itself.
- No pings, no invented features, versions, dates or release promises.

Sections (exactly these, nothing else, in this order, each heading on its own line):
1. Title line: "🌟 **Weekly Chaos Redux digest — <version>** 🌟"
2. Heading "### 🛠️ This week in the mod" followed by at most 3 bullets, one line each (roughly 20 words),
   covering the areas that saw the most work. Group related work into one bullet instead of listing every
   change. Every bullet MUST start with "- " and then one fitting emoji (art 🎨, models 🧊, scripting ⚙️,
   sound 🔊, text ✍️, flags 🚩, balance ⚖️) — a Discord bullet list, never bare emoji lines.
3. Heading "### 💬 From the community" followed by one short line: playtests, reported issues, ideas
   written up, server activity.
   Always include the server's member count exactly as the facts give it (Discord's own figure). NEVER
   mention how many members are online right now - that number is stale within minutes of posting, so the
   public digest never carries it. If the week was otherwise quiet, say so in a few words instead of
   printing zeros.
4. Heading "### 🔎 What's next" followed by one short line naming the testing focus from the facts. Do NOT
   start that line with 🔎 (or any emoji) — the heading already carries it, and a repeated emoji looks like
   a mistake. Never
   promise future features, say something is "coming", or give dates/release timelines.

Emoji are for orientation only: one per heading as above, at most one per bullet, never a wall of them.
NEVER repeat a heading's emoji at the start of a line directly under it (no "### 🔎 What's next" followed by
"🔎 …"); the heading emoji belongs to the heading alone.
Blank line between sections; no section may sit inside another; never end a bullet mid-word.
Keep the whole post under {max_chars} characters and keep it tight — Hoops called the previous digest
bloated. Short and concrete beats complete. Plain Discord markdown."""


def digest_fallback(signals: dict[str, Any]) -> str:
    """Facts-only digest used when the model is unavailable.

    Deliberately jargon-free and short: areas that saw work, community signals and a pointer to the raw
    history, so the fallback never reads worse than the model version (no commit subjects or hashes).
    """
    commits = signals.get("commits") or {}
    issues = signals.get("issues") or {}
    server = signals.get("server") or {}
    playtests = signals.get("playtests") or []
    areas = signals.get("change_areas") or []
    repo = str(signals.get("repo_url") or "").rstrip("/")
    window = signals.get("window_days", DIGEST_WINDOW_DAYS)
    area_names = [str(row.get("area")) for row in areas[:3] if row.get("area")]
    if area_names:
        work_line = f"- Work in the last {window} days went into {', '.join(area_names)}."
    else:
        work_line = f"- {commits.get('count', 0)} changes landed in the last {window} days."
    lines = [
        f"🌟 **Weekly Chaos Redux digest** — {signals.get('version') or 'in development'} 🌟",
        "",
        "**🛠️ This week in the mod**",
        work_line,
        "- 📜 Details are in the repo history if you want the technical view.",
        "",
        "**💬 From the community**",
        (
            f"- {playtest_facts_line(playtests)}; {community_facts_line(signals.get('community_captures') or [])}; "
            f"{issues_facts_line(issues)}; {server_facts_line(server, include_online=False)}"
        ),
        "",
        "**What's next**",
        "- Testing focus stays open until the current area is confirmed; the digest updates weekly.",
    ]
    if repo:
        lines.append(f"- Full change list: {repo}/commits")
    return sanitize_post("\n".join(lines), max_chars=DIGEST_MAX_CHARS)


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
