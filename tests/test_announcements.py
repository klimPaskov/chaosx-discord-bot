"""Tests for owner-requested announcements (facts, prompts, storage round-trip)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from chaosx_bot.announcements import (
    AnnouncementFacts,
    announcement_detail,
    announcement_id,
    build_announcement_fallback,
    build_announcement_prompt,
    collect_announcement_facts,
    title_from_body,
)
from chaosx_bot.storage import Store


def _facts(**overrides) -> AnnouncementFacts:
    base = dict(
        topic="Chaos Redux 0.2 is out with event006 FSM fixes",
        version="0.2",
        previous_version="0.1",
        head="abc1234",
        commits=["Cancel FSM projects on receipt loss", "Align diplomatic convoy payment equality"],
        issues_closed=["#7 Missing localisation"],
        issues_opened=["#12 Crash on load"],
        source="commits since deadbee",
    )
    base.update(overrides)
    return AnnouncementFacts(**base)


def test_prompt_carries_brief_and_verified_facts_only():
    prompt = build_announcement_prompt(facts=_facts())
    assert "Chaos Redux 0.2 is out with event006 FSM fixes" in prompt
    assert "invent nothing" in prompt
    assert "Cancel FSM projects on receipt loss" in prompt
    assert "#7 Missing localisation" in prompt
    assert "0.1" in prompt and "abc1234" in prompt
    assert "no release dates" in prompt


def test_prompt_without_a_brief_asks_for_current_state():
    prompt = build_announcement_prompt(facts=_facts(topic=""))
    assert "no specific brief was given" in prompt


def test_fallback_uses_brief_as_title_and_only_verified_changes():
    body = build_announcement_fallback(_facts())
    assert body.startswith("**Chaos Redux 0.2 is out with event006 FSM fixes**")
    assert "Cancel FSM projects on receipt loss" in body
    assert "issues channel" in body
    assert "@everyone" not in body and "<@" not in body


def test_fallback_without_commits_says_so_instead_of_inventing():
    body = build_announcement_fallback(_facts(commits=[], issues_closed=[]))
    assert "No verified change list" in body


def test_fallback_caps_length():
    long_brief = "x" * 4000
    body = build_announcement_fallback(_facts(topic=long_brief))
    assert len(body) <= 1600


def test_announcement_id_is_stable_and_topic_sensitive():
    a = announcement_id(topic="Bump", version="0.2", head="abc1234")
    b = announcement_id(topic="bump ", version="0.2", head="abc1234")
    c = announcement_id(topic="Other", version="0.2", head="abc1234")
    assert a == b
    assert a != c
    assert a.startswith("announce-")


def test_title_from_body_strips_emphasis():
    assert title_from_body("\n\n**Chaos Redux 0.2**\n\nbody") == "Chaos Redux 0.2"
    assert title_from_body("   ") == ""


def test_announcement_detail_is_json_round_trip():
    facts = _facts()
    parsed = json.loads(announcement_detail(facts))
    assert parsed["version"] == "0.2"
    assert parsed["head"] == "abc1234"
    assert parsed["commits"][0].startswith("Cancel FSM")


# --- real git facts ---------------------------------------------------------


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


@pytest.fixture()
def git_repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "tests@example.com")
    _git(tmp_path, "config", "user.name", "ChaosX tests")
    (tmp_path / "descriptor.mod").write_text('name="Chaos Redux"\nversion="0.1"\n', encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "Start mod")
    return tmp_path


async def test_collect_facts_uses_commits_since_the_previous_announcement(git_repo):
    previous_head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (git_repo / "descriptor.mod").write_text('name="Chaos Redux"\nversion="0.2"\n', encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "Cancel FSM projects on receipt loss")

    facts = await collect_announcement_facts(
        repo=git_repo,
        github_repo="klimPaskov/Chaos-Redux",
        topic="0.2 is out",
        previous_head=previous_head,
        previous_version="0.1",
    )
    assert facts.version == "0.2"
    assert facts.previous_version == "0.1"
    assert facts.commits == ["Cancel FSM projects on receipt loss"]
    assert facts.source == f"commits since {previous_head[:9]}"
    assert facts.head and facts.head != previous_head


async def test_collect_facts_without_history_falls_back_to_notable_commits(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    facts = await collect_announcement_facts(
        repo=repo, github_repo="klimPaskov/Chaos-Redux", topic="", previous_head=""
    )
    assert facts.version == ""
    assert facts.commits == []


# --- storage round trip -----------------------------------------------------


async def test_announcement_storage_draft_then_posted(tmp_path):
    store = Store(tmp_path / "chaosx.db")
    await store.init()
    draft_id = announcement_id(topic="0.2 is out", version="0.2", head="abc1234")
    detail = announcement_detail(_facts())

    await store.record_announcement(
        draft_id, actor_id=1, guild_id=2, topic="0.2 is out", body="**Draft body**",
        status="draft", detail=detail,
    )
    draft = await store.latest_draft_announcement(topic="0.2 is out")
    assert draft is not None and draft["body"] == "**Draft body**"
    assert await store.last_announcement(status="posted") is None

    await store.record_announcement(
        draft_id, actor_id=1, guild_id=2, topic="0.2 is out", status="posted",
        destination_channel_id="1395467364916662392", message_id="555",
    )
    posted = await store.last_announcement(status="posted")
    assert posted is not None and posted["announcement_id"] == draft_id
    # The facts recorded at draft time survive the status change (used for the next diff).
    assert json.loads(posted["detail"])["head"] == "abc1234"
    # No drafts left for that topic.
    assert await store.latest_draft_announcement(topic="0.2 is out") is None
    listed = await store.list_announcements(limit=5)
    assert listed and listed[0]["status"] == "posted"
