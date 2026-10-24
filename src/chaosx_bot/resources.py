"""The project's official links, in one place.

The answering model never invents a URL: this module is the only source of links
that can reach a member. `resources_block()` goes into every public answer prompt,
`help_resources_section()` into `/help`, and the curated vault page
`Tooling/Chaos Redux Official Links.md` mirrors the same list for retrieval.

Every entry here is public. Never add a private repo (for example the vault repo).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Resource:
    key: str
    label: str
    url: str
    when: str


OFFICIAL_RESOURCES: tuple[Resource, ...] = (
    Resource(
        key="youtube",
        label="YouTube channel (@ChaosRedux)",
        url="https://www.youtube.com/@ChaosRedux",
        when="devlogs, showcases, and the AI/agentic HOI4 modding tutorials",
    ),
    Resource(
        key="donate",
        label="Donation page (Ko-fi)",
        url="https://ko-fi.com/klimpaskov",
        when="if someone wants to support the mod's development",
    ),
    Resource(
        key="steam",
        label="Steam Workshop page",
        url="https://steamcommunity.com/sharedfiles/filedetails/?id=2983516043",
        when="subscribing to or installing the mod",
    ),
    Resource(
        key="github",
        label="Chaos Redux repository",
        url="https://github.com/klimPaskov/Chaos-Redux",
        when="browsing the mod's source, reporting issues, or contributing",
    ),
    Resource(
        key="tester-video",
        label="How to become a tester (video)",
        url="https://www.youtube.com/watch?v=feD3BDelD_U",
        when="the full setup: repo into the mods folder, descriptor, launcher playset, debug mode, reporting bugs",
    ),
    Resource(
        key="modding-tutorial",
        label="How to use AI for HOI4 modding (full tutorial)",
        url="https://www.youtube.com/watch?v=_ES5Dv3L5rs",
        when="the agentic setup: Codex, AGENTS.md, skills, subagents, Hermes agents, reviewing results",
    ),
    Resource(
        key="portrait-tutorial",
        label="HOI4 portraits with ComfyUI + FLUX LoRA (video)",
        url="https://www.youtube.com/watch?v=t4cwY4u5UUU",
        when="the portrait workflow: batch folders, text-to-image, old photos, PNG/156x210/DDS export",
    ),
    Resource(
        key="hoi4-agent-tools",
        label="HOI4 Agent Tools (MCP server for modding agents)",
        url="https://github.com/klimPaskov/hoi4-agent-tools",
        when="the agent tooling behind focus-tree, event-chain and scripted-GUI previews, plus map/tech/probability inspection",
    ),
    Resource(
        key="hoi4-live-tools",
        label="HOI4 Live Tools (MCP server that plays/tests the mod)",
        url="https://github.com/klimPaskov/HOI4-Live-Tools",
        when="letting a coding agent launch HOI4, drive the game and test a mod build",
    ),
    Resource(
        key="hoi4-mod-setup",
        label="HOI4 Mod Setup (AI-ready mod scaffold and tool installer)",
        url="https://github.com/klimPaskov/HOI4-Mod-Setup",
        when="installing and registering the modding tools (Live Tools, Agent Tools, ComfyUI workflows) for your coding client, or preparing a HOI4 mod for AI-assisted development",
    ),
    Resource(
        key="agentic-modding",
        label="Agentic HOI4 Modding (agent kit)",
        url="https://github.com/klimPaskov/Agentic-HOI4-Modding",
        when="the skills, rules and workflows for using coding agents on HOI4 mods",
    ),
    Resource(
        key="comfyui-portraits",
        label="ComfyUI HOI4 portraits (workflow pack)",
        url="https://github.com/klimPaskov/comfyui-hoi4-portraits",
        when="identity-preserving portrait workflows for ComfyUI",
    ),
    Resource(
        key="discord",
        label="Community Discord",
        url="https://discord.gg/rAXesGcT2t",
        when="joining the community to talk about the mod, testing, or the modding tools",
    ),
    Resource(
        key="playlist",
        label="Video tutorials playlist",
        url="https://www.youtube.com/playlist?list=PLh6JmuEabQioc4V8IYGEsMtqiw-xemeX3",
        when="the step-by-step modding-with-AI video series",
    ),
    Resource(
        key="agentic-guide",
        label="Agentic HOI4 Modding guide",
        url="https://klimpaskov.github.io/Agentic-HOI4-Modding/",
        when="setting up an agent on a mod and looking up the skills, workflows and tools",
    ),
    Resource(
        key="portrait-lora",
        label="HOI4 portrait style LoRA (FLUX.2 Klein 9B)",
        url="https://huggingface.co/Hoops-McCann/hoi4-portraits-flux2-klein-9b-lora",
        when="the LoRA used by the ComfyUI portrait workflows",
    ),
)


def resources_block() -> str:
    """Prompt-ready block of official links. Never empty, never generates a URL."""
    lines = [f"- {r.label} — {r.url} ({r.when})" for r in OFFICIAL_RESOURCES]
    return (
        "\nOfficial Chaos Redux links (authoritative — these are the only URLs you may give a member; "
        "never invent, guess, shorten or reconstruct a link, and offer the relevant one when someone asks "
        "about videos, tutorials, the modding setup, the modding tools, installing the mod, supporting the "
        "project, or contributing):\n" + "\n".join(lines) + "\n"
    )


def help_resources_section() -> str:
    """Markdown section for the public `/help` body."""
    lines = [f"- {r.label} — {r.url}" for r in OFFICIAL_RESOURCES]
    return "### Official links\n" + "\n".join(lines)
