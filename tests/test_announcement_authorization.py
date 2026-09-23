"""The announcements channel is owner-only and @everyone-directed; automation can never reach it."""

from __future__ import annotations

import types
from types import SimpleNamespace

import pytest

from chaosx_bot.announcements import AnnouncementResult
from chaosx_bot.auth import announcement_mentions, safe_allowed_mentions
from chaosx_bot.bot import ChaosXBot
from chaosx_bot.routine_posts import DEV_DIGEST
from chaosx_bot.server_actions import ActionPlan

ANNOUNCEMENTS = 1395467364916662392
CONTENT_DUMP = 1516054706286235768


class FakeChannel:
    def __init__(self, channel_id: int, name: str = "announcements") -> None:
        self.id = channel_id
        self.name = name
        self.sent: list[tuple[str, dict]] = []

    async def send(self, text: str, **kwargs):
        self.sent.append((text, kwargs))
        return SimpleNamespace(id=4242, jump_url=f"https://discord.com/channels/1/{self.id}/4242")


def _fake_self(channel: FakeChannel) -> SimpleNamespace:
    fake = SimpleNamespace(
        settings=SimpleNamespace(announcements_channel_id=ANNOUNCEMENTS),
        get_channel=lambda channel_id: channel if int(channel_id) == channel.id else None,
    )
    fake._reserved_channel_reason = types.MethodType(
        ChaosXBot._reserved_channel_reason, fake
    )
    return fake


def test_announcement_mentions_is_the_only_ping_path():
    allowed = announcement_mentions()
    assert allowed.everyone is True
    assert allowed.users is False and allowed.roles is False and allowed.replied_user is False
    safe = safe_allowed_mentions()
    assert safe.everyone is False


@pytest.mark.asyncio
async def test_authorized_announcement_pings_everyone_once():
    channel = FakeChannel(ANNOUNCEMENTS)
    result = AnnouncementResult(
        announcement_id="ann-1", status="draft", body="**Chaos Redux 0.1 is out**\nDetails inside."
    )
    outcome = await ChaosXBot._deliver_announcement(
        _fake_self(channel), result, channel_id=ANNOUNCEMENTS, authorized_by=789502982122373150
    )
    assert outcome.status == "posted"
    assert channel.sent, "expected an announcement send"
    text, kwargs = channel.sent[0]
    assert text.startswith("@everyone\n\n")
    assert kwargs["allowed_mentions"].everyone is True


@pytest.mark.asyncio
async def test_unauthorized_delivery_does_not_ping():
    channel = FakeChannel(ANNOUNCEMENTS)
    result = AnnouncementResult(announcement_id="ann-2", status="draft", body="plain text")
    await ChaosXBot._deliver_announcement(_fake_self(channel), result, channel_id=ANNOUNCEMENTS)
    text, kwargs = channel.sent[0]
    assert "@everyone" not in text
    assert kwargs["allowed_mentions"].everyone is False


@pytest.mark.asyncio
async def test_authorized_announcement_does_not_double_ping():
    channel = FakeChannel(ANNOUNCEMENTS)
    result = AnnouncementResult(announcement_id="ann-3", status="draft", body="@everyone already there")
    await ChaosXBot._deliver_announcement(
        _fake_self(channel), result, channel_id=ANNOUNCEMENTS, authorized_by=1
    )
    text, _ = channel.sent[0]
    assert text.count("@everyone") == 1


def test_reserved_channel_reason_only_blocks_the_announcements_channel():
    fake = _fake_self(FakeChannel(ANNOUNCEMENTS))
    reason = fake._reserved_channel_reason(ANNOUNCEMENTS)
    assert reason and "owner-authorized announcements" in reason
    assert fake._reserved_channel_reason(CONTENT_DUMP) is None
    assert fake._reserved_channel_reason(None) is None
    unset = SimpleNamespace(settings=SimpleNamespace(announcements_channel_id=None))
    unset._reserved_channel_reason = types.MethodType(ChaosXBot._reserved_channel_reason, unset)
    assert unset._reserved_channel_reason(ANNOUNCEMENTS) is None


@pytest.mark.asyncio
async def test_routine_post_is_refused_in_the_announcements_channel():
    channel = FakeChannel(ANNOUNCEMENTS)
    fake = _fake_self(channel)
    fake._routine_post_destination = lambda spec: ANNOUNCEMENTS
    result = await ChaosXBot._deliver_routine_post(
        fake, DEV_DIGEST, text="weekly dev digest body", preview=False
    )
    assert result.action == "error"
    assert "refused" in result.detail
    assert channel.sent == [], "nothing may be posted into the announcements channel by automation"


@pytest.mark.asyncio
async def test_owner_dm_routine_post_ignores_the_reserved_check():
    """The intel digest is a DM; the reserved channel only governs channel destinations."""
    dm = FakeChannel(0, name="dm")

    async def _dm_channel():
        return dm

    fake = _fake_self(dm)
    fake._routine_post_destination = lambda spec: ANNOUNCEMENTS
    fake._owner_dm_channel = _dm_channel
    from chaosx_bot.routine_posts import SERVER_INTEL

    result = await ChaosXBot._deliver_routine_post(
        fake, SERVER_INTEL, text="intel digest body", preview=False
    )
    assert result.action == "posted"
    assert dm.sent and "@everyone" not in dm.sent[0][0]


@pytest.mark.asyncio
async def test_admin_do_cannot_post_into_the_announcements_channel():
    channel = FakeChannel(ANNOUNCEMENTS)
    guild = SimpleNamespace(channels=[channel], name="Chaos Redux")
    fake = _fake_self(channel)
    fake.guilds = [guild]
    fake._resolve_channel = lambda _guild, name: channel
    plan = ActionPlan(
        plan_id="plan-1", action="post_message", params={"channel": "announcements", "text": "hi"}
    )
    ok, summary = await ChaosXBot._execute_action_plan(fake, plan)
    assert ok is False and "refused" in summary
    assert channel.sent == []


@pytest.mark.asyncio
async def test_admin_do_still_posts_in_normal_channels():
    channel = FakeChannel(CONTENT_DUMP, name="content-dump")
    guild = SimpleNamespace(channels=[channel], name="Chaos Redux")
    fake = _fake_self(channel)
    fake.guilds = [guild]
    fake._resolve_channel = lambda _guild, name: channel
    plan = ActionPlan(
        plan_id="plan-2", action="post_message", params={"channel": "content-dump", "text": "hi"}
    )
    ok, summary = await ChaosXBot._execute_action_plan(fake, plan)
    assert ok is True and channel.sent
    assert channel.sent[0][1]["allowed_mentions"].everyone is False
