from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path


DISALLOWED_OUTPUT_MARKERS = (
    "not enough to save",
    "not enough information",
    "not related to chaos redux",
    "off-topic",
    "i can only answer chaos redux",
    "cannot help",
    "can't help",
)


@dataclass(frozen=True)
class NoteWriteResult:
    path: Path
    created: bool


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_note_id(*parts: object) -> str:
    raw = "\n".join(str(part or "") for part in parts)
    return sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:10]


def sanitize_text(value: object, *, limit: int = 4000) -> str:
    text = str(value or "")
    text = text.replace("@everyone", "＠everyone").replace("@here", "＠here")
    text = re.sub(r"<@!?(\d{15,25})>", r"user:\1", text)
    text = re.sub(r"<#(\d{15,25})>", r"channel:\1", text)
    text = re.sub(r"(?i)(token|password|secret|api[_-]?key|authorization|cookie)\s*[:=]\s*\S+", r"\1=[REDACTED]", text)
    return text.strip()[:limit]


def slugify(value: str, *, fallback: str = "untitled", limit: int = 70) -> str:
    value = sanitize_text(value, limit=200)
    value = re.sub(r"[`*_~>#\[\]{}|\\/]", " ", value)
    value = re.sub(r"[^\w\s.!'()-]+", "", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = fallback
    return value[:limit].strip(" .") or fallback


def extract_title(draft: str, raw: str, *, fallback_prefix: str) -> str:
    for pattern in (
        r"(?im)^#\s+(.+)$",
        r"(?im)^[-*]\s*(?:Event name|Name|Title)\s*:\s*(.+)$",
        r"(?im)^(?:Event name|Name|Title)\s*:\s*(.+)$",
    ):
        match = re.search(pattern, draft or "")
        if match:
            title = match.group(1).strip(" `*_#")
            if title and title.casefold() not in {"tbd", "unknown", "unnamed"}:
                return slugify(title, fallback=fallback_prefix)
    words = " ".join(str(raw or "").split())
    return slugify(words[:80], fallback=fallback_prefix)


def should_write_approved_note(raw: str, draft: str) -> bool:
    if len(str(raw or "").strip()) < 8:
        return False
    if len(str(draft or "").strip()) < 8:
        return False
    lowered = draft.casefold()
    return not any(marker in lowered for marker in DISALLOWED_OUTPUT_MARKERS)


def fenced(text: str, *, limit: int = 4000) -> str:
    text = sanitize_text(text, limit=limit)
    if "```" in text:
        text = text.replace("```", "'''" )
    return f"```text\n{text}\n```"


def discord_quote(text: object, *, limit: int = 700) -> str:
    quoted = sanitize_text(text, limit=limit)
    if not quoted:
        return "> Not supplied."
    return "\n".join(f"> {line}" if line else ">" for line in quoted.splitlines())


def format_event_idea_post_title(*, raw_idea: str, draft: str) -> str:
    title = extract_title(draft, raw_idea, fallback_prefix="Community Event Idea")
    title = re.sub(r"\s+", " ", sanitize_text(title, limit=120)).strip(" `*_#")
    return (title or "Community Event Idea")[:95]


def format_event_idea_post_body(*, raw_idea: str, draft: str, actor_id: int | None = None) -> str:
    submitter = f"user:{actor_id}" if actor_id else "unknown Discord user"
    return f"""**New approved Chaos Redux event idea**
Submitted through `/event-idea` by {submitter}.

**Original idea**
{discord_quote(raw_idea, limit=900)}

**Formatted draft**
{sanitize_text(draft, limit=7000)}

_Review note: this is an approved community idea for discussion/review, not a release promise._
""".strip()


def write_unique_note(vault_path: Path, relative_folder: str, filename: str, content: str) -> NoteWriteResult:
    root = vault_path.expanduser().resolve()
    folder = (root / relative_folder).resolve()
    if root not in folder.parents and folder != root:
        raise ValueError("note folder escapes the configured vault")
    folder.mkdir(parents=True, exist_ok=True)
    path = (folder / filename).resolve()
    if folder not in path.parents:
        raise ValueError("note path escapes the configured folder")
    if path.exists():
        return NoteWriteResult(path=path, created=False)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
    return NoteWriteResult(path=path, created=True)


def format_event_idea_note(
    *,
    title: str,
    note_id: str,
    raw_idea: str,
    draft: str,
    actor_id: int,
    guild_id: int | None,
    channel_id: int | None,
    event_type: str = "",
    cluster: str = "",
    evo_i: str = "",
    evo_ii: str = "",
    evo_iii: str = "",
    evo_iv: str = "",
    evo_v: str = "",
    world_end: str = "",
    triggerable_scenario: str = "",
    easter_egg: str = "",
) -> str:
    captured = now_iso()
    event_type = sanitize_text(event_type or "TBD", limit=160)
    cluster = sanitize_text(cluster or "Not specified.", limit=240)
    evolution_lines = [
        ("Baseline", "See AI draft below; needs human expansion."),
        ("Evolution I", evo_i),
        ("Evolution II", evo_ii),
        ("Evolution III", evo_iii),
        ("Evolution IV", evo_iv),
        ("Evolution V", evo_v),
    ]
    evo_block = "\n\n".join(f"### {name}\n\n{sanitize_text(value or 'TBD / not supplied.', limit=1200)}" for name, value in evolution_lines)
    extra_bits = []
    if triggerable_scenario:
        extra_bits.append(f"- Triggerable scenario: {sanitize_text(triggerable_scenario, limit=1000)}")
    if easter_egg:
        extra_bits.append(f"- Easter egg: {sanitize_text(easter_egg, limit=1000)}")
    extra_text = "\n".join(extra_bits) if extra_bits else "- None supplied."
    return f"""# {title}

## Catalog entry

- Event ID: `TBD`
- Event name: {title}
- Type: {event_type}
- Status: Community idea / Needs review
- Cluster: {cluster}

## Source

- Source: Discord `/event-idea`
- Reporter Discord ID: `{actor_id}`
- Guild ID: `{guild_id or 'unknown'}`
- Channel ID: `{channel_id or 'unknown'}`
- Captured: {captured}
- Community note ID: `{note_id}`

### Original idea

{fenced(raw_idea, limit=4000)}

## Details

{sanitize_text(draft, limit=8000)}

## Evolutions

{evo_block}

## World-end scenario

{sanitize_text(world_end or 'TBD / not supplied.', limit=2000)}

## Scenario / easter egg hooks

{extra_text}

## Cluster notes

{cluster}

## Review notes

- Created automatically by ChaosX from an approved community event idea.
- Needs human review before catalog assignment, implementation, or public commitment.
- Do not assign a real event ID until this is accepted into the main event catalog.
"""


def write_event_idea_note(
    *,
    vault_path: Path,
    event_specs_folder: str,
    raw_idea: str,
    draft: str,
    actor_id: int,
    guild_id: int | None,
    channel_id: int | None,
    event_type: str = "",
    cluster: str = "",
    evo_i: str = "",
    evo_ii: str = "",
    evo_iii: str = "",
    evo_iv: str = "",
    evo_v: str = "",
    world_end: str = "",
    triggerable_scenario: str = "",
    easter_egg: str = "",
) -> NoteWriteResult | None:
    if not should_write_approved_note(raw_idea, draft):
        return None
    note_id = stable_note_id("event-idea", actor_id, raw_idea)
    title = extract_title(draft, raw_idea, fallback_prefix="Community Event Idea")
    filename = f"Community Idea - {slugify(title, fallback='Community Event Idea')} - {note_id}.md"
    content = format_event_idea_note(
        title=title,
        note_id=note_id,
        raw_idea=raw_idea,
        draft=draft,
        actor_id=actor_id,
        guild_id=guild_id,
        channel_id=channel_id,
        event_type=event_type,
        cluster=cluster,
        evo_i=evo_i,
        evo_ii=evo_ii,
        evo_iii=evo_iii,
        evo_iv=evo_iv,
        evo_v=evo_v,
        world_end=world_end,
        triggerable_scenario=triggerable_scenario,
        easter_egg=easter_egg,
    )
    return write_unique_note(vault_path, event_specs_folder, filename, content)


def format_suggestion_note(
    *,
    title: str,
    note_id: str,
    raw_suggestion: str,
    draft: str,
    actor_id: int,
    guild_id: int | None,
    channel_id: int | None,
) -> str:
    captured = now_iso()
    return f"""---
title: "{title.replace('"', "'")}"
created: {captured[:10]}
updated: {captured[:10]}
type: community-suggestion
status: needs-review
tags: [chaos-redux, community, suggestion]
sources: [discord]
confidence: medium
---

# {title}

## Source

- Source: Discord `/suggestion`
- Reporter Discord ID: `{actor_id}`
- Guild ID: `{guild_id or 'unknown'}`
- Channel ID: `{channel_id or 'unknown'}`
- Captured: {captured}
- Community note ID: `{note_id}`

## Original suggestion

{fenced(raw_suggestion, limit=4000)}

## Cleaned suggestion

{sanitize_text(draft, limit=8000)}

## Review notes

- Created automatically by ChaosX from an approved community suggestion.
- Needs human review before implementation or public commitment.
"""


def write_suggestion_note(
    *,
    vault_path: Path,
    suggestions_folder: str,
    raw_suggestion: str,
    draft: str,
    actor_id: int,
    guild_id: int | None,
    channel_id: int | None,
) -> NoteWriteResult | None:
    if not should_write_approved_note(raw_suggestion, draft):
        return None
    note_id = stable_note_id("suggestion", actor_id, raw_suggestion)
    title = extract_title(draft, raw_suggestion, fallback_prefix="Community Suggestion")
    filename = f"{slugify(title, fallback='Community Suggestion')} - {note_id}.md"
    content = format_suggestion_note(
        title=title,
        note_id=note_id,
        raw_suggestion=raw_suggestion,
        draft=draft,
        actor_id=actor_id,
        guild_id=guild_id,
        channel_id=channel_id,
    )
    return write_unique_note(vault_path, suggestions_folder, filename, content)
