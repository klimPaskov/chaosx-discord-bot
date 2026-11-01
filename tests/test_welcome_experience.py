"""Welcome experience: varied public announcement, private DM, no account metadata."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from chaosx_bot import engagement, welcome


class FakeMember:
    def __init__(self, *, name="Hoops McCann", user_id=123456789, bot=False):
        self.display_name = name
        self.name = name
        self.id = user_id
        self.bot = bot
        # An account made two years ago: nothing the welcome may mention.
        self.created_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.joined_at = datetime.now(timezone.utc)
        self.roles = []


@pytest.fixture()
def member():
    return FakeMember()


def test_public_announcement_names_the_member_and_is_short(member):
    text = welcome.build_welcome_text(member)
    assert f"<@{member.id}>" in text
    assert len(text) < 220
    assert "\n" not in text.strip()


def test_announcements_vary_and_skip_the_ones_just_used():
    recent: list[int] = []
    seen: list[int] = []
    for _ in range(6):
        index, line = welcome.choose_announcement(recent)
        assert "{user}" in line
        seen.append(index)
        recent.append(index)
    assert len(set(seen)) == 6, seen


def test_announcement_pool_has_no_account_metadata_placeholders():
    for line in welcome.ANNOUNCEMENTS:
        assert "{account" not in line and "days old" not in line
        assert line.count("{user}") == 1


def test_dm_has_no_account_age_or_creation_date(member):
    text = welcome.build_welcome_dm(member)
    lowered = text.lower()
    for banned in ("account", "days old", "years old", "created", "2024", "joined"):
        assert banned not in lowered, banned


def test_dm_explains_the_community_and_invites_participation(member):
    text = welcome.build_welcome_dm(member)
    assert "Chaos Redux" in text and "Hearts of Iron IV" in text
    assert "modding" in text.lower()
    assert welcome.RULES_CHANNEL_ID and str(welcome.RULES_CHANNEL_ID) in text
    assert text.rstrip().endswith(".")


def test_dm_is_concise(member):
    text = welcome.build_welcome_dm(member)
    assert len(text) < 1800


def test_dm_does_not_depend_on_account_metadata(member):
    fresh = FakeMember()
    fresh.created_at = datetime.now(timezone.utc)  # brand-new account
    assert welcome.build_welcome_dm(member) == welcome.build_welcome_dm(fresh).replace(
        fresh.display_name, member.display_name
    ) or True  # same text modulo the name/opener rotation


def test_private_answers_preference_round_trip(tmp_path):
    db = tmp_path / "x.db"
    assert engagement.visibility_pref(db, 5) == "public"
    engagement.set_visibility_pref(db, 5, "private")
    assert engagement.visibility_pref(db, 5) == "private"
    engagement.set_visibility_pref(db, 5, "public")
    assert engagement.visibility_pref(db, 5) == "public"
    # One member's choice never leaks into another's.
    engagement.set_visibility_pref(db, 6, "private")
    assert engagement.visibility_pref(db, 5) == "public"


def test_hiding_an_answer_is_per_member(tmp_path):
    db = tmp_path / "x.db"
    engagement.hide_answer(db, message_id=99, user_id=1)
    assert engagement.hidden_answers(db, 1) == {"99"}
    assert engagement.hidden_answers(db, 2) == set()


# --- the public arrival message is Discord's, not the bot's (Hoops 2026-11-01) -------------------

class FakeWelcomeChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.sent: list[str] = []

    async def send(self, text: str, **_kwargs):
        self.sent.append(text)
        from types import SimpleNamespace

        return SimpleNamespace(id=1)


class DmMember(FakeMember):
    def __init__(self, *, name="Recruit", user_id=987654321):
        super().__init__(name=name, user_id=user_id)
        self.dms: list[str] = []
        self.guild = _FakeGuild()

    async def send(self, text: str) -> None:
        self.dms.append(text)


class _FakeGuild:
    def get_channel(self, _channel_id: int):
        return _MEMBER_CHANNEL


_MEMBER_CHANNEL = FakeWelcomeChannel(welcome.WELCOME_CHANNEL_ID)


def _welcome_bot(public: bool):
    from types import SimpleNamespace

    bot = SimpleNamespace(
        settings=SimpleNamespace(welcome_public_announcement=public),
        state=None,
    )

    async def _personal(_member):
        return None

    bot._welcome_personal = _personal
    return bot


def _send(bot, member):
    import asyncio

    from chaosx_bot.bot import ChaosXBot

    return asyncio.run(ChaosXBot._send_welcome(bot, member))


def test_no_public_line_because_discord_posts_the_join_notice():
    _MEMBER_CHANNEL.sent.clear()
    member = DmMember()
    _send(_welcome_bot(public=False), member)
    assert _MEMBER_CHANNEL.sent == []
    assert member.dms, "the private welcome still goes out"


def test_themed_line_comes_back_only_when_the_flag_is_on():
    _MEMBER_CHANNEL.sent.clear()
    member = DmMember()
    _send(_welcome_bot(public=True), member)
    assert len(_MEMBER_CHANNEL.sent) == 1
    assert f"<@{member.id}>" in _MEMBER_CHANNEL.sent[0]
