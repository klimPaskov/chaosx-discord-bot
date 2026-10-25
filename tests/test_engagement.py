"""Per-member answer settings, feedback storage and the panel's quick-action parsing."""

import json
import sqlite3

from chaosx_bot import engagement
from chaosx_bot.answer_panel import build_answer_panel


def test_default_is_answer_me(tmp_path):
    db = tmp_path / "e.db"
    assert engagement.get_pref(db, 42)["auto_answer_mode"] == "on"
    assert engagement.auto_answer_allows(db, 42) is True


def test_member_can_turn_unprompted_answers_off(tmp_path):
    db = tmp_path / "e.db"
    engagement.set_auto_answer(db, 42, enabled=False)
    assert engagement.auto_answer_allows(db, 42) is False
    assert "off" in engagement.describe_auto_answer(engagement.get_pref(db, 42))


def test_temporary_window_expires_and_reverts(tmp_path):
    db = tmp_path / "e.db"
    engagement.set_auto_answer(db, 42, enabled=True, hours=-1)  # already in the past
    assert engagement.auto_answer_allows(db, 42) is True
    assert engagement.get_pref(db, 42)["auto_answer_mode"] == "on"


def test_permanent_switch_back_on(tmp_path):
    db = tmp_path / "e.db"
    engagement.set_auto_answer(db, 7, enabled=False)
    engagement.set_auto_answer(db, 7, enabled=True)
    assert engagement.auto_answer_allows(db, 7) is True
    assert engagement.get_pref(db, 7)["auto_answer_until"] is None


def test_one_members_setting_does_not_touch_another(tmp_path):
    db = tmp_path / "e.db"
    engagement.set_auto_answer(db, 1, enabled=False)
    assert engagement.auto_answer_allows(db, 2) is True


def test_feedback_is_stored_with_its_note(tmp_path):
    db = tmp_path / "e.db"
    fid = engagement.record_feedback(
        db, user_id=5, rating="down", message_id=99, question="what is the chaos meter?", answer="…"
    )
    assert fid > 0
    assert engagement.add_feedback_comment(db, fid, "it never said how it fills") is True
    summary = engagement.feedback_summary(db, days=7)
    assert "👎" in summary and "how it fills" in summary
    assert engagement.user_feedback_profile(db, 5) == {"up": 0, "down": 1}


def test_up_and_down_are_counted_separately(tmp_path):
    db = tmp_path / "e.db"
    engagement.record_feedback(db, user_id=5, rating="up")
    engagement.record_feedback(db, user_id=5, rating="👍")
    engagement.record_feedback(db, user_id=5, rating="nonsense")  # unknown ratings count as a dislike
    profile = engagement.user_feedback_profile(db, 5)
    assert profile == {"up": 2, "down": 1}


def test_quick_actions_parse_a_fenced_array():
    raw = '```json\n[{"label": "Show the decay rate", "prompt": "How fast does the meter decay?"}]\n```'
    actions = engagement.parse_quick_actions(raw)
    assert actions == [("Show the decay rate", "How fast does the meter decay?")]


def test_quick_actions_ignore_prose_and_cap_at_three():
    raw = (
        "Sure! Here you go:\n"
        + json.dumps([{"label": f"Action {i}", "prompt": f"Prompt {i}"} for i in range(6)])
    )
    actions = engagement.parse_quick_actions(raw, limit=3)
    assert len(actions) == 3
    assert engagement.parse_quick_actions("no json here") == []


def test_quick_actions_dedupe_and_trim_labels():
    raw = json.dumps(
        [
            {"label": "Explain more", "prompt": "Explain more"},
            {"label": "explain more", "prompt": "Explain more please"},
            {"label": "x" * 200, "prompt": "long label"},
        ]
    )
    actions = engagement.parse_quick_actions(raw)
    assert [label for label, _p in actions][0] == "Explain more"
    assert len(actions) == 2
    assert len(actions[1][0]) <= 80


def test_panel_has_the_dynamic_actions_plus_feedback_and_settings():
    view = build_answer_panel(
        bot=None,
        question="what is the chaos meter?",
        answer="It fills as the world ends.",
        user_id=1,
        message_id=2,
        actions=[("Show the stages", "list the stages"), ("How fast?", "how fast does it fill")],
    )
    labels = [getattr(child, "label", None) for child in view.children]
    assert "Show the stages" in labels
    assert "Helpful" in labels and "Not helpful" in labels and "Settings" in labels


def test_dismiss_prompt_for_a_while_or_forever(tmp_path):
    db = tmp_path / "e.db"
    assert engagement.feedback_prompt_allows(db, 9) is True
    engagement.dismiss_feedback_prompt(db, 9, hours=24)
    assert "paused" in engagement.describe_feedback_prompt(db, 9)
    engagement.dismiss_feedback_prompt(db, 9, hours=None)
    assert engagement.feedback_prompt_allows(db, 9) is False
    assert engagement.describe_feedback_prompt(db, 9).startswith("off")


def test_rating_the_same_message_twice_keeps_one_row(tmp_path):
    db = tmp_path / "e.db"
    first = engagement.record_feedback(db, user_id=3, rating="up", message_id=77)
    second = engagement.record_feedback(db, user_id=3, rating="up", message_id=77)
    assert first == second and first > 0
    other = engagement.record_feedback(db, user_id=3, rating="down", message_id=77)
    assert other != first
    assert engagement.message_rating(db, 77, 3) == "down"


def test_stored_turn_survives_a_missing_table(tmp_path):
    db = tmp_path / "empty.db"
    assert engagement.stored_turn(db, 123) == ("", "")


def test_member_answer_context_reflects_their_choices(tmp_path):
    db = tmp_path / "e.db"
    assert engagement.member_answer_context(db, 11) == ""
    engagement.set_detail_level(db, 11, "concise")
    context = engagement.member_answer_context(db, 11)
    assert "short answers" in context
    engagement.record_feedback(db, user_id=11, rating="up")
    engagement.record_feedback(db, user_id=11, rating="down")
    context = engagement.member_answer_context(db, 11)
    assert "1 helpful, 1 not helpful" in context


class _FakeInteraction:
    def __init__(self):
        self.deferred = {}
        self.sent = []
        self.user = type("U", (), {"id": 1})()
        self.response = self
        self.followup = self

    async def defer(self, **kwargs):
        self.deferred.update(kwargs)

    async def send_message(self, *a, **kw):
        self.sent.append(kw)

    async def send(self, *a, **kw):
        self.sent.append(kw)


def test_quick_action_answers_in_the_channel(monkeypatch):
    import asyncio

    from chaosx_bot import answer_panel

    async def fake_followup(*_a, **_kw):
        return "More detail here."

    monkeypatch.setattr(answer_panel, "followup_answer", fake_followup)
    view = build_answer_panel(
        bot=None,
        question="what is the chaos meter?",
        answer="It fills.",
        user_id=1,
        message_id=2,
        actions=[("Explain the stages", "explain the stages")],
    )
    button = next(child for child in view.children if getattr(child, "label", None) == "Explain the stages")
    interaction = _FakeInteraction()
    asyncio.run(button.callback(interaction))
    assert interaction.deferred == {"thinking": False}
    assert interaction.sent and all("ephemeral" not in kw for kw in interaction.sent), interaction.sent


def test_dismiss_controls_use_the_red_cross():
    from chaosx_bot.answer_panel import DismissView, SettingsView

    panel = build_answer_panel(
        bot=None, question="q", answer="a", user_id=1, message_id=2,
        actions=[("One", "one?")], show_feedback=True,
    )
    hide = next(child for child in panel.children if getattr(child, "label", None) == "Hide feedback")
    assert str(hide.emoji) == "❌"
    settings = SettingsView(None, user_id=1)
    off_week = next(child for child in settings.children if getattr(child, "label", None) == "Off for a week")
    assert str(off_week.emoji) == "❌"
    dismiss = DismissView(None, user_id=1)
    forever = next(child for child in dismiss.children if getattr(child, "label", None) == "Hide permanently")
    assert str(forever.emoji) == "❌"
