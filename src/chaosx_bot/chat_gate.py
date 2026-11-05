"""Deterministic gates in front of every casual reply (Hoops, 2026-11-05).

Why this module exists
----------------------
On 2026-11-03 #bot-spam carried 231 human messages and 231 ChaosX replies. 169 of those replies
answered 169 messages that contained only **two** distinct texts ("kill all clankers", and the
double-pasted variant). The same line came back reworded twelve times in six minutes and the bot
posted 56 messages inside a single minute. None of that is a model problem — a 231/231 ratio is a
missing gate.

Four gates, in order, each one deterministic (no model, no randomness that a restart would re-roll):

- ``address``  — is this message actually for the bot? Mention, reply, name, or a chain it joined.
- ``repeat``   — did this member already send that exact text inside the window? Answer once.
- ``inflight`` — is an answer to the same normalised text already being generated? Never run two
  model calls for one line; the duplicates arrive while the first call is still working.
- ``budget``   — a minimum gap between casual replies in a channel and at most one reply per N
  member messages. Real Chaos Redux questions are never gated (Hoops: they should be answered the
  tenth time as well as the first).

A troll that only repeats an insult, floods one channel or chains personal jabs gets a silent
cooldown here (``TrollTracker``) so the bot stops feeding the bit. The moderation ladder is a
separate decision made from the same evidence.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

# --- normalisation ---------------------------------------------------------------------------

_CUSTOM_EMOJI_RE = re.compile(r"<a?:[A-Za-z0-9_]{2,32}:\d{15,25}>")
_SHORTCODE_RE = re.compile(r":([A-Za-z0-9_]{2,32}):")
_UNICODE_PICTO_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2b00-\u2bff\ufe0f\u200d]")
_MENTION_RE = re.compile(r"<@[!&]?\d+>")
_URL_RE = re.compile(r"https?://\S+")
_NON_WORD_RE = re.compile(r"[^a-z0-9 ]+")
_SPACES_RE = re.compile(r"\s+")

# --- troll signals ---------------------------------------------------------------------------

# "clanker" is the server's own word for the bot and is not in the moderation guard's insult list,
# so the chat layer needs its own read on it.
_INSULT_RE = re.compile(
    r"\b(?:"
    r"clankers?|toaster|skynet|chatbot|ai|bot|machine|robot"
    r")\b\s*(?:is|are|you're|youre|you are)?\s*(?:stupid|dumb|garbage|trash|useless|retarded|"
    r"braindead|brainlet|worthless|ugly|broken|screwed|bad)|"
    r"\b(?:stupid|dumb|idiot|moron|useless|worthless|pathetic|brainlet|braindead|"
    r"shut up|stfu|get a job|kill yourself|kys)\b|"
    r"\bkill (?:all|every|the) (?:the )?(?:clankers?|bots?|machines?|ais?)\b",
    re.IGNORECASE,
)
def normalize(text: str) -> str:
    """A text a repeat check can compare: no mentions, links, emoji or punctuation.

    "Kill all clankers", "kill all clankers!" and "Kill all clankersKill all clankers" normalise to
    different keys on purpose (the concatenated one contains the phrase twice) — the repeat check
    also has a prefix rule for that case.
    """
    value = (text or "").lower()
    value = _CUSTOM_EMOJI_RE.sub(" ", value)
    value = _SHORTCODE_RE.sub(" ", value)
    value = _UNICODE_PICTO_RE.sub(" ", value)
    value = _MENTION_RE.sub(" ", value)
    value = _URL_RE.sub(" ", value)
    value = unicodedata.normalize("NFKC", value)
    value = _NON_WORD_RE.sub(" ", value)
    return _SPACES_RE.sub(" ", value).strip()


def _epoch(now: datetime | None = None) -> float:
    return (now or datetime.now(timezone.utc)).timestamp()


def looks_like_insult(text: str) -> bool:
    """Short, insulting, and not actually asking for anything."""
    value = (text or "").strip()
    if not value or "?" in value:
        return False
    if len(value) > 90:
        return False
    return bool(_INSULT_RE.search(value))


def is_jab_at_bot(text: str) -> bool:
    """An insult plus the bot's own name or a bot word, e.g. "stupid clanker" / "chaosx is dumb"."""
    value = (text or "").strip()
    if not looks_like_insult(value):
        return False
    return bool(re.search(r"(?i)\b(?:chaosx|chaos x|clanker|bot|ai)\b", value))


# --- address gate ----------------------------------------------------------------------------


def addresses_bot(
    *,
    content: str,
    mentioned: bool,
    replies_to_bot: bool,
    name_addressed: bool,
    in_chain: bool = False,
    banter_channel: bool = False,
    mentions_other: bool = False,
    replies_to_other: bool = False,
) -> bool:
    """Whether a channel message is for the bot at all.

    A human answering another human ("<@1125…> hi") is not, and used to get a reply anyway because
    the banter-channel branch answered everything. ``in_chain`` covers a conversation the bot is
    already part of: its own answer is the parent, or the message directly follows its reply.

    ``banter_channel`` inverts the default there: #bot-spam exists so people can talk to the bot
    without addressing it (Hoops, 2026-11-05: "the bot should respond even when not referred to
    there"). Ordinary chat counts as addressed; a line aimed at another member still does not,
    because that is two humans talking and the repeat/budget gates are what keep it to one reply.
    """
    if mentioned or replies_to_bot or name_addressed or in_chain:
        return True
    if banter_channel and not mentions_other and not replies_to_other:
        return True
    return False


# --- repeat window ---------------------------------------------------------------------------


class RepeatWindow:
    """Answer once per distinct line from a member inside ``window_seconds``.

    Keys are ``(channel_id, author_id)``; values are ``(epoch, normalised text)``. A repeat is the
    same normalised text, or one that contains an earlier one (the double-pasted "kill all
    clankersKill all clankers" flood is the same line).
    """

    def __init__(self, *, window_seconds: float = 180.0, max_keys: int = 5000) -> None:
        self.window_seconds = float(window_seconds)
        self.max_keys = int(max_keys)
        self._seen: dict[tuple[int, int], deque[tuple[float, str]]] = defaultdict(deque)

    def _prune(self, bucket: deque[tuple[float, str]], now: float) -> None:
        cutoff = now - self.window_seconds
        while bucket and bucket[0][0] < cutoff:
            bucket.popleft()

    def is_repeat(self, *, channel_id: int, author_id: int, text: str, now: datetime | None = None) -> bool:
        """True when this member already sent this line inside the window (does not record)."""
        moment = _epoch(now)
        key = (int(channel_id), int(author_id))
        bucket = self._seen.get(key)
        if not bucket:
            return False
        self._prune(bucket, moment)
        wanted = normalize(text)
        if not wanted:
            return False
        for _stamp, previous in bucket:
            if not previous:
                continue
            if previous == wanted:
                return True
            # The pasted-twice flood: "kill all clankerskill all clankers" is the same line
            # repeated, not a new one. Only that exact shape counts — a longer sentence that
            # merely starts with the earlier text ("this bot" -> "this bot again") is new.
            if len(previous) >= 12 and wanted.startswith(previous) and wanted.replace(previous, "") == "":
                return True
        return False

    def note(self, *, channel_id: int, author_id: int, text: str, now: datetime | None = None) -> None:
        moment = _epoch(now)
        key = (int(channel_id), int(author_id))
        wanted = normalize(text)
        if not wanted:
            return
        if len(self._seen) >= self.max_keys and key not in self._seen:
            # Drop the least recently used key instead of growing without bound.
            oldest = min(self._seen.items(), key=lambda item: item[1][-1][0] if item[1] else 0.0)
            self._seen.pop(oldest[0], None)
        bucket = self._seen[key]
        self._prune(bucket, moment)
        bucket.append((moment, wanted))

    def forget(self, *, channel_id: int, author_id: int) -> None:
        self._seen.pop((int(channel_id), int(author_id)), None)


# --- in-flight lock --------------------------------------------------------------------------


class InFlight:
    """One model call per (channel, normalised text) at a time.

    Duplicates arrive while the first answer is still being generated, so without this the same
    prompt is sent eight times and eight near-identical replies land seconds apart.
    """

    def __init__(self) -> None:
        self._active: set[tuple[int, str]] = set()

    @staticmethod
    def key(channel_id: int, text: str) -> tuple[int, str]:
        return (int(channel_id), normalize(text)[:400])

    def acquire(self, *, channel_id: int, text: str) -> bool:
        key = self.key(channel_id, text)
        if not key[1]:
            return True
        if key in self._active:
            return False
        self._active.add(key)
        return True

    def release(self, *, channel_id: int, text: str) -> None:
        self._active.discard(self.key(channel_id, text))

    def busy(self, *, channel_id: int, text: str) -> bool:
        return self.key(channel_id, text) in self._active


# --- reply budget ----------------------------------------------------------------------------


@dataclass
class BudgetDecision:
    allowed: bool
    reason: str = ""
    retry_after_seconds: int = 0


class ReplyBudget:
    """Casual replies per channel: a minimum gap, and at most one reply per ``one_in`` messages.

    The gap is what stops a 1:1 reply ratio. ``one_in`` is the second opinion for a member sending
    several short lines in a row: after the bot answers them, that member needs ``one_in`` fresh
    messages before the next casual reply, so a run of one-liners cannot pull a reply each. A first
    message is never gated (``since_reply`` is unknown until the bot has answered them once).
    """

    def __init__(self, *, gap_seconds: float = 25.0, one_in: int = 3, member_window_seconds: float = 300.0) -> None:
        self.gap_seconds = float(gap_seconds)
        self.one_in = max(1, int(one_in))
        self.member_window_seconds = float(member_window_seconds)
        self._last_reply: dict[int, float] = {}
        self._since_reply: dict[tuple[int, int], int] = {}

    def note_member_message(self, *, channel_id: int, author_id: int) -> None:
        key = (int(channel_id), int(author_id))
        if key in self._since_reply:
            self._since_reply[key] += 1

    def check(self, *, channel_id: int, author_id: int, now: datetime | None = None) -> BudgetDecision:
        moment = _epoch(now)
        last = self._last_reply.get(int(channel_id))
        if last is not None:
            elapsed = moment - last
            if elapsed < self.gap_seconds:
                return BudgetDecision(
                    False, "channel gap", retry_after_seconds=max(1, int(self.gap_seconds - elapsed))
                )
        since = self._since_reply.get((int(channel_id), int(author_id)))
        if since is not None and since < self.one_in:
            return BudgetDecision(
                False,
                f"waiting for {self.one_in} new messages from this member ({since}/{self.one_in})",
            )
        return BudgetDecision(True)

    def note_reply(self, *, channel_id: int, author_id: int, now: datetime | None = None) -> None:
        self._last_reply[int(channel_id)] = _epoch(now)
        self._since_reply[(int(channel_id), int(author_id))] = 0

    def reset(self) -> None:
        self._last_reply.clear()
        self._since_reply.clear()


# --- troll tracker ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrollVerdict:
    disengage: bool
    reason: str = ""
    cooldown_seconds: int = 0
    evidence: tuple[str, ...] = field(default_factory=tuple)

    def __bool__(self) -> bool:  # so `if verdict:` reads naturally
        return self.disengage


class TrollTracker:
    """Spot a member who is only trolling, and stop answering them for a while.

    Signals (any one is enough):
    - the same normalised text three times inside ``repeat_window_seconds``;
    - ``flood_messages`` messages inside ``flood_window_seconds`` in one channel;
    - three or more insult-only lines that are jabs at the bot.

    Nothing is said in the channel. The cooldown is silent by design: an announcement would be the
    attention the troll is after, and the moderation ladder handles the rule side separately.
    """

    def __init__(
        self,
        *,
        repeat_window_seconds: float = 600.0,
        repeat_threshold: int = 3,
        flood_window_seconds: float = 60.0,
        flood_messages: int = 6,
        jab_threshold: int = 3,
        jab_window_seconds: float = 900.0,
        cooldown_seconds: float = 900.0,
        max_authors: int = 5000,
    ) -> None:
        self.repeat_window_seconds = float(repeat_window_seconds)
        self.repeat_threshold = int(repeat_threshold)
        self.flood_window_seconds = float(flood_window_seconds)
        self.flood_messages = int(flood_messages)
        self.jab_threshold = int(jab_threshold)
        self.jab_window_seconds = float(jab_window_seconds)
        self.cooldown_seconds = float(cooldown_seconds)
        self.max_authors = int(max_authors)
        self._lines: dict[int, deque[tuple[float, str]]] = defaultdict(deque)
        self._arrivals: dict[int, deque[float]] = defaultdict(deque)
        self._jabs: dict[int, deque[float]] = defaultdict(deque)
        self._muted: dict[int, tuple[float, str]] = {}

    def _trim(self, author_id: int) -> None:
        if len(self._lines) > self.max_authors:
            stale = min(self._lines.items(), key=lambda item: item[1][-1][0] if item[1] else 0.0)
            self._lines.pop(stale[0], None)
            self._arrivals.pop(stale[0], None)
            self._jabs.pop(stale[0], None)

    def observe(
        self, *, author_id: int, channel_id: int, text: str, now: datetime | None = None
    ) -> TrollVerdict:
        """Record a member message; return a disengage verdict when a signal crosses its threshold."""
        moment = _epoch(now)
        author = int(author_id)
        self._trim(author)

        arrivals = self._arrivals[author]
        arrivals.append(moment)
        while arrivals and moment - arrivals[0] > self.flood_window_seconds:
            arrivals.popleft()

        lines = self._lines[author]
        wanted = normalize(text)
        if wanted:
            lines.append((moment, wanted))
        while lines and moment - lines[0][0] > self.repeat_window_seconds:
            lines.popleft()

        if is_jab_at_bot(text) or looks_like_insult(text):
            jabs = self._jabs[author]
            jabs.append(moment)
            while jabs and moment - jabs[0] > self.jab_window_seconds:
                jabs.popleft()
        jabs = self._jabs[author]

        reason = ""
        if wanted and sum(1 for _t, line in lines if line == wanted) >= self.repeat_threshold:
            reason = "repeating the same line"
        elif len(arrivals) >= self.flood_messages:
            reason = "flooding the channel"
        elif len(jabs) >= self.jab_threshold:
            reason = "only posting insults"

        if reason:
            self._muted[author] = (moment + self.cooldown_seconds, reason)
            return TrollVerdict(
                True,
                reason,
                int(self.cooldown_seconds),
                tuple(line for _t, line in list(lines)[-5:]),
            )
        return TrollVerdict(False)

    def muted(self, author_id: int, *, now: datetime | None = None) -> TrollVerdict:
        """Whether this member is inside a disengage cooldown (checked before every casual reply)."""
        moment = _epoch(now)
        entry = self._muted.get(int(author_id))
        if entry is None:
            return TrollVerdict(False)
        until, reason = entry
        if moment >= until:
            self._muted.pop(int(author_id), None)
            return TrollVerdict(False)
        return TrollVerdict(True, reason, int(until - moment))

    def clear(self, author_id: int | None = None) -> None:
        if author_id is None:
            self._lines.clear()
            self._arrivals.clear()
            self._jabs.clear()
            self._muted.clear()
            return
        author = int(author_id)
        self._lines.pop(author, None)
        self._arrivals.pop(author, None)
        self._jabs.pop(author, None)
        self._muted.pop(author, None)


# --- one object the bot holds ----------------------------------------------------------------


class ChatGate:
    """The four gates plus the troll tracker, wired to the bot's settings."""

    def __init__(
        self,
        *,
        repeat_window_seconds: float = 180.0,
        reply_gap_seconds: float = 25.0,
        one_in: int = 3,
        troll_cooldown_seconds: float = 900.0,
        flood_messages: int = 6,
        enabled: bool = True,
    ) -> None:
        self.enabled = bool(enabled)
        self.repeat = RepeatWindow(window_seconds=repeat_window_seconds)
        self.inflight = InFlight()
        self.budget = ReplyBudget(gap_seconds=reply_gap_seconds, one_in=one_in)
        self.trolls = TrollTracker(cooldown_seconds=troll_cooldown_seconds, flood_messages=flood_messages)

    def observe(self, *, channel_id: int, author_id: int, text: str, now: datetime | None = None) -> TrollVerdict:
        self.budget.note_member_message(channel_id=channel_id, author_id=author_id)
        return self.trolls.observe(author_id=author_id, channel_id=channel_id, text=text, now=now)

    def casual_allowed(
        self, *, channel_id: int, author_id: int, text: str, now: datetime | None = None
    ) -> tuple[bool, str]:
        """Full gate order for a casual reply. (False, reason) means stay silent."""
        if not self.enabled:
            return True, ""
        if self.trolls.muted(author_id, now=now):
            return False, "member in troll cooldown"
        if self.repeat.is_repeat(channel_id=channel_id, author_id=author_id, text=text, now=now):
            return False, "repeat of a message already answered"
        if self.inflight.busy(channel_id=channel_id, text=text):
            return False, "answer already being generated"
        decision = self.budget.check(channel_id=channel_id, author_id=author_id, now=now)
        if not decision.allowed:
            return False, decision.reason
        return True, ""

    def note_answered(self, *, channel_id: int, author_id: int, text: str, now: datetime | None = None) -> None:
        self.repeat.note(channel_id=channel_id, author_id=author_id, text=text, now=now)
        self.budget.note_reply(channel_id=channel_id, author_id=author_id, now=now)

    def reset(self) -> None:
        self.repeat = RepeatWindow(window_seconds=self.repeat.window_seconds)
        self.inflight = InFlight()
        self.budget.reset()
        self.trolls.clear()
