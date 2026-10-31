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


def scrub_names(text: str, names: Iterable[str], replacement: str = "a member") -> str:
    """Replace configured display names in generated text.

    Hoops (2026-09-23): "holly must never be mentioned." The facts are already filtered, so this is the
    last line of defence for a model that writes a name from elsewhere in its context. Whole-name matching
    only, so a longer name containing the configured text is untouched; IDs and mentions are left to the
    mention stripper.
    """
    cleaned = str(text or "")
    for name in names:
        name = str(name).strip()
        if len(name) < 2:
            continue
        cleaned = re.sub(rf"(?<![\w@]){re.escape(name)}(?![\w])", replacement, cleaned)
    return cleaned


# --- Discord markdown repair -------------------------------------------------
# Model-written answers keep producing markdown Discord renders badly: `####` headings, a heading glued
# to the paragraph above it, `[source]` citations that look like broken links, and bare identifiers
# (`hoi4_portrait_source.json`) whose underscores Discord turns into italics. Hoops (2026-10-24):
# "the bot has broken markdown formatting when it is responding." Repair it before sending instead of
# hoping the prompt is obeyed.
_CODE_SEGMENT_RE = re.compile(r"(```.*?```|`[^`\n]*`)", re.S)
_DEEPER_HEADING_RE = re.compile(r"(?m)^#{4,}\s*")
_GLUED_HEADING_RE = re.compile(r"(?m)([^\n])\n(#{1,3} )")
_CITATION_RE = re.compile(r"(?<![\w\]`])\[([^\]\n]{2,60})\](?!\()")
_UNDERSCORE_IDENT_RE = re.compile(
    r"(?<![\w`./\\-])([A-Za-z0-9][\w./\\-]*_[A-Za-z0-9_./\\-]+)(?![\w`])"
)
# Hoops, 2026-10-24: "The bot doesn't need to show where it gathered context from. It is not useful for a
# user." The prompt is told not to do it; these strip what still slips through — a trailing "(HOI4 Live Tools
# readme)", a "(Event 042 spec)", or a whole "Source: docs/…" line.
_PROVENANCE_LINE_RE = re.compile(r"(?im)^\s*(?:sources?|sources? used|references?)\s*:.*(?:\n|$)")
_PROVENANCE_PAREN_RE = re.compile(
    r"\s*\((?:[^()\n]{1,60}?)\b(?:readme|guidance|guide|specs?|docs?|documentation|wiki|vault|repo)\b[^()\n]{0,40}\)",
    re.I,
)
_PROVENANCE_LEAD_RE = re.compile(
    r"(?i)\baccording to (?:the )?[^.,;\n]{1,60}?\b(?:readme|guide|specs?|docs?|documentation|wiki)\b,?\s*(\S)"
)


def _drop_attribution(match: "re.Match[str]") -> str:
    """Remove "According to the X readme, ..." and capitalise what becomes the sentence start."""
    following = match.group(1)
    before = match.string[: match.start()].rstrip()
    at_sentence_start = (not before) or before[-1] in ".!?" or before.endswith("\n")
    return following.upper() if at_sentence_start and following.islower() else following


def _repair_prose(segment: str) -> str:
    segment = _PROVENANCE_LINE_RE.sub("", segment)
    segment = _PROVENANCE_PAREN_RE.sub("", segment)
    segment = _PROVENANCE_LEAD_RE.sub(_drop_attribution, segment)
    segment = _GLUED_HEADING_RE.sub(r"\1\n\n\2", segment)
    segment = _DEEPER_HEADING_RE.sub("### ", segment)
    # `[source label]` without a following URL looks like a broken link; parentheses read as a note.
    segment = _CITATION_RE.sub(lambda m: f"({m.group(1).strip()})", segment)
    segment = _UNDERSCORE_IDENT_RE.sub(lambda m: f"`{m.group(1)}`", segment)
    return segment


def repair_discord_markdown(text: str) -> str:
    """Make model-written markdown render correctly in Discord.

    Code spans and fenced blocks are left byte-for-byte alone, so a path or command that is already
    backticked cannot be double-wrapped. Never raises: formatting must not cost a member their answer.
    """
    try:
        body = str(text or "")
        if not body:
            return body
        parts = _CODE_SEGMENT_RE.split(body)
        repaired = "".join(
            part if index % 2 else _repair_prose(part) for index, part in enumerate(parts)
        )
        repaired = re.sub(r"\n{3,}", "\n\n", repaired).strip()
        # An odd number of `**` leaves bold open to the end of the message: close it on that line.
        if repaired.count("**") % 2:
            lines = repaired.split("\n")
            for index, line in enumerate(lines):
                if line.count("**") % 2:
                    lines[index] = line.rstrip() + "**"
                    break
            repaired = "\n".join(lines)
        return repaired
    except Exception:  # pragma: no cover - defensive
        return str(text or "")


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
    "STYLE: answer like a knowledgeable member chatting in Discord, not like a document. Lead with the direct "
    "answer. Match the length to the question: a simple question gets one or two short paragraphs (usually under "
    "120 words); a complex HOI4 modding question may run longer, but only with details that change what the "
    "reader does. Write plain sentences. Use a short list only for real steps or three or more separate items, and "
    "no headings unless a long answer has several distinct parts. No opening label or preamble (\"Short answer:\", "
    "\"Great question\"), no closing summary, offer or sign-off (\"Let me know if...\", \"Hope this helps\"), and "
    "never repeat what this conversation already said - build on it instead. Skip formatting entirely for a "
    "one-line answer, a quick confirmation, or casual banter."
)

# Having live data available is not the same as being asked for it. Every ask path carries a live block so the
# answer can never deny current values; without this rule the model recites that block at the first opportunity —
# a question about an unreadable video produced a full server status report plus unsolicited legal commentary.
RELEVANCE_RULE = (
    "Answer the question that was asked, and nothing else. Do not append a status report, activity figures, "
    "counts, member numbers, or project updates that nobody asked for: if the question was not about status, do "
    "not report status. Do not volunteer opinions, warnings, advice, or concerns the message did not raise — no "
    "legal, licensing, EULA, copyright, business or risk commentary, no speculation about other games or "
    "projects, no telling people what to avoid. Do not invoke, cite, or paraphrase server rules in an ordinary "
    "answer: rules belong only in a moderation reminder, and only when one was actually broken. Stick to the "
    "topic of the message — never pivot to what else is happening in the server or the project. Never claim you "
    "cannot access live or current information, and never announce a limitation about fetching data: either you "
    "have current values or you answer with what you know without commenting on what you can reach. If you could "
    "not retrieve something (a link, video, file, or search), say so in one line and stop; never fill the space "
    "with unrelated data. One question, one answer."
)

CAPABILITY_RULE = (
    "CAPABILITIES: you can read live server, GitHub and playtest data, the Chaos Redux knowledge base, and the "
    "web. Never claim you cannot access something, and never announce a limitation about what you can reach, "
    "unless a tool actually failed. If a tool failed, name that tool in one line and answer with what you have. "
    "A failed download or a blocked page is not a missing capability."
)

GROUNDING_RULE = (
    "GROUNDING: if the knowledge base had nothing on the point, say so in one line rather than filling the gap "
    "with general knowledge. Refuse only when the thing genuinely does not exist after checking the knowledge "
    "base, the catalogs and the live data; otherwise answer with what you found and name what is missing. "
    "TOOL CAPABILITIES: a \"Capabilities found in the project repositories\" list in your material is generated "
    "from the current repositories and outranks older notes. Describe what is listed with its real status - a "
    "documented agent workflow (skill), a workflow with implementation code in the repo, or experimental - and "
    "never upgrade a documented workflow into a finished one-click feature. Never state that a project tool, "
    "workflow or feature does not exist or is not covered: if your material does not show it, say you are not "
    "sure it exists rather than denying it."
)


# Labels and sign-offs the model adds on its own. The prompt asks it not to; this makes sure (Hoops, 2026-10-31:
# answers must be direct, with no unnecessary introductory or concluding sentences).
_LEAD_LABEL_RE = re.compile(
    r"^\s*(?:\*\*)?(?:short|straight|quick|simple|honest|brief)\s+(?:answer|version|take)(?:\*\*)?\s*[:\-—–]+\s*",
    re.IGNORECASE,
)
_LEAD_FILLER_RE = re.compile(r"^\s*(?:great|good|fair|nice) question[!.,—–-]*\s*", re.IGNORECASE)
_TRAIL_FILLER_RE = re.compile(
    r"\n+\s*(?:let me know if[^\n]*|hope (?:this|that) helps[^\n]*|feel free to ask[^\n]*|"
    r"happy to (?:help|go deeper|dig in)[^\n]*|(?:if you (?:want|need|have)[^\n]{0,80}(?:just )?(?:ask|let me know)[^\n]*))\s*$",
    re.IGNORECASE,
)


def trim_answer_filler(text: str) -> str:
    """Drop a leading \"Short answer:\" style label and a trailing \"let me know\" sign-off."""
    value = text or ""
    stripped = _LEAD_LABEL_RE.sub("", value, count=1)
    stripped = _LEAD_FILLER_RE.sub("", stripped, count=1)
    if stripped != value and stripped[:1].islower():
        stripped = stripped[:1].upper() + stripped[1:]
    for _ in range(2):
        updated = _TRAIL_FILLER_RE.sub("", stripped)
        if updated == stripped:
            break
        stripped = updated
    return stripped.strip() or value
