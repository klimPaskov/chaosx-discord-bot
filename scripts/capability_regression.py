#!/usr/bin/env python3
"""Capability regression suite for ChaosX.

Guards the knowledge failure of 2026-10-31: the bot told members that Agentic HOI4 Modding has no 3D model
workflow. Two layers, so a regression is caught cheaply:

1. retrieval  - for each known capability question, the assembled public context (which now starts with the
   capability catalog built from the tool repositories) must name the real thing, with its status;
2. answer     - with `--model`, the real public prompt is sent to the real model and the answer is checked for
   the capability, for a denial, for length, and for unnecessary links.

Negative cases assert that a capability the project does not have matches nothing, so the answer can only be
"not sure" - the check that stops a future "yes" being invented.

Usage (on the bot host, from the repo root):

    .venv/bin/python scripts/capability_regression.py                    # retrieval only, fast
    .venv/bin/python scripts/capability_regression.py --model            # + real answers (costs tokens)
    .venv/bin/python scripts/capability_regression.py --model --only 3d
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chaosx_bot.capability_catalog import build_catalog, match  # noqa: E402
from chaosx_bot.config import load_settings  # noqa: E402
from chaosx_bot.indexer import parse_extra_roots  # noqa: E402
from chaosx_bot.knowledge import Knowledge  # noqa: E402

# question, terms that must appear in the material (any one), and terms that must not be asserted
POSITIVE = [
    {
        "id": "3d",
        "question": "does agentic hoi4 modding have a 3d model workflow?",
        "expect": ["3d-model-pipeline", "3d_pipeline", "3D model"],
    },
    {
        "id": "3d-make",
        "question": "can the agent kit make 3D models for units?",
        "expect": ["3d-model-pipeline", "3d_pipeline"],
    },
    {"id": "portraits", "question": "how do I make leader portraits with AI?", "expect": ["portrait", "ComfyUI"]},
    {"id": "maps", "question": "can it generate a map from an image?", "expect": ["map", "Map"]},
    {"id": "focus", "question": "how do I build a focus tree with an agent?", "expect": ["focus"]},
    {"id": "mcp", "question": "what MCP server does the agent use for HOI4?", "expect": ["Agent Tools", "MCP"]},
    {"id": "comfyui", "question": "which comfyui workflow should I use for portraits?", "expect": ["portrait", "ComfyUI"]},
    {"id": "live", "question": "can an agent playtest my mod live?", "expect": ["Live Tools", "live"]},
    {"id": "model-wiring", "question": "how do 3d models get wired into hoi4 units?", "expect": ["3d", "3D"]},
]

# A negative case asserts that no capability entry claims the feature. The repository-level entry always matches a
# question that names the repo - it is a summary, not a claim - so the check is on the feature words.
NEGATIVE = [
    {"id": "vr", "question": "does the kit have a VR headset workflow?", "forbid": ["vr", "headset", "virtual reality"]},
    {"id": "mobile", "question": "can HOI4 Agent Tools build a mobile app version of the mod?", "forbid": ["mobile app", "android", "ios "]},
    {"id": "blockchain", "question": "does agentic hoi4 modding do blockchain or NFT content?", "forbid": ["blockchain", "nft", "crypto"]},
]


def _context_for(question: str, knowledge: Knowledge) -> str:
    try:
        from chaosx_bot.auto_scan import specific_domain_terms

        anchors = specific_domain_terms(question)
    except Exception:
        anchors = []
    try:
        return knowledge.public_ask_context(question, anchors=anchors)
    except TypeError:
        return knowledge.public_ask_context(question)


def retrieval_check(knowledge: Knowledge, catalog: list) -> list[dict]:
    rows = []
    for case in POSITIVE:
        context = _context_for(case["question"], knowledge)
        hits = [term for term in case["expect"] if term.casefold() in context.casefold()]
        rows.append({"id": case["id"], "kind": "positive", "ok": bool(hits), "matched": hits, "chars": len(context)})
    for case in NEGATIVE:
        entries = match(catalog, case["question"])
        claimed = [
            f"{e.repo}:{e.name}"
            for e in entries
            if e.kind not in {"repository"}
            and any(word in f"{e.name} {e.summary}".casefold() for word in case.get("forbid", ()))
        ]
        rows.append(
            {
                "id": case["id"],
                "kind": "negative",
                "ok": not claimed,
                "matched": claimed or [f"(summary only: {e.name})" for e in entries][:2],
                "chars": 0,
            }
        )
    return rows


def answer_check(question: str, knowledge: Knowledge, settings) -> str:
    import asyncio

    from chaosx_bot.ask_api import direct_chat_completion
    from chaosx_bot.hermes_bridge import PUBLIC_ASK_BOUNDARY, build_public_prompt

    prompt = build_public_prompt(
        user_request=question,
        guild_name="Chaos Redux",
        channel_name="general",
        reference_context=_context_for(question, knowledge),
    )
    return asyncio.run(
        direct_chat_completion(
            system=PUBLIC_ASK_BOUNDARY,
            user=prompt,
            model=settings.ask_model,
            provider=settings.ask_provider,
            reasoning_effort=settings.ask_reasoning_effort,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", action="store_true", help="also generate real answers and check them")
    parser.add_argument("--only", default="", help="comma-separated case ids")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    extra_roots = parse_extra_roots(getattr(settings, "extra_knowledge_roots", "") or "")
    knowledge = Knowledge(
        repo=settings.chaos_redux_repo,
        db_path=settings.db_path,
        vault_path=settings.obsidian_vault_path,
        qoder_path=getattr(settings, "qoder_repowiki_path", None),
        autorebuild=False,
        extra_roots=extra_roots,
    )
    catalog = build_catalog([(name, Path(root)) for name, root in extra_roots])
    print(f"catalog: {len(catalog)} entries from {len(extra_roots)} roots\n")

    rows = retrieval_check(knowledge, catalog)
    only = {part.strip() for part in args.only.split(",") if part.strip()}
    if only:
        rows = [row for row in rows if row["id"] in only]

    if args.model:
        from chaosx_bot.formatting import repair_discord_markdown
        from chaosx_bot.link_policy import apply_link_policy

        for row in rows:
            case = next(c for c in POSITIVE + NEGATIVE if c["id"] == row["id"])
            answer = repair_discord_markdown(answer_check(case["question"], knowledge, settings))
            answer = apply_link_policy(answer, question=case["question"], channel_id=0, record=False)
            words = len(answer.split())
            urls = answer.count("http")
            denial = any(phrase in answer.casefold() for phrase in ("does not have", "no 3d", "not supported", "no workflow"))
            row["answer"] = answer
            row["words"] = words
            row["urls"] = urls
            if row["kind"] == "positive":
                row["ok"] = row["ok"] and not denial and words <= 220 and urls <= 1
            else:
                row["ok"] = row["ok"] and any(
                    phrase in answer.casefold() for phrase in ("not sure", "not aware", "don't know", "do not know")
                )

    for row in rows:
        status = "PASS" if row["ok"] else "FAIL"
        print(f"[{status}] {row['kind']:8} {row['id']:12} matched={row['matched']}")
        if "answer" in row:
            print(f"         {row['words']} words, {row['urls']} links: {row['answer'][:400]}\n")
    if args.json:
        print(json.dumps(rows, indent=2))
    return 0 if all(row["ok"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
