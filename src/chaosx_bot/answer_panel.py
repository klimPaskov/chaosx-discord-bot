"""The private panel that follows a public answer: quick actions, 👍/👎 feedback and per-user settings.

Hoops, 2026-10-24: answers should offer quick actions "generated dynamically using the model itself", a
👍/👎 with an optional note that is saved, and a way for a member to control whether ChaosX answers them
unprompted (and for how long).

Everything a member presses here is ephemeral: the public channel keeps the answer, the tinkering stays
private.
"""

from __future__ import annotations

import asyncio
from typing import Any

import discord

from . import engagement
from .ask_api import direct_chat_completion_stream
from .hermes_bridge import PUBLIC_ASK_BOUNDARY, build_public_prompt

_MAX_BUTTONS = 3

_ACTIONS_SYSTEM = (
    "You suggest follow-up buttons for a Discord knowledge bot answer about the Hearts of Iron IV mod "
    "Chaos Redux and its modding tooling. Reply with a JSON array and nothing else. Each element is "
    '{"label": "<= 40 chars, imperative", "prompt": "the question the member would then be asking"}. '
    "Between 2 and 3 elements. They must be short, specific to this question and answer, and things the bot "
    "can actually answer from the same documentation. No numbering, no markdown, no commentary."
)

_FALLBACK_ACTIONS = [
    ("Explain it further", "Explain that in more detail."),
    ("Give me an example", "Show me a concrete example of that."),
    ("What can go wrong?", "What are the common problems with that?"),
]


def _settings_of(bot: Any):
    return getattr(bot, "settings", None)


def _db_path(bot: Any):
    settings = _settings_of(bot)
    return getattr(settings, "db_path", None)


def _split(text: str, limit: int = 1900) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


async def _complete(bot: Any, *, system: str, prompt: str, label: str) -> str:
    settings = _settings_of(bot)
    chunks: list[str] = []
    async for _reasoning, content in direct_chat_completion_stream(
        system=system,
        user=prompt,
        model=getattr(settings, "ask_model", "") or "",
        provider=getattr(settings, "ask_provider", None) or None,
    ):
        if content:
            chunks.append(content)
    text = "".join(chunks).strip()
    if not text:
        print(f"[panel] {label}: empty completion", flush=True)
    return text


async def suggest_actions(bot: Any, *, question: str, answer: str) -> list[tuple[str, str]]:
    """Ask the model for this answer's own follow-up buttons, falling back to generic ones."""
    prompt = f"Question: {question}\n\nAnswer given:\n{answer[:2500]}\n\nJSON array:"
    try:
        raw = await asyncio.wait_for(_complete(bot, system=_ACTIONS_SYSTEM, prompt=prompt, label="actions"), timeout=25)
    except Exception as exc:  # a failed suggestion must never affect the answer
        print(f"[panel] quick actions failed: {type(exc).__name__}: {exc}", flush=True)
        return list(_FALLBACK_ACTIONS[: _MAX_BUTTONS])
    actions = engagement.parse_quick_actions(raw, limit=_MAX_BUTTONS)
    return actions or list(_FALLBACK_ACTIONS[: _MAX_BUTTONS])


async def followup_answer(bot: Any, *, question: str, answer: str, prompt: str) -> str:
    """Answer a quick-action press in more depth, with the same grounding as the original answer."""
    knowledge = getattr(bot, "knowledge", None)
    context = ""
    if knowledge is not None:
        try:
            anchors = []
            from .auto_scan import specific_domain_terms

            anchors = specific_domain_terms(f"{question} {prompt}")
            try:
                context = str(knowledge.public_ask_context(f"{question} {prompt}", anchors=anchors))
            except TypeError:
                context = str(knowledge.public_ask_context(f"{question} {prompt}"))
        except Exception as exc:
            print(f"[panel] follow-up context failed: {type(exc).__name__}: {exc}", flush=True)
    full_prompt = build_public_prompt(
        user_request=prompt,
        guild_name=None,
        channel_name=None,
        reference_context=(
            f"The member was already answered this, in this conversation:\nQuestion: {question}\n"
            f"Answer: {answer[:1500]}\n\nMore material:\n{context}"
        ),
        source_paths_allowed=False,
    )
    reply = await _complete(bot, system=PUBLIC_ASK_BOUNDARY, prompt=full_prompt, label="follow-up")
    return reply or "I could not reach my knowledge base just now — ask me again in a moment."


class FeedbackNoteModal(discord.ui.Modal, title="What was wrong?"):
    def __init__(self, bot: Any, *, feedback_id: int, user_id: int) -> None:
        super().__init__()
        self.bot = bot
        self.feedback_id = feedback_id
        self.user_id = user_id
        self.note = discord.ui.TextInput(
            label="Your note",
            style=discord.TextStyle.paragraph,
            placeholder="What did I get wrong, or what would have helped?",
            max_length=1000,
            required=True,
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:  # pragma: no cover - Discord only
        db_path = _db_path(self.bot)
        saved = False
        if db_path:
            try:
                saved = engagement.add_feedback_comment(db_path, self.feedback_id, str(self.note.value))
            except Exception as exc:
                print(f"[panel] feedback note failed: {type(exc).__name__}: {exc}", flush=True)
        await interaction.response.send_message(
            "Thanks — saved. It goes into the review queue." if saved else "I could not save that note.",
            ephemeral=True,
        )


class FeedbackThanksView(discord.ui.View):
    def __init__(self, bot: Any, *, feedback_id: int, user_id: int) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        self.feedback_id = feedback_id
        self.user_id = user_id

    @discord.ui.button(label="Add a note", emoji="📝", style=discord.ButtonStyle.secondary)
    async def add_note(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:  # pragma: no cover
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("That is not your feedback.", ephemeral=True)
            return
        await interaction.response.send_modal(
            FeedbackNoteModal(self.bot, feedback_id=self.feedback_id, user_id=self.user_id)
        )


class SettingsView(discord.ui.View):
    """Per-user control over unprompted answers."""

    def __init__(self, bot: Any, *, user_id: int) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        self.user_id = user_id

    async def _apply(self, interaction: discord.Interaction, *, enabled: bool, hours: float | None) -> None:
        db_path = _db_path(self.bot)
        if not db_path:
            await interaction.response.send_message("I cannot reach my settings store.", ephemeral=True)
            return
        pref = engagement.set_auto_answer(db_path, self.user_id, enabled=enabled, hours=hours)
        await interaction.response.send_message(
            f"Saved. Unprompted answers: **{engagement.describe_auto_answer(pref)}**",
            ephemeral=True,
            view=self,
        )

    @discord.ui.button(label="Answer me automatically", style=discord.ButtonStyle.success)
    async def always(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._apply(interaction, enabled=True, hours=None)

    @discord.ui.button(label="Only when I ask", style=discord.ButtonStyle.primary)
    async def off(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._apply(interaction, enabled=False, hours=None)

    @discord.ui.button(label="For 24 hours", style=discord.ButtonStyle.secondary)
    async def day(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._apply(interaction, enabled=True, hours=24)

    @discord.ui.button(label="Short answers, please", style=discord.ButtonStyle.secondary)
    async def short(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        db_path = _db_path(self.bot)
        if not db_path:
            await interaction.response.send_message("I cannot reach my settings store.", ephemeral=True)
            return
        engagement.set_detail_level(db_path, self.user_id, "concise")
        await interaction.response.send_message(
            "Saved — I will keep your answers short.", ephemeral=True, view=self
        )

    @discord.ui.button(label="Detail is fine", style=discord.ButtonStyle.secondary)
    async def detailed(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        db_path = _db_path(self.bot)
        if not db_path:
            await interaction.response.send_message("I cannot reach my settings store.", ephemeral=True)
            return
        engagement.set_detail_level(db_path, self.user_id, "detailed")
        await interaction.response.send_message(
            "Saved — I will explain the reasoning and the steps.", ephemeral=True, view=self
        )

    @discord.ui.button(label="Off for a week", style=discord.ButtonStyle.danger)
    async def week_off(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        db_path = _db_path(self.bot)
        if not db_path:
            await interaction.response.send_message("I cannot reach my settings store.", ephemeral=True)
            return
        engagement.set_auto_answer(db_path, self.user_id, enabled=False)
        await interaction.response.send_message(
            "Saved. I will stay quiet unless you mention me — press **Answer me automatically** whenever you "
            "want the unprompted answers back.",
            ephemeral=True,
            view=self,
        )


class DismissView(discord.ui.View):
    """How long to hide the feedback prompt for; rating with the emoji keeps working either way."""

    def __init__(self, bot: Any, *, user_id: int) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.user_id = user_id

    async def _apply(self, interaction: discord.Interaction, hours: float | None, label: str) -> None:
        db_path = _db_path(self.bot)
        if not db_path:
            await interaction.response.send_message("I cannot reach my settings store.", ephemeral=True)
            return
        engagement.dismiss_feedback_prompt(db_path, self.user_id, hours=hours)
        await interaction.response.send_message(
            f"Okay — {label}. You can still rate any answer with 👍 or 👎.", ephemeral=True
        )

    @discord.ui.button(label="Hide for 24 hours", style=discord.ButtonStyle.secondary)
    async def day(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._apply(interaction, 24, "hidden for 24 hours")

    @discord.ui.button(label="Hide for a week", style=discord.ButtonStyle.secondary)
    async def week(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._apply(interaction, 24 * 7, "hidden for a week")

    @discord.ui.button(label="Hide permanently", style=discord.ButtonStyle.danger)
    async def forever(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._apply(interaction, None, "hidden permanently")


class AnswerPanelView(discord.ui.View):
    """Buttons under an answer: model-suggested follow-ups, 👍/👎, the settings panel, dismiss."""

    def __init__(
        self,
        bot: Any,
        *,
        question: str,
        answer: str,
        user_id: int,
        message_id: int | None = None,
        actions: list[tuple[str, str]] | None = None,
        show_feedback: bool = True,
    ) -> None:
        super().__init__(timeout=3600)
        self.bot = bot
        self.question = question
        self.answer = answer
        self.user_id = user_id
        self.message_id = message_id
        self.show_feedback = show_feedback
        for label, prompt in (actions or [])[:_MAX_BUTTONS]:
            self.add_item(self._action_button(label, prompt))
        if show_feedback:
            self.add_item(self._like_button())
            self.add_item(self._dislike_button())
            self.add_item(self._dismiss_button())

    def _action_button(self, label: str, prompt: str) -> discord.ui.Button:
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)

        async def callback(interaction: discord.Interaction) -> None:  # pragma: no cover - Discord only
            await interaction.response.defer(ephemeral=True, thinking=True)
            text = await followup_answer(
                self.bot, question=self.question, answer=self.answer, prompt=prompt
            )
            parts = _split(text)
            if not parts:
                await interaction.followup.send("I could not find anything more on that.", ephemeral=True)
                return
            await interaction.followup.send(parts[0], ephemeral=True)
            for part in parts[1:]:
                await interaction.followup.send(part, ephemeral=True)

        button.callback = callback
        return button

    async def _rate(self, interaction: discord.Interaction, rating: str) -> None:
        db_path = _db_path(self.bot)
        if not db_path:
            await interaction.response.send_message("I cannot reach my feedback store.", ephemeral=True)
            return
        try:
            feedback_id = engagement.record_feedback(
                db_path,
                user_id=interaction.user.id,
                rating=rating,
                message_id=self.message_id,
                question=self.question,
                answer=self.answer,
            )
        except Exception as exc:
            print(f"[panel] feedback insert failed: {type(exc).__name__}: {exc}", flush=True)
            await interaction.response.send_message("I could not save that.", ephemeral=True)
            return
        if rating == "down":
            await interaction.response.send_message(
                "Thanks — noted. If you tell me what was wrong I can use it.", 
                ephemeral=True,
                view=FeedbackThanksView(self.bot, feedback_id=feedback_id, user_id=interaction.user.id),
            )
        else:
            await interaction.response.send_message(
                "Thanks — saved.", ephemeral=True,
                view=FeedbackThanksView(self.bot, feedback_id=feedback_id, user_id=interaction.user.id),
            )

    def _like_button(self) -> discord.ui.Button:
        button = discord.ui.Button(label="Helpful", emoji="👍", style=discord.ButtonStyle.success)

        async def callback(interaction: discord.Interaction) -> None:  # pragma: no cover - Discord only
            await self._rate(interaction, "up")

        button.callback = callback
        return button

    def _dislike_button(self) -> discord.ui.Button:
        button = discord.ui.Button(label="Not helpful", emoji="👎", style=discord.ButtonStyle.danger)

        async def callback(interaction: discord.Interaction) -> None:  # pragma: no cover - Discord only
            await self._rate(interaction, "down")

        button.callback = callback
        return button

    def _dismiss_button(self) -> discord.ui.Button:
        button = discord.ui.Button(label="Hide feedback", emoji="✖️", style=discord.ButtonStyle.secondary)

        async def callback(interaction: discord.Interaction) -> None:  # pragma: no cover - Discord only
            await interaction.response.send_message(
                "How long should I stop asking for feedback?",
                ephemeral=True,
                view=DismissView(self.bot, user_id=interaction.user.id),
            )

        button.callback = callback
        return button

    async def settings_callback(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        db_path = _db_path(self.bot)
        pref = engagement.get_pref(db_path, interaction.user.id) if db_path else {}
        state = engagement.describe_auto_answer(pref) if pref else "unknown"
        await interaction.response.send_message(
            f"**How I answer you** — currently {state}.\nPick what you want; this only affects you.",
            ephemeral=True,
            view=SettingsView(self.bot, user_id=interaction.user.id),
        )


def build_answer_panel(
    bot: Any,
    *,
    question: str,
    answer: str,
    user_id: int,
    message_id: int | None = None,
    actions: list[tuple[str, str]] | None = None,
    show_feedback: bool = True,
) -> AnswerPanelView:
    view = AnswerPanelView(
        bot,
        question=question,
        answer=answer,
        user_id=user_id,
        message_id=message_id,
        actions=actions,
        show_feedback=show_feedback,
    )
    # Always last, always private: the settings panel is not part of the feedback prompt.
    settings = discord.ui.Button(label="Settings", emoji="⚙️", style=discord.ButtonStyle.secondary)
    settings.callback = view.settings_callback
    view.add_item(settings)
    return view
