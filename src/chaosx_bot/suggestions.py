"""Contextual "what can I do next" suggestions for the bottom of ChaosX command replies.

Hoops (2026-09-23): "the help command or other commands should also present these options in the
bottom and they should be dynamic, like suggested by the bot."

The rules that shape this module:

- **Suggestions are earned from live state, never a fixed list.** A suggestion only appears when the
  thing it points at actually exists right now (real queue depth, the member's own ideas, their own
  tier), so the footer is never an advertisement for an empty feature.
- **Never suggest something the asker cannot do.** Owner-only actions only appear for the owner.
- **Never ping, never mention.** Labels are plain text.
- **Short.** Five buttons is the ceiling; past that the footer becomes a second help page.
- Suggestions are ordered by usefulness, not by category, and the strongest one wins the first slot.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Suggestion:
    """One contextually-offered next action."""

    key: str
    label: str
    emoji: str


# Every action the footer can offer. The keys double as button custom_ids
# (`chaosx:suggest:<key>`), so they must stay stable across restarts.
SUGGESTION_KEYS: tuple[str, ...] = (
    "testing_vote",
    "idea_board",
    "my_ideas",
    "submit_idea",
    "my_tier",
    "report_bug",
    "ask_question",
    "idea_pipeline",
    "command_latency",
    "server_health",
)

MAX_SUGGESTIONS = 5


def build_suggestions(
    *,
    is_owner: bool = False,
    tier_name: str = "",
    testing_options: int = 0,
    has_voted: bool = False,
    open_ideas: int = 0,
    my_ideas: int = 0,
    reviewed_ideas: int = 0,
) -> list[Suggestion]:
    """Compose the next-step buttons for one member from live counts."""
    suggestions: list[Suggestion] = []

    if is_owner:
        # The owner's own queue comes first: it is the only one only he can clear.
        if open_ideas:
            suggestions.append(
                Suggestion("idea_pipeline", f"Review the idea pipeline ({open_ideas} open)", "🗂️")
            )
        suggestions.append(Suggestion("command_latency", "Command latency and health", "⏱️"))

    if testing_options:
        if has_voted:
            suggestions.append(
                Suggestion("testing_vote", f"Change your vote ({testing_options} options)", "🗳️")
            )
        else:
            suggestions.append(
                Suggestion("testing_vote", f"Vote on what gets tested ({testing_options} options)", "🗳️")
            )
    if my_ideas:
        suggestions.append(Suggestion("my_ideas", f"Track your {my_ideas} idea(s)", "📌"))
    elif not is_owner:
        suggestions.append(Suggestion("submit_idea", "Submit an event idea", "💡"))
    if open_ideas and not is_owner:
        suggestions.append(Suggestion("idea_board", f"Browse {open_ideas} community ideas", "📥"))
    if tier_name:
        suggestions.append(Suggestion("my_tier", f"Your tier: {tier_name}", "🪜"))
    if reviewed_ideas:
        suggestions.append(
            Suggestion("my_ideas", f"{reviewed_ideas} of your ideas were reviewed", "🔔")
        )
    suggestions.append(Suggestion("report_bug", "Report a bug or crash", "🐞"))
    suggestions.append(Suggestion("ask_question", "Ask a question about the mod", "❓"))
    if is_owner:
        suggestions.append(Suggestion("server_health", "Server health snapshot", "🩺"))

    # De-duplicate by key (a later, richer label must not double up a button) and cap the row count.
    seen: set[str] = set()
    unique: list[Suggestion] = []
    for suggestion in suggestions:
        if suggestion.key in seen:
            continue
        seen.add(suggestion.key)
        unique.append(suggestion)
    return unique[:MAX_SUGGESTIONS]


def render_suggestions(suggestions: list[Suggestion], *, heading: str = "Suggested next") -> str:
    """The text footer that sits under a command's own output."""
    if not suggestions:
        return ""
    lines = [f"### {heading}"]
    lines.extend(f"- {item.emoji} {item.label}" for item in suggestions)
    return "\n".join(lines)
