"""What the bot can actually do, probed at startup.

A false "I can't access X" is the worst failure mode for a support bot: the member stops asking, and the
information exists. The probe turns the environment into a short, truthful capability line that goes into every
model boundary, so the model's self-description matches reality (Hoops 2026-10-18).
"""

from __future__ import annotations

import importlib.util
import shutil


def _has(module: str, binary: str | None = None) -> bool:
    try:
        if importlib.util.find_spec(module) is not None:
            return True
    except (ImportError, ValueError):  # partially installed / broken spec
        pass
    return bool(binary and shutil.which(binary))


def probe_capabilities(*, members_intent: bool = True) -> dict:
    """Return which capabilities exist, plus a prompt-ready summary that must not overstate them."""
    checks = {
        "yt-dlp": _has("yt_dlp", "yt-dlp"),
        "web search": True,
        "live server data": True,
        "knowledge index": True,
        "member directory": bool(members_intent),
    }
    parts = [
        "live server, GitHub and playtest data",
        "the knowledge base (repo, vault, catalog)",
        "the Chaos Redux modding tooling docs (HOI4 Agent Tools MCP, ComfyUI workflows, the agentic modding setup)",
        "the project's official links (YouTube, donation page, GitHub)",
        "web search and reading web pages",
    ]
    if checks["yt-dlp"]:
        parts.append("Downloading video pages, audio and transcripts")
    if checks["member directory"]:
        parts.append("the member directory")
    summary = (
        "You CAN use: "
        + "; ".join(parts)
        + ". Never state that you lack a capability listed here. If one tool fails, say which tool failed in one "
        "line instead of turning it into a claim about your abilities."
    )
    return {
        "available": [name for name, present in checks.items() if present],
        "checks": checks,
        "summary": summary,
    }
