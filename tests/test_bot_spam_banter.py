"""Spontaneous #bot-spam banter: deterministic scheduling, no spam, no pings."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from chaosx_bot import bot_spam_banter as bsb

NOW = datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


def _history(now=NOW, per_user=4, users=(1, 2, 3)):
    rows = []
    for offset, user in enumerate(users):
        for i in range(per_user):
            rows.append((user, now - timedelta(minutes=20 + i * 5 + offset), f"message {i} from {user}"))
    return rows


def _plan(**overrides):
    args = dict(
        now=NOW,
        history=_history(),
        bot_spam=[(1, NOW - HOUR, "hello"), (2, NOW - timedelta(hours=2), "hi")],
        state={},
        enabled=True,
        quiet_hours="23-10",
        min_gap_minutes=180,
        max_per_day=3,
        target_cooldown_hours=48,
        excluded_ids={999},
        opted_out=set(),
        names={1: "Alpha", 2: "Beta", 3: "Gamma"},
    )
    args.update(overrides)
    return bsb.plan(**args)


def test_posts_when_the_server_has_been_active_and_the_channel_is_calm():
    decision = _plan()
    assert decision.post and decision.target is not None
    assert decision.target.user_id in {1, 2, 3}


def test_disabled_never_posts():
    assert not _plan(enabled=False).post


def test_quiet_hours_block_posting():
    night = datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc)
    assert not _plan(now=night, history=_history(night)).post


def test_gap_between_posts_is_respected():
    decision = _plan(state={"last_post_at": (NOW - timedelta(minutes=30)).isoformat()})
    assert not decision.post and decision.reason == "gap not elapsed"


def test_daily_cap():
    state = {"last_post_at": (NOW - timedelta(hours=8)).isoformat(), "post_days": {NOW.date().isoformat(): ["a", "b", "c"]}}
    assert not _plan(state=state).post


def test_no_post_when_a_conversation_is_going_in_bot_spam():
    busy = [(1, NOW - timedelta(minutes=3), "hey"), (2, NOW - timedelta(minutes=2), "what's up")]
    decision = _plan(bot_spam=busy)
    assert not decision.post and "conversation" in decision.reason


def test_no_post_after_a_serious_message():
    serious = [(1, NOW - timedelta(minutes=4), "my grandfather passed away yesterday")]
    assert not _plan(bot_spam=serious).post


def test_quiet_server_is_left_alone():
    assert not _plan(history=_history(per_user=1, users=(1,))).post


def test_owner_bots_and_opt_outs_are_never_targets():
    history = [(7, NOW - timedelta(minutes=5), "x") for _ in range(6)] + _history(per_user=4, users=(1, 2))
    history += [(3, NOW - timedelta(minutes=10 + i), f"gamma {i}") for i in range(6)]
    decision = _plan(history=history, excluded_ids={7}, opted_out={1, 2})
    assert decision.post and decision.target is not None and decision.target.user_id == 3


def test_same_member_not_targeted_inside_cooldown():
    state = {"targets": {"1": (NOW - timedelta(hours=5)).isoformat(), "2": (NOW - timedelta(hours=5)).isoformat()}}
    decision = _plan(state=state)
    assert decision.post and decision.target is not None and decision.target.user_id == 3


def test_all_targets_cooled_down_means_no_post():
    state = {"targets": {str(uid): (NOW - timedelta(hours=2)).isoformat() for uid in (1, 2, 3)}}
    decision = _plan(state=state)
    assert not decision.post and decision.reason == "no eligible member"


def test_ignored_previous_post_backs_off_for_a_day():
    decision = _plan(last_was_ignored=True)
    assert not decision.post and decision.next_allowed_at == NOW + timedelta(hours=24)


def test_same_inputs_give_the_same_target():
    assert _plan().target == _plan().target


def test_quiet_hours_window_wraps_midnight():
    assert bsb.in_quiet_hours(datetime(2026, 10, 31, 23, 30, tzinfo=timezone.utc), "23-10")
    assert bsb.in_quiet_hours(datetime(2026, 10, 31, 6, 0, tzinfo=timezone.utc), "23-10")
    assert not bsb.in_quiet_hours(datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc), "23-10")
