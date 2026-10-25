"""Owner-only plain-language server management: parsing, validation and the actions themselves."""

import asyncio
from datetime import timedelta

import pytest

from chaosx_bot import server_ops
from chaosx_bot.server_ops import Plan, PlanError, describe, parse_plan, validate


def test_management_phrases_are_recognised_and_questions_are_not():
    assert server_ops.looks_like_management("delete messages up to this message")
    assert server_ops.looks_like_management("timeout @someone for 10 minutes")
    assert server_ops.looks_like_management("create a new channel named ghlgghal")
    assert server_ops.looks_like_management("change permissions in the channel")
    assert not server_ops.looks_like_management("how does the chaos meter work?")
    assert not server_ops.looks_like_management("what are the mcps used for?")


def test_parse_a_plan():
    plans = parse_plan('{"action": "timeout", "args": {"user": "42", "minutes": 30}, "say": "Timeout"}')
    assert isinstance(plans, list) and len(plans) == 1
    assert plans[0].action == "timeout" and plans[0].args["minutes"] == 30
    assert plans[0].needs_confirmation is False


def test_parse_several_actions_in_order():
    raw = (
        '{"actions": [{"action": "purge", "args": {"count": 5}}, '
        '{"action": "timeout", "args": {"user": "42", "minutes": 10}}]}'
    )
    plans = parse_plan(raw)
    assert [plan.action for plan in plans] == ["purge", "timeout"]


def test_a_batch_of_six_is_capped_at_five():
    raw = '{"actions": [' + ", ".join('{"action": "purge"}' for _ in range(6)) + "]}"
    assert len(parse_plan(raw)) == 5


def test_parse_tolerates_a_fence_and_prose():
    raw = 'Sure:\n```json\n{"action": "purge", "args": {"count": 5}}\n```'
    plans = parse_plan(raw)
    assert isinstance(plans, list) and plans[0].action == "purge"


def test_parse_refusals_and_unknown_actions():
    assert parse_plan('{"action": "none"}') == "none"
    assert parse_plan('{"action": "drop_database"}') is None
    assert parse_plan("not json at all") is None


def test_purge_defaults_and_clamps():
    assert validate(Plan("purge", {})).args["count"] == 100
    assert validate(Plan("purge", {"count": 99999})).args["count"] == server_ops.MAX_PURGE
    with pytest.raises(PlanError):
        validate(Plan("purge", {"count": 0}))


def test_timeout_needs_a_duration_and_a_person():
    with pytest.raises(PlanError, match="minutes"):
        validate(Plan("timeout", {"user": "1"}))
    with pytest.raises(PlanError, match="Who"):
        validate(Plan("timeout", {"minutes": 10}))
    with pytest.raises(PlanError):
        validate(Plan("timeout", {"user": "1", "minutes": 999999}))
    assert validate(Plan("timeout", {"user": "1", "minutes": 10})).args["minutes"] == 10


def test_ban_window_is_clamped():
    plan = validate(Plan("ban", {"user": "1", "delete_message_days": 99}))
    assert plan.args["delete_message_days"] == 7


def test_channel_names_are_trimmed_to_discords_limit():
    plan = validate(Plan("create_channel", {"name": "x" * 300}))
    assert len(plan.args["name"]) == 100
    with pytest.raises(PlanError, match="called"):
        validate(Plan("create_channel", {}))


def test_permission_names_are_normalised_and_junk_is_dropped():
    plan = validate(
        Plan(
            "set_channel_permissions",
            {"role": "Members", "allow": ["Send Messages", "view channel"], "deny": ["nonsense"]},
        )
    )
    assert plan.args["allow"] == ["send_messages", "view_channel"]
    assert plan.args["deny"] == []
    with pytest.raises(PlanError, match="permission"):
        validate(Plan("set_channel_permissions", {"role": "Members", "allow": ["nonsense"]}))
    with pytest.raises(PlanError, match="role or a member"):
        validate(Plan("set_channel_permissions", {"allow": ["send_messages"]}))


def test_role_colour_parsing():
    assert validate(Plan("create_role", {"name": "Red", "color": "#ff8800"})).args["color"] == 0xFF8800
    assert validate(Plan("create_role", {"name": "Orange", "color": "orange"})).args["color"] == 0xE67E22
    assert validate(Plan("create_role", {"name": "Red", "color": "not-a-colour"})).args["color"] is None
    plan = validate(Plan("create_role", {"name": "Red", "permissions": ["Manage Messages"]}))
    assert plan.args["permissions"] == ["manage_messages"]


def test_irreversible_actions_need_confirmation():
    for action in ("ban", "kick", "delete_channel", "delete_role", "purge"):
        assert Plan(action, {}).needs_confirmation is True
    for action in ("timeout", "create_channel", "create_role", "lock_channel", "slowmode"):
        assert Plan(action, {}).needs_confirmation is False


def test_describe_all_numbers_a_batch():
    text = server_ops.describe_all([Plan("purge", {"count": 5}), Plan("ban", {"user": "42"})])
    assert text.splitlines()[0] == "1. Delete messages last 5"
    assert text.splitlines()[1] == "2. Ban 42"


def test_describe_reads_like_a_sentence():
    assert describe(Plan("purge", {"count": 20})) == "Delete messages last 20"
    assert "up to message 5" in describe(Plan("purge", {"until_message_id": "5"}))
    assert describe(Plan("ban", {"user": "42"})) == "Ban 42"
    assert describe(Plan("delete_channel", {})) == "Delete channel (this one)"


# --- execution against fakes ------------------------------------------------------------------------


class FakeChannel:
    def __init__(self, name="general"):
        self.name = name
        self.id = 111
        self.purged = []
        self.edits = []
        self.permissions = []
        self.deleted = False

    async def purge(self, **kwargs):
        self.purged.append(kwargs)
        limit = kwargs.get("limit") or 100
        # 250 messages sit between the boundary and the newest one, so the range needs three passes.
        remaining = 250 - sum(len(p.get("_returned", [])) for p in self.purged[:-1])
        returned = list(range(min(limit, max(0, remaining))))
        self.purged[-1]["_returned"] = returned
        return returned

    async def fetch_message(self, message_id):
        channel = self

        class Boundary:
            id = message_id
            deleted = False

            async def delete(self, **_kwargs):
                self.deleted = True
                channel.boundary_deleted = True

        return Boundary()

    async def edit(self, **kwargs):
        self.edits.append(kwargs)

    async def delete(self, **_kwargs):
        self.deleted = True

    async def set_permissions(self, target, overwrite=None, reason=None):
        self.permissions.append((target, overwrite, reason))

    def overwrites_for(self, _target):
        return type("Overwrite", (), {"send_messages": None})()


class FakeMember:
    def __init__(self, name="klim", member_id=42):
        self.name = name
        self.display_name = name
        self.id = member_id
        self.timeouts = []
        self.roles_added = []
        self.roles_removed = []
        self.kicked = False
        self.banned = False

    async def timeout(self, duration, reason=None):
        self.timeouts.append(duration)

    async def add_roles(self, role, reason=None):
        self.roles_added.append(role)

    async def remove_roles(self, role, reason=None):
        self.roles_removed.append(role)

    async def kick(self, reason=None):
        self.kicked = True

    async def ban(self, reason=None, delete_message_days=0):
        self.banned = delete_message_days


class FakeRole:
    def __init__(self, name="Members"):
        self.name = name
        self.id = 7
        self.deleted = False

    def is_default(self):
        return False

    async def delete(self, reason=None):
        self.deleted = True

    managed = False


class FakeGuild:
    def __init__(self):
        self.name = "Test"
        self.members = [FakeMember()]
        self.roles = [FakeRole()]
        self.default_role = FakeRole("@everyone")
        self.channels = [FakeChannel()]
        self.created = []

    def get_member(self, _id):
        return self.members[0]

    async def fetch_member(self, _id):
        return self.members[0]

    def get_role(self, _id):
        return self.roles[0]

    def get_channel_or_thread(self, _id):
        return self.channels[0]

    async def create_role(self, **kwargs):
        self.created.append(("role", kwargs))
        return FakeRole(kwargs["name"])

    async def unban(self, user, reason=None):
        self.created.append(("unban", user.id))


def run(plan, guild=None, channel="default"):
    guild = guild or FakeGuild()
    channel = guild.channels[0] if channel == "default" else channel
    summary = asyncio.run(server_ops.execute(None, plan, guild=guild, channel=channel, author_id=1))
    return summary, guild


def test_execute_purge_without_a_boundary():
    summary, guild = run(validate(Plan("purge", {"count": 3})))
    assert "Deleted 3 message(s)" in summary
    assert guild.channels[0].purged[0]["limit"] == 3


def test_execute_purge_up_to_a_message_walks_the_whole_range():
    summary, guild = run(validate(Plan("purge", {"count": 50, "until_message_id": "123"})))
    assert int(guild.channels[0].purged[0]["after"].id) == 123
    assert "Deleted 251 message(s)" in summary  # 250 in the range plus the linked message itself
    assert sum(len(entry["_returned"]) for entry in guild.channels[0].purged) == 250
    assert getattr(guild.channels[0], "boundary_deleted", False) is True


def test_execute_timeout_and_untimeout():
    summary, guild = run(validate(Plan("timeout", {"user": "42", "minutes": 15})))
    assert guild.members[0].timeouts == [timedelta(minutes=15)]
    assert "15 minute" in summary
    run(validate(Plan("untimeout", {"user": "42"})), guild=guild)
    assert guild.members[0].timeouts[-1] is None


def test_execute_ban_records_the_message_window():
    _summary, guild = run(validate(Plan("ban", {"user": "42", "delete_message_days": 3})))
    assert guild.members[0].banned == 3


def test_execute_lock_and_unlock_the_channel():
    summary, guild = run(validate(Plan("lock_channel", {})))
    assert "Locked" in summary and guild.channels[0].permissions[-1][1].send_messages is False
    summary, guild = run(validate(Plan("unlock_channel", {})), guild=guild)
    assert "Unlocked" in summary


def test_execute_set_channel_permissions_for_a_role():
    summary, guild = run(
        validate(
            Plan(
                "set_channel_permissions",
                {"channel": "general", "role": "Members", "deny": ["send messages"]},
            )
        )
    )
    assert "deny send messages" in summary
    assert isinstance(guild.channels[0].permissions[-1][0], FakeRole)


def test_execute_create_role_and_delete_it():
    summary, guild = run(validate(Plan("create_role", {"name": "Event Team", "color": "#00ff00"})))
    assert "Event Team" in summary and guild.created[0][0] == "role"
    summary, guild = run(validate(Plan("delete_role", {"role": "Members"})), guild=guild)
    assert "Deleted role" in summary


def test_unknown_targets_refuse_instead_of_guessing():
    with pytest.raises(PlanError, match="cannot find a member"):
        run(validate(Plan("timeout", {"user": "nobody-here", "minutes": 5})))
    with pytest.raises(PlanError, match="cannot find a channel"):
        run(validate(Plan("slowmode", {"channel": "no-such-channel", "seconds": 5})))


def test_everyone_can_be_the_permission_target():
    guild = FakeGuild()
    plan = validate(Plan("set_channel_permissions", {"role": "@everyone", "deny": ["send messages"]}))
    summary = asyncio.run(server_ops.execute(None, plan, guild=guild, channel=guild.channels[0], author_id=1))
    assert "@@everyone" not in summary
    assert guild.channels[0].permissions[-1][0] is guild.default_role


def test_a_channel_is_found_by_the_words_hoops_types():
    guild = FakeGuild()
    decorated = FakeChannel("💬┃jazz-and-conversation")
    decorated.id = 555
    guild.channels.append(decorated)
    plan = validate(Plan("purge", {"channel": "jazz and conversation", "count": 10}))
    summary = asyncio.run(server_ops.execute(None, plan, guild=guild, channel=guild.channels[0], author_id=1))
    assert "jazz-and-conversation" in summary
    assert decorated.purged and not guild.channels[0].purged  # purged the named channel, not the current one


def test_channel_tokens_ignore_emoji_and_filler():
    assert server_ops._channel_tokens("💬┃jazz-and-conversation") == {"jazz", "conversation"}
    assert server_ops._channel_tokens("#general-chat") == set()


def test_an_ambiguous_channel_is_refused():
    guild = FakeGuild()
    for name, cid in (("💬┃jazz-and-conversation", 601), ("🎷┃jazz-talk", 602)):
        channel = FakeChannel(name)
        channel.id = cid
        guild.channels.append(channel)
    plan = validate(Plan("slowmode", {"channel": "jazz", "seconds": 5}))
    with pytest.raises(PlanError, match="More than one channel"):
        asyncio.run(server_ops.execute(None, plan, guild=guild, channel=guild.channels[0], author_id=1))
