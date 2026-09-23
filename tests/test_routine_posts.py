"""Tests for the autonomous routine-post layer (scheduling, facts, safety)."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from chaosx_bot.routine_posts import (
    DEV_DIGEST,
    RELEASE_POSTS,
    ROUTINE_POSTS,
    build_digest_prompt,
    build_release_prompt,
    descriptor_version,
    digest_fallback,
    git_commit_summary,
    git_commits_between,
    git_files_touched,
    git_head_sha,
    is_interval_due,
    is_weekly_due,
    content_facts_line,
    ideas_facts_line,
    issues_facts_line,
    plan_due_posts,
    playtest_facts_line,
    playtest_observation,
    repo_content_counts,
    vault_recent_documents,
    release_fallback,
    release_signal_changed,
    release_state_detail,
    sanitize_post,
    server_facts_line,
    weekly_period_key,
    weekly_slot,
)

MONDAY = datetime(2026, 9, 21, tzinfo=timezone.utc)  # known Monday, 2026-W39
AFTER_SLOT = MONDAY + timedelta(days=2, hours=4)  # Thursday 04:00 UTC, slot long past


def _signals() -> dict:
    return {
        "window_days": 7,
        "version": "0.1",
        "head": "abc1234",
        "event_files": 30,
        "commits": {"count": 181, "notable": ["Add zombie outbreak event", "Rework cluster spine"]},
        "issues": {
            "available": True,
            "opened": ["#12 Crash on load"],
            "opened_count": 1,
            "closed": ["#7 Missing localisation"],
            "closed_count": 1,
            "open_total": 2,
        },
        "server": {"answers": 12, "qa_saved": 3, "warnings": 1, "playtests": 2, "members": 40},
        "content": {"events": 162, "decisions": 213, "focuses": 53},
        "playtests": [{"target": "event006", "observation": "convoy payments looked correct but the AI stalled"}],
        "event_specs": ["007 - Void Rift"],
        "suggestions": ["Tester: add an Iceland focus tree"],
        "repo_url": "https://github.com/klimPaskov/Chaos-Redux",
    }


# --- scheduling --------------------------------------------------------------


def test_post_specs_are_coherent():
    assert DEV_DIGEST.kind == "weekly" and RELEASE_POSTS.kind == "release"
    names = [spec.name for spec in ROUTINE_POSTS]
    assert len(names) == len(set(names))
    assert all(name.startswith("routine_") for name in names)


def test_weekly_slot_is_monday_and_period_key_is_stable_within_the_week():
    slot = weekly_slot(DEV_DIGEST, AFTER_SLOT)
    assert slot.weekday() == 0 and slot.hour == DEV_DIGEST.hour_utc
    assert slot <= AFTER_SLOT
    # Sunday 23:00 of the same ISO week keeps the key; the next Monday starts a new one.
    assert weekly_period_key(slot) == weekly_period_key(slot + timedelta(days=6, hours=11))
    assert weekly_period_key(slot) != weekly_period_key(slot + timedelta(days=7))


def test_weekly_due_catches_up_but_never_posts_twice_in_a_week():
    # Slot already passed this week and nothing recorded yet -> due (catch-up).
    assert is_weekly_due(DEV_DIGEST, now=AFTER_SLOT, last_period_key=None)
    # Same week already posted -> not due again, even if it fires minutes later.
    assert not is_weekly_due(
        DEV_DIGEST, now=AFTER_SLOT, last_period_key=weekly_period_key(AFTER_SLOT)
    )
    # Earlier in the week, before the slot -> not due yet.
    before_slot = MONDAY + timedelta(hours=6)
    assert not is_weekly_due(DEV_DIGEST, now=before_slot, last_period_key=None)
    # Previous week's key must not block this week.
    previous_week = weekly_period_key(MONDAY - timedelta(days=7))
    assert is_weekly_due(DEV_DIGEST, now=AFTER_SLOT, last_period_key=previous_week)


def test_interval_due_gates_release_checks():
    now = AFTER_SLOT
    assert is_interval_due(RELEASE_POSTS, now=now, checked_at=None)
    assert not is_interval_due(
        RELEASE_POSTS, now=now, checked_at=(now - timedelta(hours=1)).isoformat()
    )
    assert is_interval_due(
        RELEASE_POSTS,
        now=now,
        checked_at=(now - timedelta(hours=RELEASE_POSTS.interval_hours + 1)).isoformat(),
    )


def test_plan_due_posts_respects_kill_switch_and_stored_state():
    specs = (DEV_DIGEST, RELEASE_POSTS)
    posted_this_week = {
        DEV_DIGEST.name: {"period_key": weekly_period_key(AFTER_SLOT)},
        RELEASE_POSTS.name: {"checked_at": AFTER_SLOT.isoformat()},
    }
    enabled = {DEV_DIGEST.name: True, RELEASE_POSTS.name: True}
    assert plan_due_posts(specs, now=AFTER_SLOT, states=posted_this_week, enabled=enabled) == []

    # Disabled automations never run, even when due.
    disabled = {DEV_DIGEST.name: False, RELEASE_POSTS.name: False}
    assert plan_due_posts(specs, now=AFTER_SLOT, states={}, enabled=disabled) == []

    # Fresh state: weekly due, release check due.
    due = plan_due_posts(specs, now=AFTER_SLOT, states={}, enabled=enabled)
    assert [spec.name for spec in due] == [DEV_DIGEST.name, RELEASE_POSTS.name]


# --- safety -----------------------------------------------------------------


def test_sanitize_post_strips_pings_and_caps_length():
    dirty = "Hello @everyone <@&123456789012345678> <@987654321098765432> @here team"
    cleaned = sanitize_post(dirty)
    for banned in ("@everyone", "@here", "<@", "<@&"):
        assert banned not in cleaned
    assert "team" in cleaned

    capped = sanitize_post("x" * 5000, max_chars=200)
    assert len(capped) <= 201
    assert capped.endswith("…")


def test_builders_never_emit_mentions_from_facts():
    signals = _signals()
    signals["commits"]["notable"].append("@everyone look at this")
    text = digest_fallback(signals)
    assert "@everyone" not in text


# --- release signal ---------------------------------------------------------


def test_release_signal_needs_a_baseline_then_detects_version_and_tag_changes():
    assert not release_signal_changed(state={}, version="0.1", tag="")
    detail = release_state_detail(version="0.1", tag="v0.1", head="abc1234", commits=["a"])
    assert not release_signal_changed(state={"detail": detail}, version="0.1", tag="v0.1")
    assert release_signal_changed(state={"detail": detail}, version="0.2", tag="v0.1")
    assert release_signal_changed(state={"detail": detail}, version="0.1", tag="v0.2")
    # Unknown version/tag must never look like a change.
    assert not release_signal_changed(state={"detail": detail}, version="", tag="")
    # Corrupt state is treated as "no baseline", not as a change.
    assert not release_signal_changed(state={"detail": "{not json"}, version="0.2", tag="v1")


def test_release_state_detail_round_trips():
    detail = release_state_detail(version="0.2", tag="v0.2", head="deadbee", commits=["Add X"])
    state = {"detail": detail}
    assert not release_signal_changed(state=state, version="0.2", tag="v0.2")
    assert release_signal_changed(state=state, version="0.3", tag="v0.2")


# --- prompts / fallbacks ----------------------------------------------------


def test_prompts_carry_the_facts_and_forbid_invention():
    digest_prompt = build_digest_prompt(signals=_signals())
    assert "invent nothing" in digest_prompt
    assert "Add zombie outbreak event" in digest_prompt
    assert "#12 Crash on load" in digest_prompt
    assert "181" in digest_prompt


def test_digest_prompt_is_player_first_and_wider_scope():
    prompt = build_digest_prompt(signals=_signals())
    # audience + hard rules against repo/technical language
    assert "not programmers" in prompt
    assert "NEVER use file names, paths, repo hashes, branch names" in prompt
    assert "never quote them" in prompt
    assert "NOT a changelog" in prompt
    # wider scope: content scale, playtests, community ideas, server
    assert "162 event files" in prompt and "213 decision files" in prompt
    assert "convoy payments looked correct" in prompt
    assert "007 - Void Rift" in prompt
    assert "add an Iceland focus tree" in prompt
    # the five sections, in order
    for section in ("Weekly Chaos Redux digest", "This week in the mod", "The mod right now", "From the community", "What's next"):
        assert section in prompt


def test_digest_fallback_is_jargon_free_and_wider_scope():
    digest = digest_fallback(_signals())
    assert "Weekly Chaos Redux digest" in digest
    assert "This week in the mod" in digest
    assert "The mod right now" in digest
    assert "From the community" in digest
    assert "What's next" in digest
    assert "181 changes landed" in digest
    assert "162 event files" in digest
    assert "convoy payments looked correct" in digest
    assert "007 - Void Rift" in digest
    assert "2 still open" in digest
    assert "https://github.com/klimPaskov/Chaos-Redux/commits" in digest
    # no technical leakage in the fallback path
    assert "abc1234" not in digest
    assert "Add zombie outbreak event" not in digest
    assert "Repo HEAD" not in digest
    assert "@everyone" not in digest and "@here" not in digest

    release_prompt = build_release_prompt(
        signals={
            "version": "0.2",
            "previous_version": "0.1",
            "commits": ["Add zombie outbreak event"],
            "commit_count": 1,
            "head": "abc1234",
            "release_tag": "",
        }
    )
    assert "Chaos Redux 0.2" in release_prompt
    assert "Add zombie outbreak event" in release_prompt


def test_fallbacks_render_only_supplied_facts():
    digest = digest_fallback(_signals())
    assert "181" in digest
    assert "#12 Crash on load" in digest

    release = release_fallback(
        {"version": "0.2", "commits": ["Add zombie outbreak event"], "commit_count": 1}
    )
    assert "Chaos Redux 0.2" in release
    assert "Add zombie outbreak event" in release


def test_quiet_week_facts_do_not_render_as_a_wall_of_zeros():
    quiet_server = {"answers": 0, "qa_saved": 0, "warnings": 0, "playtests": 0, "members": 0}
    quiet_issues = {"available": True, "opened": [], "closed": [], "opened_count": 0, "closed_count": 0}
    assert server_facts_line(quiet_server) == "no tracked server activity in this window"
    assert issues_facts_line(quiet_issues) == "no GitHub issue activity"
    assert issues_facts_line({"available": False}) == "GitHub issue data unavailable for this window"

    signals = _signals() | {
        "commit": {"count": 0, "notable": []},
        "issues": quiet_issues,
        "server": quiet_server,
    }
    text = digest_fallback(signals)
    assert "no GitHub issue activity" in text
    assert "no tracked server activity" in text
    assert "0 questions auto-answered" not in text
    assert "0 members" not in text


def test_issue_facts_line_lists_real_titles():
    line = issues_facts_line(
        {"available": True, "opened": ["#12 Crash on load"], "closed": ["#7 Localisation"]}
    )
    assert "#12 Crash on load" in line and "#7 Localisation" in line


# --- real git signals -------------------------------------------------------


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def git_repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "tests@example.com")
    _git(tmp_path, "config", "user.name", "ChaosX tests")
    (tmp_path / "events").mkdir()
    (tmp_path / "events" / "zombie.txt").write_text("outbreak", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "Add zombie outbreak event")
    (tmp_path / "descriptor.mod").write_text(
        'name="Chaos Redux"\nversion="0.2"\nsupported_version="1.19.*"\n', encoding="utf-8"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "Bump version")
    return tmp_path


async def test_git_signals_read_real_history(git_repo):
    summary = await git_commit_summary(git_repo, since_days=7)
    assert summary["available"] is True
    assert summary["count"] == 2
    assert "Add zombie outbreak event" in summary["subjects"]
    assert summary["latest"] == "Bump version"

    touched = await git_files_touched(git_repo, since_days=7, prefix="events")
    assert touched == 1

    version = await descriptor_version(git_repo)
    assert version == "0.2"

    head = await git_head_sha(git_repo)
    assert head and len(head) <= 12

    commits = await git_commits_between(git_repo, old_sha="HEAD~1")
    assert commits == ["Bump version"]


async def test_git_signals_degrade_when_repo_is_missing(tmp_path):
    missing = tmp_path / "nope"
    summary = await git_commit_summary(missing, since_days=7)
    assert summary["available"] is False
    assert summary["count"] == 0
    assert await descriptor_version(missing) == ""
    assert await git_head_sha(missing) == ""
    assert await git_commits_between(missing, old_sha="HEAD~1") == []

# --- wider scope signals ----------------------------------------------------


def test_repo_content_counts_counts_events_decisions_and_focuses(tmp_path):
    repo = tmp_path / "mod"
    (repo / "events").mkdir(parents=True)
    (repo / "events" / "a.txt").write_text("x")
    (repo / "events" / "nested").mkdir()
    (repo / "events" / "nested" / "b.txt").write_text("x")
    (repo / "common" / "decisions").mkdir(parents=True)
    (repo / "common" / "decisions" / "one.txt").write_text("x")
    (repo / "common" / "focus").mkdir(parents=True)
    (repo / "common" / "focus" / "tree.txt").write_text("x")
    (repo / "common" / "focus" / "notes.md").write_text("x")  # not .txt, ignored
    counts = repo_content_counts(repo)
    assert counts == {"events": 2, "decisions": 1, "focuses": 1}
    assert repo_content_counts(tmp_path / "missing") == {"events": 0, "decisions": 0, "focuses": 0}


def test_vault_recent_documents_honours_the_window(tmp_path):
    import os
    from time import time

    specs = tmp_path / "vault" / "Events" / "Event Specs"
    specs.mkdir(parents=True)
    fresh = specs / "007 - Void Rift.md"
    stale = specs / "002 - Zombie Outbreak.md"
    fresh.write_text("new idea")
    stale.write_text("old idea")
    old = time() - 30 * 86400
    os.utime(stale, (old, old))
    names = vault_recent_documents(tmp_path / "vault", "Events/Event Specs", since_days=7)
    assert names == ["007 - Void Rift"]
    assert vault_recent_documents(tmp_path / "vault", "Planning/Community Suggestions") == []


def test_playtest_observation_reads_only_real_observations():
    assert playtest_observation('{"observation": "AI stalled at the convoy"}' ) == "AI stalled at the convoy"
    assert playtest_observation("{not json") == ""
    assert playtest_observation("") == ""
    assert playtest_observation('{"event_id": "event006"}') == ""


def test_fact_lines_are_honest_about_quiet_weeks():
    assert content_facts_line({}) == "content counts unavailable"
    assert "no playtest observations" in playtest_facts_line([])
    assert "no new community ideas" in ideas_facts_line(event_specs=[], suggestions=[])
    assert "event ideas/specs added" in ideas_facts_line(event_specs=["007 - Void Rift"], suggestions=[])
    assert "no GitHub issue activity" in issues_facts_line({"available": True, "opened": [], "closed": []})
    assert "unavailable" in issues_facts_line({"available": False})
    assert "no issue movement (3 open)" in issues_facts_line(
        {"available": True, "opened": [], "closed": [], "open_total": 3}
    )
    moved = issues_facts_line({"available": True, "opened": ["#9 crash"], "closed": [], "open_total": 3})
    assert "#9 crash" in moved and "3 still open" in moved
    assert "convoy" in playtest_facts_line([{"target": "event006", "observation": "convoy stalled"}])
