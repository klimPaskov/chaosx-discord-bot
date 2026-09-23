"""Consistent structure for ChaosX messages.

Hoops (2026-09-23): "the bot messages should have more structure, so like headers, right now everything is
flat." Discord renders `##`/`###` headings, `**bold**`, `-`/numbered lists and `-#` small print, so every
scripted surface and every model-written answer uses the same shapes instead of a wall of prose.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

BULLET = "•"


def heading(text: str, emoji: str = "") -> str:
    """The top heading of a message (`##`, the largest Discord renders in a message)."""
    label = f"{emoji} {text}".strip() if emoji else text
    return f"## {label}"


def section(text: str, emoji: str = "") -> str:
    """A sub-heading inside one message (`###`)."""
    label = f"{emoji} {text}".strip() if emoji else text
    return f"### {label}"


def bullets(items: Iterable[str], bullet: str = BULLET) -> list[str]:
    """One bullet per non-empty item."""
    return [f"{bullet} {item}" for item in items if str(item).strip()]


def numbered(items: Iterable[str]) -> list[str]:
    """Ordered steps - use when the sequence matters."""
    return [f"{index}. {item}" for index, item in enumerate(items, start=1)]


def kv(label: str, value: str, emoji: str = "") -> str:
    """A bolded label followed by its value (`**Chaos:** 690`)."""
    prefix = f"{emoji} " if emoji else ""
    return f"{prefix}**{label}:** {value}"


def small(text: str) -> str:
    """Discord small print (`-#`) - for footnotes and caveats."""
    return f"-# {text}"


def table(rows: Sequence[tuple[str, str]], emoji: str = "") -> list[str]:
    """A two-column list rendered as labelled bullets (Discord has no real tables)."""
    return [kv(left, right, emoji=emoji) for left, right in rows]


_HEADING_RE = re.compile(r"^\s*#{2,3}\s+(?P<text>.*)$")
_BULLET_RE = re.compile(r"^(?P<indent>\s*)(?P<marker>[-•*]|\d+\.)\s+(?P<text>.*)$")


def _leading_emoji(text: str) -> str:
    parts = text.strip().split(" ", 1)
    if not parts:
        return ""
    candidate = parts[0]
    return candidate if candidate and not candidate.isascii() else ""


def fix_duplicate_emoji(text: str) -> str:
    """Drop a bullet's emoji when the heading above it already uses that emoji.

    Hoops (2026-09-23): "🔎 What's next / 🔎 Testing focus … Duplicate emojis, this shouldn't happen." The
    prompt asks for the heading emoji only, but the model repeats it now and then - this makes it
    impossible rather than merely discouraged.
    """
    lines = str(text or "").splitlines()
    out: list[str] = []
    heading_emoji = ""
    for line in lines:
        match = _HEADING_RE.match(line)
        if match:
            heading_emoji = _leading_emoji(match.group("text"))
            out.append(line)
            continue
        if not line.strip():
            out.append(line)
            continue
        bullet = _BULLET_RE.match(line)
        if heading_emoji:
            if bullet:
                text_part = bullet.group("text")
                if _leading_emoji(text_part) == heading_emoji:
                    stripped = text_part.split(" ", 1)[1].strip() if " " in text_part else ""
                    line = f"{bullet.group('indent')}{bullet.group('marker')} {stripped}".rstrip()
            else:
                # a plain line under the heading (the model often writes "🔎 Testing focus: …" with no marker)
                indent = line[: len(line) - len(line.lstrip())]
                text_part = line.strip()
                if _leading_emoji(text_part) == heading_emoji:
                    stripped = text_part.split(" ", 1)[1].strip() if " " in text_part else ""
                    line = f"{indent}{stripped}".rstrip()
        out.append(line)
    return "\n".join(out)


def block(*parts: object) -> str:
    """Join message parts with a blank line between them, dropping anything empty.

    Lists become their own lines, so a caller can pass headings, bullet lists and paragraphs without
    hand-writing the blank lines.
    """
    chunks: list[str] = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, (list, tuple)):
            text = "\n".join(str(item) for item in part if str(item).strip())
        else:
            text = str(part).strip()
        if text:
            chunks.append(text)
    return "\n\n".join(chunks)


# Appended to the system boundary so every model-written answer is skimmable instead of flat prose.
STRUCTURE_RULE = (
    "Format replies so they can be skimmed: open with a one-line answer, then use `##` or `###` headings "
    "with `-` bullets (numbered steps when the order matters) for anything with more than two points, and "
    "bold the key term in a bullet. Keep headings short, never nest more than two levels, and skip "
    "formatting entirely for a one-line answer, a quick confirmation, or casual banter."
)
