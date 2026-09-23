"""Owner-requested announcements: draft from a brief plus real repo facts, then post.

The bot writes announcement copy only from facts it can verify (mod version, commits since the
last announcement, closed issues). The owner brief sets the topic; the model may not add features,
dates or promises that the facts do not support. Every announcement passes through the same
ping-stripping sanitizer the routine posts use, so generated text can never ping a channel.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .routine_posts import (
    MAX_NOTABLE_COMMITS,
    git_commits_between,
    git_commit_summary,
    git_head_sha,
    descriptor_version,
    github_issue_activity,
    sanitize_post,
    utcnow,
)

MAX_ANNOUNCEMENT_CHARS = 1600
DEFAULT_WINDOW_DAYS = 7

_TITLE_FALLBACK = "Chaos Redux update"


@dataclass(frozen=True)
class AnnouncementFacts:
    """Everything the announcement copy is allowed to claim."""

    topic: str = ""
    version: str = ""
    previous_version: str = ""
    head: str = ""
    commits: list[str] = field(default_factory=list)
    issues_closed: list[str] = field(default_factory=list)
    issues_opened: list[str] = field(default_factory=list)
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "version": self.version,
            "previous_version": self.previous_version,
            "head": self.head,
            "commits": self.commits,
            "issues_closed": self.issues_closed,
            "issues_opened": self.issues_opened,
            "source": self.source,
        }


def announcement_id(*, topic: str, version: str, head: str) -> str:
    raw = "|".join([topic.strip().lower(), version, head])
    return "announce-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


async def collect_announcement_facts(
    *,
    repo: Path,
    github_repo: str,
    topic: str = "",
    previous_head: str = "",
    previous_version: str = "",
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> AnnouncementFacts:
    """Gather the real signals behind an announcement (no invention allowed downstream)."""
    version, head, issues = await _gather(repo, github_repo, window_days)
    commits: list[str] = []
    source = ""
    if previous_head:
        commits = await git_commits_between(repo, old_sha=previous_head)
        source = f"commits since {previous_head[:9]}"
    if not commits:
        summary = await git_commit_summary(repo, since_days=window_days)
        commits = list(summary.get("notable") or [])
        source = source or f"notable commits, last {window_days} days"
    return AnnouncementFacts(
        topic=topic.strip(),
        version=version,
        previous_version=previous_version,
        head=head,
        commits=commits[:MAX_NOTABLE_COMMITS],
        issues_closed=list(issues.get("closed") or []),
        issues_opened=list(issues.get("opened") or []),
        source=source,
    )


async def _gather(repo: Path, github_repo: str, window_days: int):
    return await asyncio.gather(
        descriptor_version(repo),
        git_head_sha(repo),
        github_issue_activity(github_repo, since_days=window_days),
    )


def _bullets(lines: list[str], limit: int = MAX_NOTABLE_COMMITS) -> str:
    out = [f"- {line}" for line in lines[:limit] if line]
    return "\n".join(out) if out else "- (no verified changes recorded)"


def build_announcement_prompt(
    *, facts: AnnouncementFacts, max_chars: int = MAX_ANNOUNCEMENT_CHARS
) -> str:
    brief = facts.topic or "announce the current state of the mod (no specific brief was given)"
    closed = ", ".join(facts.issues_closed) if facts.issues_closed else "none closed"
    return f"""Write a Chaos Redux announcement for the server's announcements channel.

Owner brief: {brief}

Verified facts (use only these; invent nothing, no pings or mentions of any kind):
- Mod version: {facts.version or 'unknown'} (previous announced version: {facts.previous_version or 'not recorded'})
- Repo HEAD: {facts.head or 'unknown'}
- Verified change list ({facts.source or 'recent history'}):
{_bullets(facts.commits)}
- GitHub issues closed in this window: {closed}

Write:
1. One bold title line, max 90 characters, leading with the mod name and what this is about.
2. Two to five bullets of concrete changes, grouped from the verified change list (no file paths).
3. One closing line: bugs and feedback go to the issues channel.

Keep it under {max_chars} characters, plain Discord markdown, no code blocks, no pings,
no release dates, no promises, no features that are not in the verified change list."""


def title_from_body(body: str) -> str:
    """First meaningful line of a post, with markdown emphasis stripped (used as the title)."""
    for line in (body or "").splitlines():
        stripped = line.strip().strip("*").strip()
        if stripped:
            return stripped[:90]
    return ""


def build_announcement_fallback(facts: AnnouncementFacts) -> str:
    """Deterministic copy used when the model is unavailable (facts only, never silent)."""
    title = facts.topic.strip() or f"{_TITLE_FALLBACK} {facts.version or ''}".strip()
    title = re.sub(r"\s+", " ", title)[:90]
    body = [f"**{title}**", ""]
    if facts.commits:
        body.append(_bullets(facts.commits))
    else:
        body.append("- No verified change list was available for this announcement.")
    if facts.issues_closed:
        body.append("")
        body.append(f"Closed issues: {', '.join(facts.issues_closed)}")
    body.append("")
    body.append("Bugs and feedback go to the issues channel.")
    return sanitize_post("\n".join(body), max_chars=MAX_ANNOUNCEMENT_CHARS)


def announcement_detail(facts: AnnouncementFacts) -> str:
    return json.dumps(facts.as_dict(), ensure_ascii=False)[:4000]


@dataclass
class AnnouncementResult:
    announcement_id: str
    status: str  # "drafted" | "posted" | "error"
    body: str = ""
    title: str = ""
    detail: str = ""
    channel_id: int | None = None
    message_id: int | None = None
    facts: AnnouncementFacts | None = None
    created_at: str = field(default_factory=lambda: utcnow().isoformat())
