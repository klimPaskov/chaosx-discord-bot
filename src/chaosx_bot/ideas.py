"""The event-idea pipeline: intake, review state, board and awards (Hoops 2026-09-23).

Hoops: "the event idea creation and everything should be updated and made better. Plan this out." and then
"okay, implement the plans" - this module holds the parts that are pure logic so the bot layer stays thin:

- the lifecycle every community idea moves through (`filed` -> `reviewing` -> `planned` -> `building` ->
  `shipped`, or `declined`), with the labels and buttons the reviewer sees;
- the staged award: a small amount for filing, a large one when an idea is actually accepted, so chaos pays
  for quality instead of volume;
- a near-duplicate check, because the old flow keyed the award on the note path, so rewording an idea made a
  brand new file and a brand new award;
- the board and status rendering, so `/ideas` and the forum post read the same way.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# --- lifecycle ----------------------------------------------------------------------------------------

FILED = "filed"
REVIEWING = "reviewing"
PLANNED = "planned"
BUILDING = "building"
SHIPPED = "shipped"
DECLINED = "declined"

IDEA_STATUSES: tuple[str, ...] = (FILED, REVIEWING, PLANNED, BUILDING, SHIPPED, DECLINED)

STATUS_LABELS: dict[str, str] = {
    FILED: "Filed",
    REVIEWING: "Being reviewed",
    PLANNED: "Planned",
    BUILDING: "Being built",
    SHIPPED: "In the mod",
    DECLINED: "Not this time",
}

STATUS_EMOJI: dict[str, str] = {
    FILED: "📥",
    REVIEWING: "🔍",
    PLANNED: "🗺️",
    BUILDING: "🛠️",
    SHIPPED: "✅",
    DECLINED: "🚫",
}

# Statuses that count as "open" for the reviewer queue, in the order they should be worked.
OPEN_STATUSES: tuple[str, ...] = (FILED, REVIEWING, PLANNED, BUILDING)

# The buttons a reviewer gets on an idea's own forum post (owner-only in the bot layer).
REVIEW_BUTTONS: tuple[tuple[str, str, str], ...] = (
    (REVIEWING, "🔍 Reviewing", "secondary"),
    (PLANNED, "🗺️ Planned", "primary"),
    (BUILDING, "🛠️ Building", "secondary"),
    (SHIPPED, "✅ In the mod", "success"),
    (DECLINED, "🚫 Not this time", "danger"),
)

# --- awards -------------------------------------------------------------------------------------------

FILING_AWARD = 8.0  # for a well-formed, non-duplicate submission that actually reaches the forum
STATUS_AWARDS: dict[str, float] = {
    PLANNED: 12.0,  # accepted into the plan
    SHIPPED: 20.0,  # actually in the mod
}

# A member cannot file more than this many ideas in a rolling week (spam guard).
MAX_SUBMISSIONS_PER_WEEK = 5

# Above this token-overlap score a new idea is treated as a possible duplicate and must be confirmed.
DUPLICATE_THRESHOLD = 0.55

_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "by", "at", "from", "into",
    "if", "then", "when", "whenever", "is", "are", "be", "will", "would", "should", "can", "could",
    "this", "that", "these", "those", "it", "its", "as", "after", "before", "event", "events", "idea",
    "add", "adds", "make", "makes", "get", "gets", "give", "gives", "new", "country", "countries",
}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(str(status), str(status).replace("_", " ").title() or "Unknown")


def status_emoji(status: str) -> str:
    return STATUS_EMOJI.get(str(status), "•")


def done_status(status: str) -> bool:
    return str(status) in {SHIPPED, DECLINED}


def award_for_status(status: str) -> float:
    """The chaos paid when an idea enters this status (0 for the ones that do not pay)."""
    return float(STATUS_AWARDS.get(str(status), 0.0))


# --- duplicate detection -------------------------------------------------------------------------------

def keywords(text: str, *, limit: int = 40) -> set[str]:
    """Significant lowercase words of an idea, for overlap comparison."""
    words = [
        word for word in re.findall(r"[a-z0-9]{3,}", (text or "").lower()) if word not in _STOPWORDS
    ]
    return set(words[:limit])


def overlap(left: str, right: str) -> float:
    """Jaccard-style overlap of two ideas' keywords, 0..1."""
    first, second = keywords(left), keywords(right)
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


@dataclass(frozen=True)
class Duplicate:
    """A previously filed idea that looks like the one being submitted."""

    submission_id: int
    title: str
    status: str
    score: float

    def line(self) -> str:
        return (
            f"this looks like submission #{self.submission_id} (*{self.title}*, "
            f"{status_label(self.status).lower()}, {int(self.score * 100)}% overlap)"
        )


def find_duplicate(
    idea: str, existing: list[tuple[int, str, str]], *, threshold: float = DUPLICATE_THRESHOLD
) -> Duplicate | None:
    """The closest existing idea above `threshold`, or None.

    `existing` is `(submission_id, title, status)` for recently filed ideas.
    """
    best: Duplicate | None = None
    for submission_id, title, status in existing:
        score = overlap(idea, title)
        if score >= threshold and (best is None or score > best.score):
            best = Duplicate(int(submission_id), str(title), str(status), float(score))
    return best


# --- rendering ----------------------------------------------------------------------------------------

def priority_marker(priority: bool) -> str:
    """The marker a priority idea (Rising Chaos and up) carries in its post title."""
    return "⭐ " if priority else ""


def status_line(*, status: str, note: str = "") -> str:
    """The one-line status shown on the idea's own forum post."""
    line = f"{status_emoji(status)} **{status_label(status)}**"
    if note:
        line += f" — {note}"
    return line


def preview_text(
    *,
    draft: str,
    raw_idea: str,
    duplicate: Duplicate | None = None,
    vague_hint: str = "",
    priority: bool = False,
) -> str:
    """The ephemeral preview shown before anything is written or posted.

    Nothing reaches the vault or the forum until the submitter presses a button, which is the whole point:
    the old flow wrote the note and posted it in one shot.
    """
    parts = ["## 📥 Your event idea draft", draft.strip()]
    if vague_hint:
        parts.append(f"⚠️ {vague_hint}")
    if duplicate is not None:
        parts.append(
            f"⚠️ **Possible duplicate:** {duplicate.line()}. "
            "Post it anyway if yours is a new take, or edit it to say what is different."
        )
    if priority:
        parts.append(
            "⭐ **Priority review** — your tier puts this at the front of the review queue once it is posted."
        )
    parts.append(f"-# Original submission: {raw_idea.strip()[:400]}")
    parts.append(
        "Nothing has been saved or posted yet. `Post to forum` files it, `Edit` reopens the form with your "
        "text, `Discard` throws it away."
    )
    return "\n\n".join(parts)


def board_line(
    *,
    submission_id: int,
    title: str,
    status: str,
    author: str,
    age_days: int,
    priority: bool = False,
) -> str:
    """One row of the idea board."""
    marker = "⭐ " if priority else ""
    days = f"{int(age_days)}d" if int(age_days) < 60 else f"{int(age_days) // 30}mo"
    return (
        f"`#{submission_id}` {status_emoji(status)} {marker}**{title}** — {status_label(status)} · "
        f"{author or 'unknown'} · {days} old"
    )


def board_summary(counts: dict[str, int], *, oldest_open_days: int | None = None) -> str:
    """The queue-health line above the board rows."""
    open_count = sum(int(counts.get(status, 0)) for status in OPEN_STATUSES)
    if not open_count:
        return "No ideas waiting for review right now."
    oldest = f", oldest {oldest_open_days} days" if oldest_open_days else ""
    shipped = int(counts.get(SHIPPED, 0))
    return (
        f"**{open_count} idea(s) in the pipeline**{oldest}"
        + (f" · {shipped} already in the mod" if shipped else "")
    )


def rate_limit_message(*, recent: int, limit: int = MAX_SUBMISSIONS_PER_WEEK) -> str:
    return (
        f"You have filed {recent} ideas this week, which is the limit ({limit}). "
        "Give the queue a chance to move — you can file more next week."
    )
