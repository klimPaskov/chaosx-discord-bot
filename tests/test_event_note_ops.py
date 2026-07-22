from pathlib import Path

import pytest

from chaosx_bot.event_note_ops import (
    YOUR_TASK_BLOCK,
    EventNoteConflictError,
    EventNoteError,
    build_admin_event_idea_prompt,
    build_admin_event_improvement_prompt,
    create_generated_event_note,
    next_available_event_id,
    normalize_generated_event_note,
    normalize_improved_event_note,
    replace_event_note,
    resolve_event_note,
)


def idea_draft(event_id: int = 168) -> str:
    return f"""# {event_id} - The Clockwork Armistice

## Catalog entry

- Event ID: `TBD`
- Event name: Temporary Name
- Type: Major
- Status: New
- Chaos level: 4
- Cluster: Diplomacy

## General description

Every active war pauses while a strange diplomatic clock counts down.

## Baseline behaviour

A temporary armistice interrupts wars and opens competing diplomatic choices.

## Evolution stages

### Evolution I

Some states refuse the armistice and suffer escalating isolation.

## World-end scenario

Not needed because the event is a temporary diplomatic disruption.

## Manual triggerable scenario

A manual version could be exposed only if it becomes useful for testing or custom games.

## Connections

It can connect to faction tension, white peace, guarantees, and the chaos meter.
"""


def test_next_id_and_generated_note_follow_numbered_vault_standard(tmp_path: Path):
    folder = tmp_path / "Events/Event Specs"
    folder.mkdir(parents=True)
    (folder / "166 - Existing.md").write_text("# Existing\n", encoding="utf-8")
    (folder / "167 - Reserved.md").write_text("", encoding="utf-8")

    assert next_available_event_id(tmp_path, "Events/Event Specs") == 168

    result = create_generated_event_note(
        vault_path=tmp_path,
        event_specs_folder="Events/Event Specs",
        event_id=168,
        draft=idea_draft(),
    )

    assert result.path.name == "168 - The Clockwork Armistice.md"
    text = result.path.read_text(encoding="utf-8")
    assert text.startswith("# The Clockwork Armistice\n")
    assert "- Event ID: `168`" in text
    assert "- Event name: The Clockwork Armistice" in text
    assert text.endswith(YOUR_TASK_BLOCK + "\n")
    assert text.count("## Your Task") == 1
    assert "Plan ahead before writing the final package." in text


def test_generated_note_rejects_stale_allocated_id(tmp_path: Path):
    folder = tmp_path / "Events/Event Specs"
    folder.mkdir(parents=True)
    (folder / "168 - Claimed.md").write_text("# Claimed\n", encoding="utf-8")

    with pytest.raises(EventNoteConflictError, match="next available id is 169"):
        create_generated_event_note(
            vault_path=tmp_path,
            event_specs_folder="Events/Event Specs",
            event_id=168,
            draft=idea_draft(),
        )


def test_generated_note_requires_clear_event_structure(tmp_path: Path):
    folder = tmp_path / "Events/Event Specs"
    folder.mkdir(parents=True)

    with pytest.raises(EventNoteError, match="missing required sections"):
        create_generated_event_note(
            vault_path=tmp_path,
            event_specs_folder="Events/Event Specs",
            event_id=1,
            draft="# Thin Idea\n\n## Catalog entry\n\n- Event ID: `1`\n- Event name: Thin Idea\n",
        )


def test_generated_note_does_not_enforce_a_fixed_list_quota():
    large_event = idea_draft() + "\n" + "\n".join(
        f"- Distinct cross-system connection {index}" for index in range(20)
    )

    title, normalized = normalize_generated_event_note(large_event, event_id=168)

    assert title == "The Clockwork Armistice"
    assert "Distinct cross-system connection 19" in normalized


def test_event_improvement_resolves_note_and_preserves_identity_and_footer(tmp_path: Path):
    folder = tmp_path / "Events/Event Specs"
    folder.mkdir(parents=True)
    path = folder / "020 - Black Plague.md"
    path.write_text(
        "# Black Plague\n\n## Catalog entry\n\n- Event ID: `20`\n- Event name: Black Plague\n\n## Details\n\nOld ideas.\n\n"
        + YOUR_TASK_BLOCK
        + "\n",
        encoding="utf-8",
    )

    assert resolve_event_note(tmp_path, "Events/Event Specs", "020") == path
    result = replace_event_note(
        path=path,
        event_id=20,
        draft="""# Renamed by model

## Catalog entry

- Event ID: `999`
- Event name: Wrong Name

## Details

Old ideas remain, with a new connection to biological warfare containment and rat-country escalation.

## New idea connections

The disease mapmode can expose spread pressure without prescribing implementation work.

## Your Task

Wrong footer from model.
""",
    )

    text = result.path.read_text(encoding="utf-8")
    assert text.startswith("# Black Plague\n")
    assert "- Event ID: `20`" in text
    assert "- Event name: Black Plague" in text
    assert "Renamed by model" not in text
    assert "Wrong footer" not in text
    assert text.count("## Your Task") == 1
    assert text.endswith(YOUR_TASK_BLOCK + "\n")


def test_event_improvement_rejects_planning_or_coding_sections():
    with pytest.raises(EventNoteError, match="planning or coding guidance"):
        normalize_improved_event_note(
            """# Event

## Catalog entry

- Event ID: `7`
- Event name: Event

## Details

Keep this as a rough idea with enough material for validation.

## Implementation plan

Change files and add tests.
""",
            event_id=7,
            existing_title="Event",
        )


def test_event_improvement_does_not_enforce_a_fixed_length():
    stages = "\n\n".join(
        f"### Evolution {index}\n\nDistinct escalation {index} adds a necessary cross-system consequence."
        for index in range(100)
    )
    large_event = f"""# Event

## Catalog entry

- Event ID: `7`
- Event name: Event

## Details

A genuinely large event can justify a longer rough note.

{stages}
"""

    normalized = normalize_improved_event_note(
        large_event,
        event_id=7,
        existing_title="Event",
    )

    assert len(normalized) > 7_500
    assert "Evolution 99" in normalized


def test_event_improvement_allows_implementation_status_and_persists(tmp_path: Path):
    path = tmp_path / "023 - SOV Nuclear Bombs.md"
    path.write_text("# SOV Nuclear Bombs\n\n## Catalog entry\n\n- Event ID: `23`\n", encoding="utf-8")

    result = replace_event_note(
        path=path,
        event_id=23,
        draft="""# SOV Nuclear Bombs

## Catalog entry

- Event ID: `23`
- Event name: SOV Nuclear Bombs

## Details

Keep this as a rough idea with enough material for validation and expansion.

## Implementation status

The event currently exists in rough implemented form, while the new escalation ideas remain draft material.
""",
    )

    normalized = result.path.read_text(encoding="utf-8")
    assert "## Implementation status" in normalized
    assert normalized.endswith(YOUR_TASK_BLOCK + "\n")


def test_resolve_event_note_rejects_missing_and_ambiguous_ids(tmp_path: Path):
    folder = tmp_path / "Events/Event Specs"
    folder.mkdir(parents=True)
    (folder / "000 - Seed A.md").write_text("# A\n", encoding="utf-8")
    (folder / "000 - Seed B.md").write_text("# B\n", encoding="utf-8")

    with pytest.raises(EventNoteError, match="ambiguous"):
        resolve_event_note(tmp_path, "Events/Event Specs", "0")
    with pytest.raises(EventNoteError, match="No event note for id 42"):
        resolve_event_note(tmp_path, "Events/Event Specs", "42")


def test_admin_prompts_require_context_mining_and_forbid_unwanted_side_effects(tmp_path: Path):
    vault = tmp_path / "vault"
    note_path = vault / "Events/Event Specs/020 - Black Plague.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text("# Black Plague\n", encoding="utf-8")

    idea_prompt = build_admin_event_idea_prompt(
        event_id=168,
        vault_path=vault,
        event_specs_folder="Events/Event Specs",
    )
    assert "stronger owner/operator model" in idea_prompt
    assert "Broadly inspect the live Chaos Redux repo" in idea_prompt
    assert "Events/Event Catalog Index.md" in idea_prompt
    assert "Search for duplicate ideas" in idea_prompt
    assert "Follow the planning skill internally" in idea_prompt
    assert "Scale its length to the event's actual complexity" in idea_prompt
    assert "Do not use a fixed word, character, section, evolution, or bullet quota" in idea_prompt
    assert "Use bullets only when they make distinct alternatives or relationships clearer" in idea_prompt
    assert "Research broadly but synthesize narrowly" in idea_prompt
    assert "## General description" in idea_prompt
    assert "## Baseline behaviour" in idea_prompt
    assert "## Evolution stages" in idea_prompt
    assert "## World-end scenario" in idea_prompt
    assert "## Manual triggerable scenario" in idea_prompt
    assert "Do not write or modify files" in idea_prompt
    assert "post anything to Discord" in idea_prompt
    assert "Do not include a `## Your Task` section" in idea_prompt

    improvement_prompt = build_admin_event_improvement_prompt(
        event_id=20,
        note_path=note_path,
        existing_note="# Black Plague\n\n## Details\n\nRough idea.\n\n" + YOUR_TASK_BLOCK,
        vault_path=vault,
    )
    assert "No separate improvement instruction is supplied" in improvement_prompt
    assert "identify thin or unclear sections" in improvement_prompt
    assert "preserve the core useful ideas rather than every sentence" in improvement_prompt
    assert "Improving a bloated note means shortening it" in improvement_prompt
    assert "Follow the planning skill internally" in improvement_prompt
    assert "Scale length and section depth to the event's actual complexity" in improvement_prompt
    assert "Do not use a fixed word, character, section, evolution, or bullet quota" in improvement_prompt
    assert "use bullets only when they clarify distinct alternatives or relationships" in improvement_prompt
    assert "Do not enumerate every obvious consequence" in improvement_prompt
    assert "rough idea note, not a full specification" in improvement_prompt
    assert "Do not add an implementation plan, coding guidance" in improvement_prompt
    assert "Wrong footer" not in improvement_prompt
    assert "## Your Task" not in improvement_prompt.split("--- BEGIN EXISTING EVENT NOTE ---", 1)[1]
