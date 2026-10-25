"""Per-member answer settings, feedback storage and the panel's quick-action parsing."""

import asyncio
import json
import sqlite3

from chaosx_bot import engagement
from chaosx_bot.answer_panel import build_answer_panel


def asyncio_run(coro):
    return asyncio.run(coro)


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


class _FakeResponse(_FakeInteraction):
    def __init__(self):
        super().__init__()
        self.modal = None
        self.followup = self
        self.response = self

    async def send_modal(self, modal):
        self.modal = modal

    async def edit_message(self, **kwargs):
        self.sent.append(kwargs)


def _bot_with_db(tmp_path):
    from types import SimpleNamespace

    return SimpleNamespace(settings=SimpleNamespace(db_path=tmp_path / "e.db"))


def test_pressing_helpful_opens_a_private_box_and_stores_the_thumb(tmp_path):
    bot = _bot_with_db(tmp_path)
    view = build_answer_panel(bot=bot, question="what is the chaos meter?", answer="It fills.", user_id=1, message_id=99)
    like = next(child for child in view.children if getattr(child, "label", None) == "Helpful")
    interaction = _FakeResponse()
    asyncio_run(like.callback(interaction))
    assert interaction.modal is not None
    assert interaction.modal.title == "What was helpful?"
    assert interaction.modal.note.required is False  # they can submit it empty
    assert engagement.message_rating(bot.settings.db_path, 99, 1) == "up"


def test_pressing_not_helpful_asks_what_went_wrong(tmp_path):
    bot = _bot_with_db(tmp_path)
    view = build_answer_panel(bot=bot, question="q", answer="a", user_id=1, message_id=100)
    dislike = next(child for child in view.children if getattr(child, "label", None) == "Not helpful")
    interaction = _FakeResponse()
    asyncio_run(dislike.callback(interaction))
    assert interaction.modal.title == "What was wrong?"
    assert "wrong or missing" in interaction.modal.note.placeholder


def test_the_box_saves_the_explanation_and_thanks_privately(tmp_path):
    bot = _bot_with_db(tmp_path)
    view = build_answer_panel(bot=bot, question="q", answer="a", user_id=1, message_id=101)
    dislike = next(child for child in view.children if getattr(child, "label", None) == "Not helpful")
    interaction = _FakeResponse()
    asyncio_run(dislike.callback(interaction))
    modal = interaction.modal
    modal.note._value = "it named a file I could not find"
    submit = _FakeResponse()
    asyncio_run(modal.on_submit(submit))
    assert submit.sent and submit.sent[-1].get("ephemeral") is True
    summary = engagement.feedback_summary(bot.settings.db_path, days=7)
    assert "it named a file I could not find" in summary


def test_an_empty_explanation_is_fine(tmp_path):
    bot = _bot_with_db(tmp_path)
    view = build_answer_panel(bot=bot, question="q", answer="a", user_id=1, message_id=102)
    like = next(child for child in view.children if getattr(child, "label", None) == "Helpful")
    interaction = _FakeResponse()
    asyncio_run(like.callback(interaction))
    modal = interaction.modal
    modal.note._value = ""
    submit = _FakeResponse()
    asyncio_run(modal.on_submit(submit))
    assert engagement.message_rating(bot.settings.db_path, 102, 1) == "up"


def test_the_owner_can_see_who_rates_and_what_they_said(tmp_path):
    db = tmp_path / "e.db"
    first = engagement.record_feedback(db, user_id=77, rating="up", question="q1")
    engagement.add_feedback_comment(db, first, "clear and short")
    engagement.record_feedback(db, user_id=88, rating="down", question="q2")
    summary = engagement.feedback_summary(db, days=30)
    assert "Who rates me:" in summary
    assert "<@77> — 👍 1 · 👎 0" in summary
    assert "clear and short" in summary
