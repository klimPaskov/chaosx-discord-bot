"""A stale checkout must not make the digest claim nothing landed.

The bug this pins: the host checkout's HEAD was twelve days old while hundreds of commits had been pushed.
Every history read used HEAD, so the week looked empty (Hoops, 2026-10-19).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chaosx_bot.routine_posts import (
    git_change_areas,
    git_commit_summary,
    git_head_sha,
    preferred_revision,
    revision_commit_date,
)


def _git(repo: Path, *args: str, date: str | None = None) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}
    if date:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    out = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, env=env, check=True)
    return out.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "master")
    (path / "a.txt").write_text("one\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "first", date="2026-10-13T10:00:00+00:00")
    (path / "b.txt").write_text("two\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "second", date="2026-10-15T10:00:00+00:00")
    head = _git(path, "rev-parse", "HEAD")
    # a remote-tracking ref that is ahead of the checkout, exactly like the host after a fetch
    _git(path, "update-ref", "refs/remotes/origin/master", head)
    _git(path, "checkout", "-q", "HEAD~1", "--detach")
    return path


def test_prefers_the_fetched_branch_over_a_stale_head(repo: Path):
    assert asyncio.run(preferred_revision(repo)) == "origin/master"


def test_summary_reads_the_fetched_branch(repo: Path):
    window_start = datetime(2026, 10, 12, tzinfo=timezone.utc)
    window_end = window_start + timedelta(days=7)
    stale = asyncio.run(git_commit_summary(repo, since=window_start, until=window_end, rev="HEAD"))
    fresh = asyncio.run(
        git_commit_summary(repo, since=window_start, until=window_end, rev="origin/master")
    )
    assert stale["count"] == 1, "the stale view should only see the commit it is checked out at"
    assert fresh["count"] == 2, "the fetched branch should see both commits"


def test_head_sha_can_name_the_fetched_branch(repo: Path):
    assert asyncio.run(git_head_sha(repo, rev="origin/master")) == _git(repo, "rev-parse", "--short", "origin/master")


def test_revision_commit_date_parses(repo: Path):
    when = asyncio.run(revision_commit_date(repo, "origin/master"))
    assert when is not None and when.year == 2026 and when.month == 10


def test_one_bulk_art_commit_cannot_outrank_repeated_systems_work(tmp_path: Path):
    """The bug behind "focus tree icons took the biggest slice": files were counted, not commits."""
    path = tmp_path / "areas"
    path.mkdir()
    _git(path, "init", "-q", "-b", "master")

    # one commit that ships fifty icon files
    icons = path / "gfx" / "interface"
    icons.mkdir(parents=True)
    for i in range(50):
        (icons / f"icon_{i}.dds").write_text("x")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "export icons", date="2026-10-13T10:00:00+00:00")

    # three commits that each touch the scripting layer
    scripts = path / "common" / "scripted_effects"
    scripts.mkdir(parents=True)
    for i in range(3):
        (scripts / f"effect_{i}.txt").write_text("x")
        _git(path, "add", ".")
        _git(path, "commit", "-qm", f"wire effect {i}", date=f"2026-10-1{4 + i}T10:00:00+00:00")

    window_start = datetime(2026, 10, 12, tzinfo=timezone.utc)
    window_end = window_start + timedelta(days=7)
    areas = asyncio.run(git_change_areas(path, since=window_start, until=window_end))
    ranked = [a["area"] for a in areas]
    assert "game scripting" in ranked
    assert ranked.index("game scripting") < ranked.index("interface art"), ranked
    assert areas[0]["commits"] >= 3
