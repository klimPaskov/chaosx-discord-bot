"""Durable member memory, knowledge gaps, and evidence cases."""

import asyncio
import sqlite3

from chaosx_bot import knowledge_gaps, member_memory, moderation_cases

PROFILE = """User profile for Holly (memory about this community member):
**Preferences**
- Prefers short answers and dislikes walls of text.
- Wants more event content added to the mod.
**Notable facts**
- Works as a software engineer and referees tournaments at weekends.
"""


def test_profile_becomes_facts_and_survives_a_rewrite(tmp_path):
    db = tmp_path / "m.db"
    assert member_memory.absorb_profile(db, 7, PROFILE) >= 3
    facts = member_memory.durable_facts(db, 7)
    assert any("short answers" in fact for fact in facts)
    # a later, thinner profile must not erase what was learned
    member_memory.absorb_profile(db, 7, "User profile for Holly: **Summary**\n- Likes events.")
    facts = member_memory.durable_facts(db, 7)
    assert any("short answers" in fact for fact in facts)


def test_seeing_a_fact_again_raises_its_rank(tmp_path):
    db = tmp_path / "m.db"
    member_memory.absorb_profile(db, 9, "Preferences\n- Plays with a small group on weekends.")
    member_memory.absorb_profile(db, 9, "Preferences\n- Plays with a small group on weekends.")
    conn = sqlite3.connect(db)
    hits = conn.execute("SELECT hits FROM user_memory_facts WHERE author_id=9").fetchone()[0]
    assert hits == 2


def test_compaction_keeps_the_table_bounded(tmp_path):
    db = tmp_path / "m.db"
    for index in range(30):
        member_memory.absorb_profile(db, 11, f"Facts\n- Distinct detail number {index} about the member here.")
    removed = member_memory.compact(db, 11, keep=5)
    remaining = sqlite3.connect(db).execute("SELECT COUNT(*) FROM user_memory_facts WHERE author_id=11").fetchone()[0]
    assert remaining == 5 and removed > 0


def test_forget_removes_profile_and_facts(tmp_path):
    db = tmp_path / "m.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE user_profiles (author_id INTEGER, author_name TEXT, profile TEXT)")
    conn.execute("INSERT INTO user_profiles VALUES (7, 'Holly', 'something')")
    conn.commit()
    member_memory.absorb_profile(db, 7, PROFILE)
    removed = member_memory.forget(db, 7)
    assert removed >= 3
    assert member_memory.durable_facts(db, 7) == []
    assert conn.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0] == 0


def test_three_dislikes_open_one_gap(tmp_path):
    db = tmp_path / "k.db"
    assert knowledge_gaps.record_dislike(db, question="how does the chaos meter work?") is None
    assert knowledge_gaps.record_dislike(db, question="where is the chaos meter configured?") is None
    fired = knowledge_gaps.record_dislike(db, question="chaos meter thresholds again?")
    assert fired and fired["topic"] == "meter"
    gaps = knowledge_gaps.open_gaps(db)
    assert gaps and gaps[0][0] == "meter" and gaps[0][1] >= 3
    assert knowledge_gaps.resolve(db, "meter") is True
    assert all(topic != "meter" for topic, _count, _sample in knowledge_gaps.open_gaps(db))


class FakeMsg:
    def __init__(self, message_id, content):
        self.id = message_id
        self.content = content
        self.created_at = __import__("datetime").datetime(2026, 10, 25, tzinfo=__import__("datetime").timezone.utc)
        self.author = type("A", (), {"display_name": "member", "id": 5})()


class FakeChannel:
    def __init__(self, messages):
        self.id = 111
        self._messages = messages
        self.guild = type("G", (), {"id": 999})()

    def history(self, limit=100, after=None, oldest_first=False):
        channel = self

        class It:
            def __aiter__(self):
                self._it = iter(channel._messages[:limit])
                return self

            async def __anext__(self):
                try:
                    return next(self._it)
                except StopIteration:
                    raise StopAsyncIteration

        return It()


def test_evidence_is_saved_before_the_messages_go(tmp_path):
    db = tmp_path / "c.db"
    channel = FakeChannel([FakeMsg(1, "first"), FakeMsg(2, "second")])
    messages = asyncio.run(moderation_cases.snapshot_messages(channel))
    case_id = moderation_cases.save_case(
        db, actor_id=1, guild_id=999, channel_id=111, action="purge", summary="delete 2", messages=messages
    )
    assert case_id > 0
    stored = moderation_cases.case_messages(db, case_id)
    assert [m["id"] for m in stored] == ["1", "2"]
    assert stored[0]["jump"].startswith("https://discord.com/channels/999/111/")
    assert moderation_cases.latest_cases(db)[0][3] == 2
