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

from . import engagement, member_memory, panel_state
from . import knowledge_gaps
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
            # Same synchronous FTS as the main ask path: on the loop it stalls every interaction for seconds, and a
            # click landing in that window expires before it can be acknowledged (10062).
            try:
                context = str(
                    await asyncio.to_thread(
                        knowledge.public_ask_context, f"{question} {prompt}", anchors=anchors
                    )
                )
            except TypeError:
                context = str(
                    await asyncio.to_thread(knowledge.public_ask_context, f"{question} {prompt}")
                )
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


class FeedbackModal(discord.ui.Modal):
    """The private box Hoops asked for: press 👍 or 👎 and say why, in a text box only you see.

    The rating is already stored when this opens, so dismissing the box still counts the thumb; submitting it
    adds the note.
    """

    def __init__(self, bot: Any, *, feedback_id: int, rating: str) -> None:
        helpful = rating != "down"
        super().__init__(title="What was helpful?" if helpful else "What was wrong?")
        self.bot = bot
        self.feedback_id = feedback_id
        self.rating = rating
        self.note = discord.ui.TextInput(
            label="Why?" if helpful else "What went wrong?",
            style=discord.TextStyle.paragraph,
            placeholder=(
                "What did it get right? Anything you would add?"
                if helpful
                else "What was wrong or missing? What would have helped?"
            ),
            max_length=1000,
            required=False,
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:  # pragma: no cover - Discord only
        text = str(self.note.value or "").strip()
        db_path = _db_path(self.bot)
        saved = False
        if text and db_path:
            try:
                saved = engagement.add_feedback_comment(db_path, self.feedback_id, text)
            except Exception as exc:
                print(f"[panel] feedback note failed: {type(exc).__name__}: {exc}", flush=True)
        thanks = "Thanks — saved with your note." if saved else "Thanks — noted."
        await interaction.response.send_message(thanks, ephemeral=True)


# A modal is only worth showing if it asks for the two things that decide the answer. Which two depends on the
# topic: a setup failure is a platform plus an error, a broken event is a name plus intended behaviour. Matching on
# the question keeps one modal flexible instead of a canned form for setup and nothing for everything else.
MODAL_TOPICS: list[tuple[tuple[str, ...], str, tuple[tuple[str, str, bool], ...]]] = [
    (
        ("setup", "install", "mcp", "codex", "cursor", "claude", "agent", "app", "server", "detect", "start"),
        "Check your modding setup",
        (
            ("OS and agent", "Windows 11, Claude Code (or Codex / Cursor / OpenCode)", False),
            ("What happens?", "Paste the error text, or what the app/MCP server does instead of starting.", True),
        ),
    ),
    (
        ("test", "tester", "qa", "playtest", "bug", "crash", "error log", "broken"),
        "Tell me about the bug",
        (
            ("Game and mod version", "HOI4 1.16, Chaos Redux build from the devlog", False),
            ("What you did, and what happened", "Steps, then what you saw. Paste the error.log line if you have it.", True),
        ),
    ),
    (
        ("event", "scenario", "focus", "tree", "decision", "mechanic"),
        "Check the event or focus",
        (
            ("Which event or focus?", "Name or id, and the file it lives in if you know", False),
            ("Intended vs actual", "What should happen when it fires, and what happens instead.", True),
        ),
    ),
    (
        ("lore", "world", "nation", "history", "story", "faction"),
        "Check it against the lore",
        (
            ("Nation, region or faction", "e.g. a country tag or a region name", False),
            ("What you are building", "The idea you want to keep consistent with the setting.", True),
        ),
    ),
]

DEFAULT_MODAL_SPEC = (
    "More detail, better answer",
    (
        ("What are you actually doing?", "The step you are on, the tool you are using, the version.", False),
        ("What do you want me to look at?", "The exact symptom or the part you are unsure about.", True),
    ),
)


def modal_spec_for(question: str) -> tuple[str, tuple[tuple[str, str, bool], ...]]:
    """Pick the questions worth asking for this topic; falls back to a generic pair."""
    lowered = (question or "").lower()
    for keywords, title, fields in MODAL_TOPICS:
        if any(keyword in lowered for keyword in keywords):
            return title, fields
    return DEFAULT_MODAL_SPEC


class ActionModal(discord.ui.Modal):
    """Topic-shaped follow-up questions, answered with the same grounded pipeline as any other ask."""

    def __init__(self, bot: Any, *, question: str, answer: str, message_id: int) -> None:
        title, fields = modal_spec_for(question)
        super().__init__(title=title[:45] or "More detail")
        self.bot = bot
        self.question = question
        self.answer = answer
        self.message_id = message_id
        self.inputs: list[discord.ui.TextInput] = []
        for label, placeholder, paragraph in fields[:2]:
            item = discord.ui.TextInput(
                label=label[:45],
                placeholder=placeholder[:100],
                style=discord.TextStyle.paragraph if paragraph else discord.TextStyle.short,
                max_length=1000 if paragraph else 120,
                required=True,
            )
            self.inputs.append(item)
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:  # pragma: no cover - Discord only
        details = "\n".join(f"{item.label}: {item.value}" for item in self.inputs)
        prompt = f"{(self.inputs[0].value if self.inputs else '')[:400]}\n{details}"
        try:
            text = await followup_answer(self.bot, question=self.question, answer=self.answer, prompt=prompt)
            parts = _split(text)
            for part in parts:
                await interaction.followup.send(part, allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            print(f"[panel] modal answer failed: {type(exc).__name__}: {exc}", flush=True)
            await interaction.followup.send(
                "That did not go through — try again.", ephemeral=True
            )


class ForgetConfirmView(discord.ui.View):
    """Deleting memory is irreversible, so it takes a second, deliberate press."""

    def __init__(self, bot: Any, *, user_id: int) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.user_id = user_id

    @discord.ui.button(label="Yes, delete everything", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        db_path = _db_path(self.bot)
        if not db_path:
            await interaction.response.send_message("I cannot reach my memory store.", ephemeral=True)
            return
        removed = member_memory.forget(db_path, self.user_id)
        await interaction.response.edit_message(
            content=(
                f"Done — {removed} saved fact{'s' if removed != 1 else ''} about you deleted, and your profile "
                "with it. I will start fresh from what you say next."
            ),
            view=None,
        )

    @discord.ui.button(label="Keep my memory", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Nothing deleted. Your memory is intact.", view=None)


class MemoryView(discord.ui.View):
    """What ChaosX actually has on file for this member, and a way to wipe it."""

    def __init__(self, bot: Any, *, user_id: int) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        self.user_id = user_id

    def render(self) -> str:
        db_path = _db_path(self.bot)
        if not db_path:
            return "I cannot reach my memory store right now."
        try:
            facts = member_memory.durable_facts(db_path, self.user_id, limit=25)
        except Exception as exc:
            return f"I could not read your memory: {type(exc).__name__}"
        if not facts:
            return "Nothing saved about you yet. I learn from what you say in the server — mention your setup, your mod, what you are stuck on."
        header = f"**What I remember about you** ({len(facts)})"
        footer = "-# Say “forget that I …” and I will drop the matching line."
        body: list[str] = []
        used = len(header) + len(footer) + 8
        for fact in facts:
            line = f"• {fact}"
            if used + len(line) + 1 > 1900:
                body.append(f"-# …and {len(facts) - len(body)} more")
                break
            body.append(line)
            used += len(line) + 1
        return "\n".join([header, "", *body, "", footer])

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await interaction.response.edit_message(content=self.render(), view=self)

    @discord.ui.button(label="Forget everything about me", emoji="❌", style=discord.ButtonStyle.danger)
    async def wipe(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        db_path = _db_path(self.bot)
        if not db_path:
            await interaction.response.send_message("I cannot reach my memory store.", ephemeral=True)
            return
        facts = member_memory.durable_facts(db_path, self.user_id, limit=200)
        await interaction.response.send_message(
            f"This deletes **{len(facts)} saved fact{'s' if len(facts) != 1 else ''}** about you and your profile. "
            "It cannot be undone.",
            ephemeral=True,
            view=ForgetConfirmView(self.bot, user_id=self.user_id),
        )


class SettingsModal(discord.ui.Modal, title="ChaosX settings"):
    """A real popup: three dropdowns, one Save, and nothing left hanging in the channel."""

    def __init__(self, bot: Any, *, user_id: int) -> None:
        super().__init__()
        self.bot = bot
        self.user_id = user_id
        db_path = _db_path(bot)
        pref = engagement.get_pref(db_path, user_id) if db_path else {}

        self.answer = discord.ui.Select(
            placeholder="When should I answer you?",
            options=[
                discord.SelectOption(label="Answer me automatically", value="auto:on", emoji="🤖"),
                discord.SelectOption(label="Only when I ask", value="auto:off", emoji="🙋"),
                discord.SelectOption(label="Answer for 24 hours", value="auto:24", emoji="⏱️"),
            ],
            min_values=1,
            max_values=1,
            required=False,
        )
        self.detail = discord.ui.Select(
            placeholder="How much detail?",
            options=[
                discord.SelectOption(label="Short answers", value="detail:concise", emoji="✂️"),
                discord.SelectOption(label="Explain the reasoning", value="detail:detailed", emoji="📖"),
            ],
            min_values=1,
            max_values=1,
            required=False,
        )
        self.feedback = discord.ui.Select(
            placeholder="Feedback prompt",
            options=[
                discord.SelectOption(label="Keep asking after answers", value="fb:on", emoji="👍"),
                discord.SelectOption(label="Hide it for 24 hours", value="fb:24", emoji="🌙"),
                discord.SelectOption(label="Hide it for a week", value="fb:week", emoji="📅"),
                discord.SelectOption(label="Hide it permanently", value="fb:never", emoji="❌"),
            ],
            min_values=1,
            max_values=1,
            required=False,
        )
        self.add_item(self.answer)
        self.add_item(self.detail)
        self.add_item(self.feedback)

    async def on_submit(self, interaction: discord.Interaction) -> None:  # pragma: no cover - Discord only
        db_path = _db_path(self.bot)
        if not db_path:
            await interaction.response.send_message("I cannot reach my settings store.", ephemeral=True)
            return
        changes: list[str] = []
        choice = (self.answer.values or [""])[0]
        if choice == "auto:on":
            engagement.set_auto_answer(db_path, self.user_id, enabled=True, hours=None)
            changes.append("answering you automatically")
        elif choice == "auto:off":
            engagement.set_auto_answer(db_path, self.user_id, enabled=False, hours=None)
            changes.append("only answering when you ask")
        elif choice == "auto:24":
            engagement.set_auto_answer(db_path, self.user_id, enabled=True, hours=24)
            changes.append("answering you automatically for 24 hours")

        choice = (self.detail.values or [""])[0]
        if choice == "detail:concise":
            engagement.set_detail_level(db_path, self.user_id, "concise")
            changes.append("short answers")
        elif choice == "detail:detailed":
            engagement.set_detail_level(db_path, self.user_id, "detailed")
            changes.append("answers with reasoning")

        choice = (self.feedback.values or [""])[0]
        if choice == "fb:on":
            engagement.dismiss_feedback_prompt(db_path, self.user_id, hours=0)
            changes.append("feedback prompt back on")
        elif choice in ("fb:24", "fb:week", "fb:never"):
            hours = {"fb:24": 24, "fb:week": 24 * 7, "fb:never": None}[choice]
            engagement.dismiss_feedback_prompt(db_path, self.user_id, hours=hours)
            changes.append({"fb:24": "feedback hidden for 24 hours", "fb:week": "feedback hidden for a week", "fb:never": "feedback hidden permanently"}[choice])

        pref = engagement.get_pref(db_path, self.user_id)
        summary = engagement.describe_auto_answer(pref)
        text = (
            "**Saved.** " + ("; ".join(changes) + ".\n" if changes else "Nothing changed.\n")
            + f"Unprompted answers: **{summary}**."
        )
        await interaction.response.send_message(
            text, ephemeral=True, view=MemoryShortcutView(self.bot, user_id=self.user_id)
        )


class MemoryShortcutView(discord.ui.View):
    """Offered right after saving, so what the bot remembers is one press away."""

    def __init__(self, bot: Any, *, user_id: int) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.user_id = user_id

    @discord.ui.button(label="What I remember", emoji="🧠", style=discord.ButtonStyle.secondary)
    async def show_memory(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        view = MemoryView(self.bot, user_id=self.user_id)
        await interaction.response.send_message(view.render(), ephemeral=True, view=view)

    @discord.ui.button(label="Settings again", emoji="⚙️", style=discord.ButtonStyle.secondary)
    async def again(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await interaction.response.send_modal(SettingsModal(self.bot, user_id=self.user_id))


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

    @discord.ui.button(label="What I remember", emoji="🧠", style=discord.ButtonStyle.secondary)
    async def remember_view(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        view = MemoryView(self.bot, user_id=self.user_id)
        await interaction.response.send_message(view.render(), ephemeral=True, view=view)

    @discord.ui.button(label="Hide feedback", emoji="❌", style=discord.ButtonStyle.secondary)
    async def hide_feedback(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "How long should I stop asking for feedback?",
            ephemeral=True,
            view=DismissView(self.bot, user_id=self.user_id),
        )

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

    @discord.ui.button(label="Off for a week", emoji="❌", style=discord.ButtonStyle.danger)
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

    @discord.ui.button(label="Hide permanently", emoji="❌", style=discord.ButtonStyle.danger)
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
        for index, (label, prompt) in enumerate((actions or [])[:_MAX_BUTTONS]):
            self.add_item(self._action_button(label, prompt, index=index))
        if show_feedback:
            self.add_item(self._like_button())
            self.add_item(self._dislike_button())
            self.add_item(self._dismiss_button())

    def _action_button(self, label: str, prompt: str, *, index: int = 0) -> discord.ui.Button:
        # Stable custom id: the same button keeps working after a restart, and the row it sits on can be rebuilt
        # from what was stored against the message.
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, custom_id=f"cxfb:act{index}")

        async def callback(interaction: discord.Interaction) -> None:  # pragma: no cover - Discord only
            if label.strip().lower().startswith("check my setup"):
                await interaction.response.send_modal(
                    ActionModal(self.bot, question=self.question, answer=self.answer, message_id=message_id)
                )
                return
            # Public on purpose (Hoops, 2026-10-24): pressing a quick action is a member asking the bot something
            # in the channel, so everyone gets the answer; only the settings and feedback panels stay private.
            await interaction.response.defer(thinking=False)
            text = await followup_answer(
                self.bot, question=self.question, answer=self.answer, prompt=prompt
            )
            parts = _split(text)
            if not parts:
                await interaction.followup.send(
                    "I could not find anything more on that.", allowed_mentions=discord.AllowedMentions.none()
                )
                return
            for part in parts:
                await interaction.followup.send(part, allowed_mentions=discord.AllowedMentions.none())

        button.callback = callback
        return button

    async def on_error(self, interaction, error, item) -> None:  # pragma: no cover - Discord only
        import logging

        logging.getLogger("chaosx.answer_panel").exception("panel item %s failed", getattr(item, "label", item))
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "That did not go through. I logged it — try again in a moment.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "That did not go through. I logged it — try again in a moment.", ephemeral=True
                )
        except Exception:
            pass

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
            try:
                knowledge_gaps.record_dislike(db_path, question=self.question)
            except Exception as exc:
                print(f"[panel] gap record failed: {type(exc).__name__}: {exc}", flush=True)
        # The rating is in the database before the box opens, so closing the box still counts the thumb.
        await interaction.response.send_modal(
            FeedbackModal(self.bot, feedback_id=feedback_id, rating=rating)
        )

    def _like_button(self) -> discord.ui.Button:
        button = discord.ui.Button(
            label="Helpful", emoji="👍", style=discord.ButtonStyle.success, custom_id="cxfb:up"
        )

        async def callback(interaction: discord.Interaction) -> None:  # pragma: no cover - Discord only
            await self._rate(interaction, "up")

        button.callback = callback
        return button

    def _dislike_button(self) -> discord.ui.Button:
        button = discord.ui.Button(
            label="Not helpful", emoji="👎", style=discord.ButtonStyle.danger, custom_id="cxfb:down"
        )

        async def callback(interaction: discord.Interaction) -> None:  # pragma: no cover - Discord only
            await self._rate(interaction, "down")

        button.callback = callback
        return button

    def _dismiss_button(self) -> discord.ui.Button:
        button = discord.ui.Button(
            label="Hide feedback", emoji="❌", style=discord.ButtonStyle.secondary, custom_id="cxfb:hide"
        )

        async def callback(interaction: discord.Interaction) -> None:  # pragma: no cover - Discord only
            await interaction.response.send_message(
                "How long should I stop asking for feedback?",
                ephemeral=True,
                view=DismissView(self.bot, user_id=interaction.user.id),
            )

        button.callback = callback
        return button

    async def settings_callback(self, interaction: discord.Interaction, button: discord.ui.Button | None = None) -> None:
        db_path = _db_path(self.bot)
        pref = engagement.get_pref(db_path, interaction.user.id) if db_path else {}
        state = engagement.describe_auto_answer(pref) if pref else "unknown"
        # A popup dismisses itself; an ephemeral message with eight buttons lingers in the channel instead.
        await interaction.response.send_modal(SettingsModal(self.bot, user_id=interaction.user.id))


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
    settings = discord.ui.Button(
        label="Settings", emoji="⚙️", style=discord.ButtonStyle.secondary, custom_id="cxfb:set"
    )
    settings.callback = view.settings_callback
    view.add_item(settings)
    return view


_PANEL_KEYS = {
    "up": ("Helpful", "👍", discord.ButtonStyle.success),
    "down": ("Not helpful", "👎", discord.ButtonStyle.danger),
    "hide": ("Hide feedback", "❌", discord.ButtonStyle.secondary),
    "set": ("Settings", "⚙️", discord.ButtonStyle.secondary),
}


async def handle_panel_click(interaction: discord.Interaction, key: str, *, deferred: bool = False) -> None:
    """Answer a panel click using the panel's stored subject, not in-memory objects.

    This is what makes old panels work: the question, answer and quick actions come back from the database keyed
    by the message the button sits under, so a click works after any number of restarts.
    """
    bot = interaction.client
    message = interaction.message
    message_id = getattr(message, "id", None)
    db_path = _db_path(bot)
    state = panel_state.load(db_path, message_id) if (db_path and message_id) else None
    fallback = (message.content if message and message.content else "")
    question = (state or {}).get("question") or fallback
    answer = (state or {}).get("answer") or fallback
    actions = (state or {}).get("actions") or []
    view = AnswerPanelView(
        bot,
        question=question,
        answer=answer,
        user_id=interaction.user.id,
        message_id=message_id,
        actions=None,
        show_feedback=False,
    )
    if key in ("up", "down"):
        await view._rate(interaction, key)
        return
    if key == "set":
        await view.settings_callback(interaction)
        return
    if key == "hide":
        await interaction.response.send_message(
            "How long should I stop asking for feedback?",
            ephemeral=True,
            view=DismissView(bot, user_id=interaction.user.id),
        )
        return
    if key.startswith("act"):
        index = int(key[3:])
        if index >= len(actions):
            await interaction.response.send_message(
                "That option is not available any more — ask me again.", ephemeral=True
            )
            return
        label, prompt = actions[index]
        if str(label).strip().lower().startswith("check my setup"):
            await interaction.response.send_modal(
                ActionModal(bot, question=question, answer=answer, message_id=message_id)
            )
            return
        if not deferred:
            await interaction.response.defer(thinking=False)
        text = await followup_answer(bot, question=question, answer=answer, prompt=prompt)
        parts = _split(text)
        if not parts:
            await interaction.followup.send(
                "I could not find anything more on that.", allowed_mentions=discord.AllowedMentions.none()
            )
            return
        for part in parts:
            await interaction.followup.send(part, allowed_mentions=discord.AllowedMentions.none())
        # The action has been used, so remove it: a row of spent buttons invites clicks that do nothing.
        try:
            if interaction.message is not None:
                await interaction.message.edit(
                    view=build_answer_panel(
                        bot,
                        question=question,
                        answer=answer,
                        user_id=interaction.user.id,
                        message_id=message_id,
                        actions=[],
                        show_feedback=True,
                    )
                )
                if db_path:
                    panel_state.remember(
                        db_path,
                        message_id=message_id,
                        question=question,
                        answer=answer,
                        user_id=interaction.user.id,
                        actions=[],
                    )
        except Exception as exc:
            print(f"[panel] could not clear used actions: {type(exc).__name__}: {exc}", flush=True)


class PanelItem(discord.ui.DynamicItem[discord.ui.Button], template=r"cxfb:(?P<key>up|down|hide|set|act\d+)"):
    """Catches panel buttons on any message, including ones posted before the last restart."""

    def __init__(self, key: str) -> None:
        label, emoji, style = _PANEL_KEYS.get(key, ("More", None, discord.ButtonStyle.secondary))
        super().__init__(
            discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"cxfb:{key}")
        )
        self.key = key

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: Any, /
    ) -> "PanelItem":
        # discord.py awaits this factory, so it must be a coroutine: returning an instance from a sync method made
        # every click die with "object PanelItem can't be used in 'await' expression" (Hoops, 2026-10-25).
        return cls(match["key"])

    async def callback(self, interaction: discord.Interaction) -> None:
        print(f"[panel] click {self.key} by {interaction.user.id} on {getattr(interaction.message, 'id', 0)}", flush=True)
        try:
            # Acknowledge before touching the database: Discord allows three seconds, and a click that lands during a
            # stall must still be answered rather than dying as "did not respond in time". The exception is an action
            # that opens a modal, because a modal has to be the *first* response to its interaction.
            deferred = False
            wants_modal = False
            if self.key.startswith("act"):
                label = ""
                try:
                    state = panel_state.load(_db_path(interaction.client), interaction.message.id)
                    index = int(self.key[3:])
                    actions = (state or {}).get("actions") or []
                    if index < len(actions):
                        label = str(actions[index][0])
                except Exception:
                    label = ""
                wants_modal = any(
                    word in label.strip().lower() for word in ("check", "diagnose", "verify", "inspect")
                )
            if self.key.startswith("act") and not wants_modal and not interaction.response.is_done():
                await interaction.response.defer(thinking=False)
                deferred = True
            await handle_panel_click(interaction, self.key, deferred=deferred)
        except Exception as exc:  # pragma: no cover - Discord only
            import logging

            logging.getLogger("chaosx.answer_panel").exception("panel click failed: %s", exc)
            # An expired or already-answered interaction cannot be replied to; telling the member "try again"
            # only adds noise on top of a click Discord already showed as failed.
            if isinstance(exc, (discord.errors.InteractionResponded, discord.errors.NotFound)):
                return
            try:
                if interaction.response.is_done():
                    await interaction.followup.send("That did not go through — try again.", ephemeral=True)
                else:
                    await interaction.response.send_message(
                        "That did not go through — try again.", ephemeral=True
                    )
            except Exception:
                pass
