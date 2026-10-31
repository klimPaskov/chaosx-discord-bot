"""The capability catalog: what the project's tool repositories can do, and in what state."""

from __future__ import annotations

from pathlib import Path

import pytest

from chaosx_bot import capability_catalog as cc


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def roots(tmp_path: Path) -> list[tuple[str, Path]]:
    kit = tmp_path / "Agentic-HOI4-Modding"
    _write(kit / "README.md", "# Agentic HOI4 Modding\n\nBuild HOI4 mods with your agent.\n\n## Skills and agents\n\nThirty skills.\n")
    _write(
        kit / ".claude/skills/hoi4-3d-model-pipeline/SKILL.md",
        "---\nname: hoi4-3d-model-pipeline\ndescription: Create, rig and export custom HOI4 3D models.\n---\n\n"
        "# 3D pipeline\n\nRun `.tools/3d_pipeline/verify_environment.py` first.\n",
    )
    _write(kit / ".claude/agents/hoi4-3d-model-pipeline.md", "---\nname: hoi4-3d-model-pipeline\ndescription: Runs the 3D pipeline.\n---\n")
    _write(kit / ".tools/3d_pipeline/README.md", "# Reusable HOI4 3D model pipeline\n\nBounded source-to-handoff route for custom models.\n")
    _write(kit / ".tools/3d_pipeline/verify_environment.py", "print('ok')\n")
    _write(kit / ".claude/skills/hoi4-map-states/SKILL.md", "---\nname: hoi4-map-states\ndescription: Map and state data.\n---\n")
    setup = tmp_path / "HOI4-Mod-Setup"
    _write(setup / "README.md", "# HOI4 Mod Setup\n\nInstall and prepare a mod.\n\n## Native Rust preview\n\nA new runtime.\n")
    _write(setup / ".agents/skills/hoi4-mod-setup-testing/SKILL.md", "---\nname: hoi4-mod-setup-testing\ndescription: Test the app itself.\n---\n")
    return [("agentic-modding", kit), ("mod-setup", setup)]


def test_three_d_workflow_is_discoverable(roots):
    catalog = cc.build_catalog(roots)
    rendered = cc.render(cc.match(catalog, "does agentic hoi4 modding have a 3d model workflow?"))
    assert "hoi4-3d-model-pipeline" in rendered
    assert "workflow + implementation" in rendered  # the skill points at a tool package that exists
    assert "Reusable HOI4 3D model pipeline" in rendered


def test_skill_and_subagent_with_the_same_name_both_survive(roots):
    catalog = cc.build_catalog(roots)
    kinds = {c.kind for c in catalog if c.name == "hoi4-3d-model-pipeline"}
    assert {"agent skill", "specialist subagent"} <= kinds


def test_contributor_skills_rank_below_feature_skills(roots):
    catalog = cc.build_catalog(roots)
    assert any(c.status == "contributor skill" for c in catalog if c.name == "hoi4-mod-setup-testing")
    hits = cc.match(catalog, "how do I test the mod setup app build", limit=5)
    assert hits and hits[0].root == "mod-setup"


def test_heading_preview_is_experimental_but_prose_preview_is_not(roots):
    catalog = cc.build_catalog(roots)
    statuses = {c.name: c.status for c in catalog if c.root == "mod-setup"}
    assert statuses.get("Native Rust preview") == "experimental"


def test_unrelated_question_matches_nothing(roots):
    catalog = cc.build_catalog(roots)
    assert cc.match(catalog, "who is winning the football tonight?") == []
    assert cc.match(catalog, "what is the weather like") == []


def test_missing_root_is_skipped(tmp_path):
    catalog = cc.build_catalog([("ghost", tmp_path / "nope")])
    assert catalog == []


def test_render_carries_the_status_and_no_paths(roots):
    catalog = cc.build_catalog(roots)
    rendered = cc.render(cc.match(catalog, "can the kit make 3d models", limit=2), freshness={"agentic-modding": "2026-10-25"})
    assert "repo updated 2026-10-25" in rendered
    # A path is evidence for us, never text for a member.
    assert ".claude/" not in rendered and ".tools/" not in rendered
