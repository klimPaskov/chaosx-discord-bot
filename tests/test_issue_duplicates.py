from types import SimpleNamespace
from typing import Any, cast

import pytest

from chaosx_bot import bot as bot_module
from chaosx_bot.issue_duplicates import (
    SimilarGitHubIssue,
    candidate_review_context,
    clear_duplicate_candidate,
    parse_duplicate_decision,
    rank_similar_issues,
)


def _candidate(number: int = 17) -> SimilarGitHubIssue:
    return SimilarGitHubIssue(
        number=number,
        title="[Bug] Zombie outbreak crashes on activation",
        body="The game crashes when the zombie outbreak event is activated.",
        url=f"https://github.com/klimPaskov/Chaos-Redux/issues/{number}",
        state="OPEN",
        score=0.95,
    )


def test_rank_similar_issues_prioritizes_same_underlying_report() -> None:
    issues = [
        {
            "number": 17,
            "title": "[Bug] Zombie outbreak crashes on activation",
            "body": "The game crashes when the zombie outbreak event is activated.",
            "url": "https://github.com/klimPaskov/Chaos-Redux/issues/17",
            "state": "OPEN",
        },
        {
            "number": 18,
            "title": "Add more music tracks",
            "body": "The soundtrack needs more variety.",
            "url": "https://github.com/klimPaskov/Chaos-Redux/issues/18",
            "state": "CLOSED",
        },
    ]

    ranked = rank_similar_issues(
        issues,
        title="[Crash] Zombie outbreak crashes when activated",
        description="Activating the zombie outbreak event immediately crashes the game.",
    )

    assert [item.number for item in ranked] == [17]
    assert ranked[0].score > 0.5


def test_rank_similar_issues_does_not_match_generic_issue_words() -> None:
    issues = [
        {
            "number": 2,
            "title": "[General] ChaosX issue command test",
            "body": "This issue only tests the Discord command.",
            "url": "https://github.com/klimPaskov/Chaos-Redux/issues/2",
            "state": "CLOSED",
        }
    ]

    ranked = rank_similar_issues(
        issues,
        title="[Enhancement] Improve nuclear winter balance",
        description="The nuclear winter penalties should scale more gradually.",
    )

    assert ranked == []


def test_duplicate_decision_must_reference_a_listed_candidate() -> None:
    candidate = _candidate()

    assert parse_duplicate_decision("DUPLICATE #17: same crash", [candidate]) == candidate
    assert parse_duplicate_decision("DUPLICATE #999: invented", [candidate]) is None
    assert "#17" in candidate_review_context([candidate])


def test_only_near_exact_candidate_is_a_clear_deterministic_duplicate() -> None:
    exact = _candidate()
    ambiguous = SimilarGitHubIssue(
        number=18,
        title="Zombie outbreak balance",
        body="The event should be rebalanced.",
        url="https://github.com/klimPaskov/Chaos-Redux/issues/18",
        state="OPEN",
        score=0.71,
    )

    assert clear_duplicate_candidate([exact]) == exact
    assert clear_duplicate_candidate([ambiguous]) is None


class _FakeStore:
    def __init__(self) -> None:
        self.audits: list[dict[str, object]] = []

    async def audit(self, **kwargs: object) -> None:
        self.audits.append(kwargs)


@pytest.mark.asyncio
async def test_submit_rejects_clear_duplicate_and_does_not_create_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = _candidate()
    store = _FakeStore()
    fake_bot = SimpleNamespace(
        settings=SimpleNamespace(github_repo="klimPaskov/Chaos-Redux"),
        store=store,
    )

    async def fake_lookup(*args: object, **kwargs: object) -> tuple[bool, list[SimilarGitHubIssue], str]:
        return True, [candidate], ""

    async def forbidden_review(*args: object, **kwargs: object) -> tuple[bool, str, SimilarGitHubIssue]:
        raise AssertionError("a clear duplicate must be rejected before model review")

    async def forbidden_create(*args: object, **kwargs: object) -> tuple[bool, str]:
        raise AssertionError("duplicate reports must not create a GitHub issue")

    monkeypatch.setattr(bot_module, "find_similar_github_issues", fake_lookup)
    monkeypatch.setattr(bot_module, "ai_review_issue_report", forbidden_review)
    monkeypatch.setattr(bot_module, "create_github_issue", forbidden_create)

    ok, message, issue_title = await bot_module.submit_validated_issue(
        cast(Any, fake_bot),
        actor_id=1,
        guild_id=2,
        channel_id=3,
        reporter="tester",
        issue_type="enhancement",
        title="Zombie outbreak crashes on activation",
        description="Activating the zombie outbreak event immediately crashes the game.",
    )

    assert not ok
    assert issue_title is None
    assert "#17" in message
    assert candidate.url in message
    assert "not approved or posted again" in message
    assert store.audits[0]["command"] == "issue duplicate"


@pytest.mark.asyncio
async def test_submit_fails_closed_when_existing_issues_cannot_be_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bot = SimpleNamespace(
        settings=SimpleNamespace(github_repo="klimPaskov/Chaos-Redux"),
        store=_FakeStore(),
    )

    async def failed_lookup(*args: object, **kwargs: object) -> tuple[bool, list[SimilarGitHubIssue], str]:
        return False, [], "authentication failed"

    async def forbidden_review(*args: object, **kwargs: object) -> tuple[bool, str, None]:
        raise AssertionError("AI review must not run when duplicate lookup fails")

    monkeypatch.setattr(bot_module, "find_similar_github_issues", failed_lookup)
    monkeypatch.setattr(bot_module, "ai_review_issue_report", forbidden_review)

    ok, message, issue_title = await bot_module.submit_validated_issue(
        cast(Any, fake_bot),
        actor_id=1,
        guild_id=2,
        channel_id=3,
        reporter="tester",
        issue_type="enhancement",
        title="Improve nuclear winter balance",
        description="The nuclear winter penalties should scale more gradually over time.",
    )

    assert not ok
    assert issue_title is None
    assert "could not check existing GitHub issues" in message
    assert "authentication" not in message
