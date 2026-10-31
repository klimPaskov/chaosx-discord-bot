"""When an answer may carry a link, and keeping Discord from unfurling the ones it does carry.

Hoops (2026-10-31): the bot linked his projects, tools and tutorials in almost every answer, and every link
unfurled into a preview card. Two rules came out of that:

- Official project links appear when a member asks for a tool, app, repository, download, tutorial or video,
  or when the link is the answer. A follow-up about a feature of a tool that was already linked gets the
  explanation, not the link again.
- Bot messages are plain text: link previews are suppressed on every send that does not carry an embed of its
  own (`install_plain_message_defaults`). The few surfaces that genuinely need an embed pass one explicitly and
  keep it.

The prompt asks the model for the same behaviour, but a prompt is not a guarantee, so `apply_link_policy` is the
deterministic pass every public answer goes through before it is sent.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from typing import Iterable

from .resources import OFFICIAL_RESOURCES

# How long a link counts as "already given" in a channel. Long enough to cover a conversation, short enough that
# somebody asking again tomorrow gets the link.
RECENT_LINK_WINDOW_SECONDS = 6 * 3600
_MAX_LINKS_PER_CHANNEL = 50

_URL_RE = re.compile(r"<?(https?://[^\s<>()\[\]]+[^\s<>()\[\].,;:!?'\"*_`])>?")
_MD_LINK_RE = re.compile(r"\[([^\]\n]{1,200})\]\(<?(https?://[^\s)>]+)>?\)")

# Hosts and prefixes that make a URL one of the project's own resources. Anything else (a web source the answer
# was built from, a Paradox wiki page) is left alone — the complaint was self-promotion, not citations.
_OFFICIAL_PREFIXES = tuple(
    sorted(
        {r.url.rstrip("/").lower() for r in OFFICIAL_RESOURCES}
        | {
            "https://github.com/klimpaskov",
            "https://klimpaskov.github.io",
            "https://www.youtube.com/@chaosredux",
            "https://ko-fi.com/klimpaskov",
            "https://huggingface.co/hoops-mccann",
            "https://discord.gg/raxesgct2t",
        },
        key=len,
        reverse=True,
    )
)

# The member is asking for the thing a link points at.
_LINK_REQUEST_RE = re.compile(
    r"\b(?:links?|urls?|download(?:s|ing)?|repos?|repository|repositories|github|git ?hub|tutorials?|videos?|"
    r"playlist|devlogs?|youtube|guide|docs|documentation|website|site|page|invite|ko-?fi|donat\w*|support the|"
    r"releases?|installers?|where (?:can|do|should) i (?:get|find|download|grab|watch|see|read)|"
    r"how (?:do|can) i (?:get|install|download)|send (?:me|it|that|the)|share (?:it|that|the)|"
    r"give me (?:the|a)|point me)\b",
    re.IGNORECASE,
)

# Explicitly asking for something already linked: "send that link again", "what was the repo link?".
_REPEAT_REQUEST_RE = re.compile(
    r"\b(?:links?|urls?|again|resend|re-send|send (?:me|it|that|the)|repost|what was the|where was the)\b",
    re.IGNORECASE,
)

# "What is X?" / "tell me about X" for one of the official tools counts as asking for the tool itself.
_INTRO_RE = re.compile(r"\b(?:what(?:'s| is| are)|tell me about|introduce|explain what)\b", re.IGNORECASE)

# Sentence fragments whose only job is to point at a link. When the link goes, the pointer goes with it.
_STRONG_POINTER_RE = re.compile(
    r"\b(?:is|are) (?:right )?here\b|\bhere\s*[:—–-]|\bcheck (?:it |them |that )?out\b|\bavailable (?:at|on|here)\b|"
    r"\b(?:grab|find|get|download) (?:it|them)\b|\b(?:the|this|that|a) links?\b|\b(?:lives|is|are) at\b|\bin the playlist\b|"
    r"\bstart here\b|\bfor reference\b|\bif you want to (?:dig|read|watch)\b",
    re.IGNORECASE,
)
_POINTER_RE = re.compile(
    r"\b(?:here|repo(?:sitory)?|guide|walkthrough|tutorial|video|page|playlist|see|watch|download)\b",
    re.IGNORECASE,
)

_recent_lock = threading.Lock()
_recent: dict[int, deque[tuple[float, str]]] = {}


def normalize_url(url: str) -> str:
    return url.strip().strip("<>").rstrip("/").lower()


def is_official_url(url: str) -> bool:
    value = normalize_url(url)
    return any(value.startswith(prefix) for prefix in _OFFICIAL_PREFIXES)


def urls_in(text: str) -> list[str]:
    return [match.group(1) for match in _URL_RE.finditer(text or "")]


def asks_for_links(question: str) -> bool:
    """True when the member is asking for a resource a link would deliver."""
    text = question or ""
    if _LINK_REQUEST_RE.search(text):
        return True
    if _INTRO_RE.search(text):
        lowered = text.casefold()
        for resource in OFFICIAL_RESOURCES:
            name = resource.label.split("(")[0].strip().casefold()
            if name and name in lowered:
                return True
    return False


def asks_to_repeat(question: str) -> bool:
    return bool(_REPEAT_REQUEST_RE.search(question or ""))


def record_links(channel_id: int | None, text: str, *, now: float | None = None) -> None:
    """Remember the official links the bot just sent in a channel (or thread)."""
    if not channel_id:
        return
    found = [normalize_url(url) for url in urls_in(text) if is_official_url(url)]
    if not found:
        return
    stamp = time.time() if now is None else now
    with _recent_lock:
        bucket = _recent.setdefault(int(channel_id), deque(maxlen=_MAX_LINKS_PER_CHANNEL))
        for url in found:
            bucket.append((stamp, url))


def recent_links(channel_id: int | None, *, now: float | None = None) -> set[str]:
    if not channel_id:
        return set()
    stamp = time.time() if now is None else now
    with _recent_lock:
        bucket = _recent.get(int(channel_id))
        if not bucket:
            return set()
        return {url for when, url in bucket if stamp - when <= RECENT_LINK_WINDOW_SECONDS}


def forget_links() -> None:
    """Test helper: start from a clean slate."""
    with _recent_lock:
        _recent.clear()


def _split_sentences(line: str) -> list[str]:
    # Split after sentence punctuation followed by a space, but never inside a URL.
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(\"'*_])", line)
    return [part for part in parts if part]


def _remove_url(sentence: str, url: str) -> str:
    """Drop one URL and the connector that introduced it."""
    escaped = re.escape(url)
    patterns = (
        rf"\s*\(\s*<?{escaped}>?\s*\)",            # (url)
        rf"\s*[—–-]{{1,2}}\s*<?{escaped}>?",        # — url
        rf"\s*:\s*<?{escaped}>?",                   # : url
        rf"\s+(?:at|on|via|here)\s+<?{escaped}>?",  # at url
        rf"\s*<?{escaped}>?",
    )
    for pattern in patterns:
        updated, count = re.subn(pattern, "", sentence, count=1)
        if count:
            return updated
    return sentence


def _clean_sentence(text: str) -> str:
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,;:])\s*([.!?])", r"\2", text)
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s*[—–-]\s*$", "", text)
    text = re.sub(r"(?:,|\band)\s*$", "", text.rstrip())
    return text.strip()


def _strip_links(text: str, drop: set[str]) -> str:
    """Remove the given (normalized) official URLs, and sentences that only existed to point at them."""
    if not drop:
        return text
    # Markdown links keep their label.
    def _md(match: re.Match[str]) -> str:
        return match.group(1) if normalize_url(match.group(2)) in drop else match.group(0)

    text = _MD_LINK_RE.sub(_md, text)
    out_lines: list[str] = []
    for line in text.split("\n"):
        line_urls = [url for url in urls_in(line) if normalize_url(url) in drop]
        if not line_urls:
            out_lines.append(line)
            continue
        bullet = re.match(r"^(\s*(?:[-*•]|\d+[.)])\s+)", line)
        prefix = bullet.group(1) if bullet else ""
        body = line[len(prefix):]
        kept: list[str] = []
        for sentence in _split_sentences(body):
            sentence_urls = [url for url in urls_in(sentence) if normalize_url(url) in drop]
            if not sentence_urls:
                kept.append(sentence)
                continue
            stripped = sentence
            for url in sentence_urls:
                stripped = _remove_url(stripped, url)
            stripped = _clean_sentence(stripped)
            words = re.findall(r"[A-Za-z0-9']+", stripped)
            # A sentence that only pointed at the link has nothing left to say without it.
            if (
                len(words) <= 3
                or _STRONG_POINTER_RE.search(stripped)
                or (_POINTER_RE.search(stripped) and len(words) <= 6)
            ):
                continue
            if stripped and stripped[-1] not in ".!?:)" and sentence.rstrip()[-1:] in ".!?":
                stripped += sentence.rstrip()[-1]
            kept.append(stripped)
        rebuilt = " ".join(part for part in kept if part.strip())
        if rebuilt.strip():
            out_lines.append(prefix + rebuilt)
        # else: the whole line was a link pointer — drop it.
    result = "\n".join(out_lines)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def apply_link_policy(
    answer: str,
    *,
    question: str,
    channel_id: int | None = None,
    recent: Iterable[str] | None = None,
    now: float | None = None,
    record: bool = True,
) -> str:
    """Keep official links only where they were asked for, and never repeat one given recently.

    Non-official URLs (web sources, the Paradox wiki) are untouched. When `record` is set the links that survive
    are remembered for the channel, so the next answer there does not repeat them.
    """
    text = answer or ""
    official = {normalize_url(url) for url in urls_in(text) if is_official_url(url)}
    official |= {normalize_url(m.group(2)) for m in _MD_LINK_RE.finditer(text) if is_official_url(m.group(2))}
    if not official:
        return text
    already = {normalize_url(url) for url in (recent if recent is not None else recent_links(channel_id, now=now))}
    if not asks_for_links(question):
        drop = set(official)
    elif asks_to_repeat(question):
        drop = set()
    else:
        drop = official & already
    cleaned = _strip_links(text, drop) if drop else text
    if record:
        record_links(channel_id, cleaned, now=now)
    return cleaned or text


# --- plain messages ------------------------------------------------------------------------------------------

_INSTALLED = False


def _wants_plain(kwargs: dict) -> bool:
    return not kwargs.get("embed") and not kwargs.get("embeds") and "suppress_embeds" not in kwargs


def install_plain_message_defaults() -> None:
    """Make every bot message plain text by default: no automatic link previews.

    discord.py has no global switch, so the send entry points are wrapped once. A call that passes its own
    `embed`/`embeds` (a status panel) or decides `suppress_embeds` itself is left exactly as it was. Links the
    bot sends in a channel are also recorded here, so answers there do not repeat them.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    import discord

    original_send = discord.abc.Messageable.send

    async def send(self, content=None, **kwargs):  # type: ignore[no-untyped-def]
        if _wants_plain(kwargs):
            kwargs["suppress_embeds"] = True
        message = await original_send(self, content, **kwargs)
        try:
            if content:
                record_links(getattr(message, "channel", None) and message.channel.id, str(content))
        except Exception:
            pass
        return message

    original_webhook_send = discord.Webhook.send

    async def webhook_send(self, content=discord.utils.MISSING, **kwargs):  # type: ignore[no-untyped-def]
        if _wants_plain(kwargs):
            kwargs["suppress_embeds"] = True
        return await original_webhook_send(self, content, **kwargs)

    original_response = discord.InteractionResponse.send_message

    async def send_message(self, content=None, **kwargs):  # type: ignore[no-untyped-def]
        if _wants_plain(kwargs):
            kwargs["suppress_embeds"] = True
        return await original_response(self, content, **kwargs)

    discord.abc.Messageable.send = send  # type: ignore[method-assign]
    discord.Webhook.send = webhook_send  # type: ignore[method-assign]
    discord.InteractionResponse.send_message = send_message  # type: ignore[method-assign]
    _INSTALLED = True
