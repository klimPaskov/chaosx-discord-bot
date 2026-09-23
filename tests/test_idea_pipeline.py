"""The event idea pipeline: intake logic, lifecycle, board and the promotion bridge."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chaosx_bot import ideas  # noqa: E402
from chaosx_bot.activity import BONUS_XP  # noqa: E402
from chaosx_bot.community_notes import promote_community_idea  # noqa: E402
from chaosx_bot.storage import Store  # noqa: E402


def test_lifecycle_labels_cover_every_status():
    for status in ideas.IDEA_STATUSES:
        assert ideas.status_label(status)
        assert ideas.status_emoji(status)
    assert ideas.status_label(ideas.SHIPPED) == "In the mod"
    assert ideas.status_label(ideas.DECLINED) == "Not this time"


def test_staged_awards_pay_for_acceptance_not_filing():
    # filing pays something, acceptance pays much more, and closing pays nothing
    assert 0 < BONUS_XP["idea_filed"] < BONUS_XP["idea_planned"] < BONUS_XP["idea_shipped"]
    assert ideas.award_for_status(ideas.PLANNED) == BONUS_XP["idea_planned"]
    assert ideas.award_for_status(ideas.SHIPPED) == BONUS_XP["idea_shipped"]
    assert ideas.award_for_status(ideas.DECLINED) == 0
    assert ideas.award_for_status(ideas.FILED) == 0  # filing is paid once, at file time


def test_review_buttons_cover_the_pipeline():
    statuses = {status for status, _label, _style in ideas.REVIEW_BUTTONS}
    assert statuses == {ideas.REVIEWING, ideas.PLANNED, ideas.BUILDING, ideas.SHIPPED, ideas.DECLINED}


def test_duplicate_detection_finds_a_reworded_repeat():
    existing = [
        (1, "Military coup in Namibia after the civil war", "filed"),
        (2, "Zombie outbreak in the Balkans", "shipped"),
    ]
    found = ideas.find_duplicate("A military coup in Namibia once the civil war ends", existing)
    assert found is not None and found.submission_id == 1
    assert "looks like submission #1" in found.line()
    assert ideas.find_duplicate("Canada joins the Antarctic research pact", existing) is None


def test_keywords_ignore_stopwords_and_short_words():
    words = ideas.keywords("An event idea of the country to be added")
    assert "event" not in words and "idea" not in words and "the" not in words and "country" not in words
    assert words == {"added"}


def test_preview_says_nothing_is_saved_yet():
    text = ideas.preview_text(draft="draft body", raw_idea="raw idea")
    assert "Nothing has been saved or posted yet" in text
    assert "Post to forum" in text and "Discard" in text
    duplicate = ideas.Duplicate(4, "Zombie outbreak", "filed", 0.8)
    warned = ideas.preview_text(draft="d", raw_idea="r", duplicate=duplicate, vague_hint="add a trigger")
    assert "Possible duplicate" in warned and "add a trigger" in warned
    assert "Priority review" in ideas.preview_text(draft="d", raw_idea="r", priority=True)


def test_board_summary_talks_about_the_pipeline():
    counts = {ideas.FILED: 2, ideas.PLANNED: 1, ideas.SHIPPED: 3, ideas.DECLINED: 4}
    summary = ideas.board_summary(counts, oldest_open_days=12)
    assert "3 idea(s) in the pipeline" in summary and "oldest 12 days" in summary
    assert "3 already in the mod" in summary
    assert ideas.board_summary({}, oldest_open_days=None) == "No ideas waiting for review right now."


def test_board_line_marks_priority_and_age():
    line = ideas.board_line(
        submission_id=7, title="Zombie outbreak", status=ideas.PLANNED, author="tester", age_days=3, priority=True
    )
    assert "`#7`" in line and "⭐" in line and "3d old" in line and "tester" in line
    old = ideas.board_line(
        submission_id=8, title="Old idea", status=ideas.FILED, author="tester", age_days=75
    )
    assert "2mo old" in old


def test_rate_limit_message_states_the_limit():
    message = ideas.rate_limit_message(recent=5)
    assert "5 ideas this week" in message and "limit (5)" in message


@pytest.mark.asyncio
async def test_storage_round_trip_and_status_log(tmp_path):
    store = Store(tmp_path / "chaosx.db")
    await store.init()
    submission_id = await store.create_idea_submission(
        user_id=42, title="Zombie outbreak", raw_idea="raw", draft="draft", priority=True
    )
    row = await store.idea_submission(submission_id)
    assert row is not None and row["status"] == ideas.FILED and row["priority"] == 1
    assert await store.idea_status_counts() == {ideas.FILED: 1}
    await store.set_idea_status(
        submission_id=submission_id, status=ideas.PLANNED, note="fits the 1948 cluster", reviewer_id=1
    )
    row = await store.idea_submission(submission_id)
    assert row["status"] == ideas.PLANNED and row["reviewer_note"] == "fits the 1948 cluster"
    assert await store.idea_submissions(statuses=ideas.OPEN_STATUSES) != []
    assert await store.idea_submissions(statuses=(ideas.SHIPPED,)) == []
    assert await store.idea_submissions_since(user_id=42, since_iso="2000-01-01") == 1
    assert await store.oldest_open_idea_days() == 0
    await store.set_idea_post_location(
        submission_id=submission_id, channel_id=1, message_id=2, vault_path="/vault/note.md"
    )
    row = await store.idea_submission(submission_id)
    assert row["forum_message_id"] == 2 and row["vault_path"] == "/vault/note.md"
    await store.set_idea_promoted(submission_id=submission_id, promoted_path="/vault/007 - Zombie.md")
    assert (await store.idea_submission(submission_id))["promoted_path"] == "/vault/007 - Zombie.md"


def test_promotion_writes_a_numbered_spec(tmp_path):
    vault = tmp_path / "vault"
    (vault / "Events" / "Event Specs").mkdir(parents=True)
    result = promote_community_idea(
        vault_path=vault,
        event_specs_folder="Events/Event Specs",
        event_id=7,
        title="Zombie outbreak",
        draft="## Catalog entry\n\n- Event ID: `TBD`\n\nSome draft body about zombies.",
        raw_idea="zombies rise in the balkans",
        submission_id=3,
    )
    assert result.created
    text = result.path.read_text()
    assert result.path.name.startswith("007 - ")
    assert "`007`" in text and "`TBD`" not in text  # the identity changed
    assert "submission `#3`" in text and "zombies rise in the balkans" in text
