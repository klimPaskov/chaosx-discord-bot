"""Post-processing for outbound text: emoji discipline, source-tag leaks, canned-line repeats.

The prompt asks for restraint; the model does not reliably deliver it, so these rules are applied to
the finished text instead of being requested politely (Hoops, 2026-11-05: "it should not feel forced,
it should only use the server emojis when it is appropriate, not spam the same one in each response").

Nothing here rewrites an answer's meaning. It removes an icon, a bracketed source note or a repeated
canned line — the three things that made 2026-11-03 read like a machine.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from datetime import datetime, timezone

_CUSTOM_RE = re.compile(r"<a?:([A-Za-z0-9_]{2,32}):(\d{15,25})>")
_SHORTCODE_RE = re.compile(r"(?<![:a-zA-Z0-9]):([A-Za-z0-9_]{2,32}):(?![:a-zA-Z0-9])")
_UNICODE_RE = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\u2b00-\u2bff\u2700-\u27bf\ufe0f]"
)

# A bracketed/parenthesised source note is provenance the member never asked for:
# "[HOI4 Live Tools readme]", "(HOI4 Agent Tools readme)".
_SOURCE_TAG_RE = re.compile(
    r"\[\s*[^\[\]]{0,80}?\b(?:readme|notes?|docs?|documentation)\b[^\[\]]{0,80}?\s*\]"
    r"|\(\s*[^()]{0,80}?\b(?:readme|notes?|docs?|documentation)\b[^()]{0,80}?\s*\)",
    re.IGNORECASE,
)

_VERB = r"(?:checked|looked (?:at|through)|searched|read|reviewed|consulted)"
_OBJECT = (
    r"(?:the |my |our )?(?:chaos redux |project )?(?:reference )?"
    r"(?:notes|vault|material|context|knowledge base|docs)"
)

def _capitalise(match: "re.Match[str]") -> str:
    return match.group(1).upper()


_NOTES_PHRASES: tuple[tuple[re.Pattern[str], object], ...] = (
    # "I checked the Chaos Redux notes and they don't cover X" -> "The Chaos Redux material doesn't cover X"
    (
        re.compile(
            rf"(?i)\bI(?:'ve| have)? {_VERB} {_OBJECT}\b,? (?:and|but) (?:they|it) "
            r"(?:don't|do not|doesn't|does not) cover\b"
        ),
        "The Chaos Redux material doesn't cover",
    ),
    # "I checked the Chaos Redux notes, but there's no entry" -> "There's no entry"
    (
        re.compile(rf"(?i)\bI(?:'ve| have)? {_VERB} {_OBJECT}\b,? (?:and|but)\s+([a-z])"),
        _capitalise,
    ),
    # Any other first-person mention of the material becomes a plain subject.
    (re.compile(rf"(?i)\bI(?:'ve| have)? {_VERB} {_OBJECT}\b"), "The Chaos Redux material"),
    # Not first person: "that detail isn't in my reference notes".
    (
        re.compile(r"(?i)\b(?:the |my |our )?(?:chaos redux |project )?reference (?:notes|material|context)\b"),
        "the Chaos Redux material",
    ),
    (re.compile(r"(?i)\b(?:the |my |our )?chaos redux (?:notes|vault|material)\b"), "the Chaos Redux material"),
    (re.compile(r"(?i)\b(?:the |my |our )?vault\b"), "the Chaos Redux material"),
)

_SPACE_RE = re.compile(r"[ \t]{2,}")


def _icons(text: str) -> list[tuple[int, int, str]]:
    """(start, end, name) for every custom or unicode icon in the text, in order."""
    found: list[tuple[int, int, str]] = []
    for match in _CUSTOM_RE.finditer(text):
        found.append((match.start(), match.end(), match.group(1)))
    for match in _SHORTCODE_RE.finditer(text):
        found.append((match.start(), match.end(), match.group(1)))
    for match in _UNICODE_RE.finditer(text):
        found.append((match.start(), match.end(), match.group(0)))
    found.sort()
    return found


def count_icons(text: str) -> int:
    return len(_icons(text))


def icon_names(text: str) -> list[str]:
    return [name for _s, _e, name in _icons(text)]


def _sentence_count(text: str) -> int:
    body = _CUSTOM_RE.sub("", text)
    body = _SHORTCODE_RE.sub("", body)
    body = _UNICODE_RE.sub("", body).strip()
    if not body:
        return 0
    return len([part for part in re.split(r"(?<=[.!?])\s+", body) if part.strip()])


def _is_structured(text: str) -> bool:
    """Headings, bullets, numbered lists and code fences are never signed with an icon."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("#", "- ", "* ", "> ", "```")) or re.match(r"^\d+\.\s", stripped):
            return True
    return False


class EmojiPolicy:
    """At most one icon; never the same one as the last few messages; never a signature ending.

    ``recent_window`` icons per channel are remembered. A trailing icon survives only when all of
    these hold: it is not one of those, the previous bot message in the channel was not signed
    either, and the message is a short single/double-sentence line with no structure. In practice
    that yields an icon on roughly one reply in four instead of nine in ten.
    """

    def __init__(self, *, recent_window: int = 6) -> None:
        self.recent_window = max(1, int(recent_window))
        self._recent: dict[int, deque[str]] = defaultdict(lambda: deque(maxlen=self.recent_window))
        self._last_signed: dict[int, bool] = {}

    def _remember(self, channel_id: int, text: str) -> None:
        for name in icon_names(text):
            bucket = self._recent[int(channel_id)]
            if name not in bucket:
                bucket.append(name)
        self._last_signed[int(channel_id)] = bool(_icons(text))

    def recent(self, channel_id: int) -> list[str]:
        return list(self._recent[int(channel_id)])

    def seed(self, channel_id: int, text: str) -> None:
        """Remember a message the bot already sent, e.g. replayed from the archive at startup.

        Without seeding, a restart wipes the window and the first replies can reuse the icon the
        channel just saw — the exact "forced" feel this policy removes.
        """
        self._remember(int(channel_id or 0), text or "")

    def finalize(self, text: str, *, channel_id: int | None = None, casual: bool = False) -> str:
        """Return the text with the icon rules applied, and remember what was actually sent."""
        channel = int(channel_id or 0)
        body = text or ""
        icons = _icons(body)
        recent = set(self._recent[channel])
        # Remember what the model tried to use, not only what was sent: the habit is the problem, so
        # an icon that has just been suppressed stays off-limits for the next few messages too.
        for _start, _end, name in icons:
            bucket = self._recent[channel]
            if name not in bucket:
                bucket.append(name)

        if not icons:
            self._last_signed[channel] = False
            return body

        # 1. At most one icon: keep the first that is not a repeat, else none.
        keep_index = None
        for index, (_start, _end, name) in enumerate(icons):
            if name in recent:
                continue
            keep_index = index
            break
        if keep_index is None:
            body = self._remove(body, icons)
            self._last_signed[channel] = False
            return self._tidy(body)

        # Drop every other icon.
        extras = [item for index, item in enumerate(icons) if index != keep_index]
        if extras:
            body = self._remove(body, extras)
            icons = [icons[keep_index]]
        start, end, name = icons[0]

        trailing = not body[end:].strip()
        if not trailing:
            # An inline icon is tone, not a signature — keep it and stop.
            self._last_signed[channel] = False
            return self._tidy(body)
        if self._keep_trailing(body, start, end, name, channel, casual):
            self._last_signed[channel] = True
            return self._tidy(body)

        body = body[:start] + body[end:]
        self._last_signed[channel] = False
        return self._tidy(body)

    def _keep_trailing(self, body: str, start: int, end: int, name: str, channel: int, casual: bool) -> bool:
        if not casual:
            return False
        if self._last_signed.get(channel, False):
            return False
        if _is_structured(body):
            return False
        if _sentence_count(body) > 2:
            return False
        if len(body) > 240:
            return False
        return True

    def _remove(self, body: str, icons: list[tuple[int, int, str]]) -> str:
        for start, end, _name in sorted(icons, reverse=True):
            body = body[:start] + body[end:]
        return body

    @staticmethod
    def _tidy(text: str) -> str:
        text = _SPACE_RE.sub(" ", text)
        text = re.sub(r"[ \t]+([,.;:!?])", r"\1", text)
        return text.strip()


def strip_source_tags(text: str) -> str:
    """Remove bracketed source notes such as "[HOI4 Live Tools readme]"."""
    cleaned = _SOURCE_TAG_RE.sub("", text or "")
    if cleaned == text:
        return cleaned
    cleaned = _SPACE_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    return cleaned.strip()


def strip_notes_phrasing(text: str) -> str:
    """Replace "I checked the Chaos Redux notes/vault…" with neutral wording.

    The redaction has to read like a sentence afterwards: a member should never see the bot talk
    about its notes, and should also never see a half-rewritten clause.
    """
    cleaned = text or ""
    for pattern, replacement in _NOTES_PHRASES:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = re.sub(r"(?i)\bmaterial material\b", "material", cleaned)
    cleaned = re.sub(r"^\s*the Chaos Redux material", "The Chaos Redux material", cleaned)
    return cleaned


def sanitize_outbound(text: str) -> str:
    """Both leak fixes in the order that matters: tags first, then phrasing."""
    return strip_notes_phrasing(strip_source_tags(text))


class CannedSuppressor:
    """At most one canned guidance line per member per window.

    "I can only answer Chaos Redux and modding questions…" went out four times in thirty days and
    twice inside five minutes on 2026-11-03 — the second one *in reply to* a spam message, which is
    the same line the repeat gate exists to stop.
    """

    def __init__(self, *, window_seconds: float = 3600.0) -> None:
        self.window_seconds = float(window_seconds)
        self._last: dict[int, float] = {}

    def allow(self, author_id: int, now: datetime | None = None) -> bool:
        moment = (now or datetime.now(timezone.utc)).timestamp()
        last = self._last.get(int(author_id))
        if last is not None and moment - last < self.window_seconds:
            return False
        self._last[int(author_id)] = moment
        return True

    def reset(self) -> None:
        self._last.clear()
