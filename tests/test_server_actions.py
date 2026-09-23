"""Owner-requested server actions: plan parsing, validation, and execution branches."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from chaosx_bot.bot import ChaosXBot
from chaosx_bot.server_actions import (
    ACTION_SPECS,
    ACTIONS,
    ActionPlan,
    build_plan_prompt,
    describe_plan,
    parse_action_plan,
    plan_detail,
    plan_id_for,
    unresolvable_params,
)


class FakeChannel:
    def __init__(self, name: str, channel_id: int = 1) -> None:
        self.name = name
        self.id = channel_id
        self.sent: list[tuple[str, dict]] = []
        self.edited: list[dict] = []

    async def send(self, text: str, **kwargs):
        self.sent.append((text, kwargs))
        return SimpleNamespace(id=999, jump_url=f"https://discord.com/channels/1/{self.id}/999")

    async def edit(self, **kwargs):
        self.edited.append(kwargs)
        return self

    async def create_thread(self, *, name: str, **kwargs):
        thread = SimpleNamespace(id=555, mention="<#555>", name=name, sent=[], **{})
        self.sent.append((f"thread:{name}", kwargs))
        return thread

    async def fetch_message(self, message_id: int):
        async def _pin(reason: str = "") -> bool:
            return True

        return SimpleNamespace(
            id=message_id,
            jump_url=f"https://discord.com/channels/1/{self.id}/{message_id}",
            pin=_pin,
        )


def _self(guild) -> SimpleNamespace:
    def resolve_channel(_guild, name):
        key = str(name or "").strip().lstrip("#").lower()
        return next(
            (c for c in guild.channels if c.name.lower() == key),
            None,
        )

    return SimpleNamespace(
        guilds=[guild],
        _resolve_channel=resolve_channel,
    )


def test_parse_action_plan_happy_path():
    raw = json.dumps(
        {"action": "update_channel_topic", "params": {"channel": "event-ideas", "topic": "Post event ideas here"}, "reason": "asked"}
    )
    plan, error = parse_action_plan(raw, request="set the topic")
    assert error == ""
    assert plan is not None
    assert plan.action == "update_channel_topic"
    assert plan.params == {"channel": "event-ideas", "topic": "Post event ideas here"}
    assert plan.label == "Update a channel topic"


def test_parse_action_plan_tolerates_fences_and_prose():
    raw = 'Here is the plan:\n```json\n{"action": "post_message", "params": {"channel": "general", "text": "hi"}}\n```\n'
    plan, error = parse_action_plan(raw, request="say hi")
    assert plan is not None and error == ""
    assert plan.params["text"] == "hi"


def test_parse_action_plan_rejects_unknown_action_and_missing_params():
    plan, error = parse_action_plan('{"action": "ban_member", "params": {"member": "x"}}')
    assert plan is None and "unsupported action" in error
    plan, error = parse_action_plan('{"action": "update_channel_topic", "params": {"channel": "general"}}')
    assert plan is None and "topic" in error
    plan, error = parse_action_plan('{"action": "", "params": {}, "reason": "unsupported"}')
    assert plan is None and "no action" in error
    plan, error = parse_action_plan("not json at all")
    assert plan is None and "JSON" in error
    plan, error = parse_action_plan('{"action": "create_thread", "params": {"channel": "general", "name": 5}}')
    assert plan is not None and plan.params["name"] == "5"


def test_parse_action_plan_strips_pings_from_text_params():
    raw = json.dumps(
        {"action": "post_message", "params": {"channel": "general", "text": "hey @everyone look"}}
    )
    plan, _ = parse_action_plan(raw)
    assert plan is not None
    assert "@everyone" not in plan.params["text"]


def test_parse_action_plan_int_param_validation():
    plan, error = parse_action_plan(
        '{"action": "create_scheduled_event", "params": {"name": "T", "start": "2026-09-25T17:00:00+00:00", "duration_minutes": "ninety"}}'
    )
    assert plan is None and "whole number" in error


def test_plan_id_is_stable_and_detail_round_trips():
    a = plan_id_for(request="open a thread", action="create_thread", params={"channel": "general", "name": "x"})
    b = plan_id_for(request="open a thread", action="create_thread", params={"channel": "general", "name": "x"})
    assert a == b and a.startswith("plan-")
    plan = ActionPlan(plan_id=a, action="create_thread", params={"channel": "general", "name": "x"}, reason="r", request="q")
    assert json.loads(plan_detail(plan))["params"]["name"] == "x"


def test_describe_plan_and_unresolvable_params():
    plan = ActionPlan(
        plan_id="plan-1", action="post_message", params={"channel": "event-ideas", "text": "hi"}, reason="asked"
    )
    resolvers = {"channel": {"event-ideas": "42"}, "member": {}, "role": {}}
    text = describe_plan(plan, resolvers=resolvers)
    assert "#event-ideas" in text and "why: asked" in text
    assert "#42" not in text  # show the channel name, not the resolved id
    assert unresolvable_params(plan, resolvers=resolvers) == []
    missing = ActionPlan(plan_id="plan-2", action="post_message", params={"channel": "nope", "text": "hi"})
    assert unresolvable_params(missing, resolvers=resolvers)


def test_build_plan_prompt_lists_actions_and_names():
    prompt = build_plan_prompt(
        request="open a thread in event-ideas",
        channel_names=["event-ideas", "general"],
        role_names=["Tester"],
        member_names=["Hoops McCann"],
    )
    assert "create_thread" in prompt and "#event-ideas" not in prompt
    assert "event-ideas" in prompt and "Tester" in prompt
    assert "ONLY a JSON object" in prompt
    assert "@everyone" in prompt  # instruction to avoid pings


def test_action_specs_are_non_destructive():
    assert {spec.name for spec in ACTION_SPECS} == {
        "create_scheduled_event",
        "update_channel_topic",
        "create_thread",
        "post_message",
        "pin_message",
        "grant_role",
        "revoke_role",
    }
    assert not any(
        word in spec.name for spec in ACTION_SPECS for word in ("ban", "kick", "delete", "purge")
    )


@pytest.mark.asyncio
async def test_execute_post_message_and_topic():
    channel = FakeChannel("event-ideas", 42)
    guild = SimpleNamespace(channels=[channel], name="Chaos Redux")
    fake_self = _self(guild)
    plan = ActionPlan(plan_id="plan-1", action="post_message", params={"channel": "event-ideas", "text": "hello"})
    ok, summary = await ChaosXBot._execute_action_plan(fake_self, plan)
    assert ok is True
    assert channel.sent and channel.sent[0][0] == "hello"
    assert "discord.com/channels/1/42/999" in summary

    plan = ActionPlan(
        plan_id="plan-2", action="update_channel_topic", params={"channel": "event-ideas", "topic": "Ideas here"}
    )
    ok, summary = await ChaosXBot._execute_action_plan(fake_self, plan)
    assert ok is True and channel.edited and channel.edited[0]["topic"] == "Ideas here"


@pytest.mark.asyncio
async def test_execute_missing_channel_fails_cleanly():
    guild = SimpleNamespace(channels=[], name="Chaos Redux")
    plan = ActionPlan(plan_id="plan-3", action="post_message", params={"channel": "ghost", "text": "hi"})
    ok, summary = await ChaosXBot._execute_action_plan(_self(guild), plan)
    assert ok is False and "not found" in summary


@pytest.mark.asyncio
async def test_execute_unsupported_action_is_refused():
    guild = SimpleNamespace(channels=[], name="Chaos Redux")
    plan = ActionPlan(plan_id="plan-4", action="delete_channel", params={})
    ok, summary = await ChaosXBot._execute_action_plan(_self(guild), plan)
    assert ok is False and "unsupported" in summary
