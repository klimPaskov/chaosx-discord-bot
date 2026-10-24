"""Audit the uninvited-answer gate against a generated corpus of thousands of messages.

Hoops, 2026-10-24: "What, chaos meter wasn't caught? Please fix this. Run thousands more tests."
The classifier is a precision/recall problem, so it is measured like one: generated positives (real
project questions, phrasing variants, plurals, typos) and negatives (ordinary chat, other games, other
subjects) run through the real classifier with the real index, then scored.

usage:
    .venv/bin/python scripts/audit_auto_scan_corpus.py [--per-topic 8] [--negatives 2000] [--show 25]
exit code 0 when recall >= --min-recall and false-positive rate <= --max-fpr.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chaosx_bot.auto_scan import classify_message  # noqa: E402
from chaosx_bot.config import load_settings  # noqa: E402
from chaosx_bot.indexer import parse_extra_roots  # noqa: E402
from chaosx_bot.knowledge import Knowledge  # noqa: E402

# (topic anchor words used in the sentence, question templates)
TOPICS: list[tuple[str, list[str]]] = [
    ("chaos meter", ["what modes does the {t} have?", "how does the {t} fill up?", "{t} tiers explained?", "can the {t} go down again?", "what does the {t} actually do?"]),
    ("chaos redux", ["what is {t} about?", "when did {t} release?", "is {t} compatible with vanilla?", "who makes {t}?", "where do I download {t}?"]),
    ("event 42", ["what is the trigger for {t}?", "why does {t} not fire?", "who wrote {t}?", "how long is {t}?", "is {t} in the current build?"]),
    ("scenario", ["how many {t}s are implemented?", "which {t} is the hardest?", "how do I start a {t}?", "what decides the {t} ending?", "can a {t} be replayed?"]),
    ("cluster", ["what is a {t}?", "how do {t}s interact?", "which {t} is newest?", "do {t}s stack?", "how do I unlock the next {t}?"]),
    ("evolution stage", ["how many {t}s are there?", "what unlocks the next {t}?", "can I skip an {t}?", "what happens at the last {t}?", "do {t}s reset on reload?"]),
    ("world-end scenario", ["how does the {t} work?", "what triggers the {t}?", "can you survive the {t}?", "is the {t} optional?", "how long does the {t} take?"]),
    ("focus tree", ["how big is the {t}?", "where is the {t} file?", "can the AI finish the {t}?", "why is my {t} missing focuses?", "how do I edit the {t}?"]),
    ("national focus", ["how do I add a {t}?", "why is the {t} greyed out?", "what unlocks this {t}?", "do {t}s have prerequisites?", "how do I localise a {t}?"]),
    ("decision", ["how do I add a {t}?", "why is the {t} not showing?", "what does this {t} cost?", "how do {t}s refresh?", "can {t}s be hidden?"]),
    ("scripted gui", ["how do I build a {t}?", "why is my {t} blank?", "how do {t} elements update?", "what is the {t} syntax?", "can a {t} show live values?"]),
    ("localisation", ["how do I add {t}?", "why is the {t} showing a key?", "which {t} file handles tooltips?", "does {t} support colour codes?", "how do I fix broken {t}?"]),
    ("portrait", ["how do I make a {t}?", "what size should a leader {t} be?", "how do I convert a {t} to dds?", "which workflow makes {t}s?", "how do I install the {t} lora?"]),
    ("icon", ["how do I make a mod {t}?", "what resolution are {t}s?", "why is my {t} blurry?", "can I batch generate {t}s?", "where do {t} files go?"]),
    ("3d model", ["how do I add a {t} to the mod?", "which tool converts a {t}?", "what format does a {t} need?", "can I reuse a vanilla {t}?", "how do I preview a {t}?"]),
    ("map", ["how do I edit the {t}?", "why are provinces missing on the {t}?", "how do I add a new {t} region?", "what breaks if I change the {t}?", "how do I test the {t}?"]),
    ("state", ["how do I add a {t}?", "why is the {t} history not applied?", "how do I move a {t} to another country?", "what is a {t} id?", "can I split a {t}?"]),
    ("division template", ["how do I edit a {t}?", "why is my {t} so weak?", "what is the best {t}?", "how do {t}s scale with tech?", "can I script a {t}?"]),
    ("save game", ["how do I load a {t} with the agent?", "why does my {t} not load?", "where is the {t} stored?", "can the agent read a text {t}?", "how do I convert an ironman {t}?"]),
    ("error log", ["where is the {t}?", "what does this {t} line mean?", "how do I clear the {t}?", "which {t} errors matter?", "can the agent read the {t}?"]),
    ("crash", ["why does the mod {t} on startup?", "how do I debug a {t}?", "what causes a {t} after loading a save?", "is there a known {t}?", "what log do I check after a {t}?"]),
    ("game rule", ["how do I add a {t}?", "what does this {t} change?", "can {t}s be set mid-game?", "do {t}s affect the AI?", "how do I localise a {t}?"]),
    ("compatibility", ["is it {t} with the latest patch?", "what breaks {t}?", "how do I check {t} with other mods?", "does {t} matter for saves?", "which mods cause {t} issues?"]),
    ("load order", ["does {t} matter here?", "what is the right {t}?", "why is my {t} wrong?", "how do I change the {t}?", "can a bad {t} crash the game?"]),
    ("balance", ["how is the {t} tuned?", "who decides {t}?", "why does the {t} feel off?", "can I change {t} myself?", "is the {t} being worked on?"]),
    ("AI", ["how does the {t} behave late game?", "why is the {t} so passive?", "can I script the {t}?", "what makes the {t} declare war?", "is the {t} stronger now?"]),
    ("testing queue", ["what is in the {t}?", "how do I join the {t}?", "how long is the {t}?", "what do testers check in the {t}?", "who runs the {t}?"]),
    ("playtest", ["how do I join a {t}?", "what do I need for a {t}?", "when is the next {t}?", "how do I report a {t} bug?", "do {t}s happen on weekends?"]),
    ("tier", ["how do I reach the next {t}?", "what are {t} rewards?", "do {t}s reset?", "how is the {t} calculated?", "which {t} am I?"]),
    ("ladder title", ["how do I get a {t}?", "why is my {t} missing?", "what are the {t}s?", "can I change my {t}?", "do {t}s update automatically?"]),
    ("ideas", ["how do I post {t}?", "where do {t} go?", "are {t} voted on?", "who reviews {t}?", "can I edit my {t} after posting?"]),
    ("bug report", ["how do I file a {t}?", "where do {t}s go?", "what belongs in a {t}?", "do I need logs for a {t}?", "who triages {t}s?"]),
    ("steam workshop", ["is the mod on the {t}?", "how do I update from the {t}?", "why is the {t} version behind?", "does the {t} include the music?", "can I subscribe on the {t}?"]),
    ("mod setup", ["how do I use the {t} app?", "what does the {t} install?", "why does the {t} fail on launch?", "does the {t} work on linux?", "can the {t} update my mod?"]),
    ("MCP server", ["what are the {t}s used for?", "how do I install the {t}?", "which coding client supports the {t}?", "why is the {t} not connecting?", "do {t}s work on mac?"]),
    ("live tools", ["how do I install {t}?", "what can {t} do in game?", "why does {t} fail the selftest?", "does {t} work with ironman saves?", "which clients support {t}?"]),
    ("agent tools", ["what does {t} render?", "how do I register {t}?", "why are {t} previews blank?", "which node version does {t} need?", "can {t} edit localisation?"]),
    ("comfyui", ["how do I set up {t}?", "which {t} workflow makes portraits?", "why are my {t} nodes red?", "does {t} work on runpod?", "how much vram does {t} need?"]),
    ("lora", ["how do I train a {t}?", "where does the {t} go?", "which {t} does the mod use?", "why is the {t} not loading?", "can I merge two {t}s?"]),
    ("runpod", ["how do I run {t} workflows?", "how much does {t} cost?", "why did my {t} pod stop?", "can {t} use the mod lora?", "which template for {t}?"]),
    ("codex", ["how do I use {t} with the mod?", "which {t} config file?", "why does {t} not see the tools?", "does {t} support mcp?", "can {t} read the error log?"]),
    ("reaction role", ["how do I get a {t}?", "where are the {t}s?", "can I remove a {t}?", "who sets up {t}s?", "are {t}s per channel?"]),
    ("discord invite", ["what is the {t}?", "is there a {t} for testers?", "how do I join the {t}?", "can I share the {t}?", "does the {t} expire?"]),
    ("tutorial", ["is there a {t} for modding?", "which {t} should I watch first?", "how long is the {t}?", "is the {t} up to date?", "can I follow the {t} with the new tools?"]),
    ("ko-fi", ["is there a {t} page?", "how do I support on {t}?", "what do {t} donations fund?", "do {t} supporters get perks?", "can I donate monthly on {t}?"]),
    ("changelog", ["where is the {t}?", "what changed in the latest {t}?", "does the {t} list fixes?", "how often is the {t} updated?", "can I read the {t} in discord?"]),
    ("roadmap", ["is there a {t}?", "what is next on the {t}?", "how do I suggest something for the {t}?", "does the {t} include scenarios?", "when does the {t} update?"]),
]

POSITIVE_PREFIXES = ["", "hey ", "quick question ", "okay, ", "so ", "sorry, ", "yo ", "real quick "]
POSITIVE_SUFFIXES = ["", " thanks", " in the current build", " for a new player", " lol", " please"]
POSITIVE_VARIANTS = [
    lambda s: s,
    lambda s: s.replace("?", ""),
    lambda s: s.replace("does", "do"),
    lambda s: s.replace("is ", "is the "),
]

NEGATIVE_MESSAGES = [
    "hello", "hi everyone", "gm", "good night", "how are you doing", "what's up", "lol", "nice",
    "thanks a lot", "brb", "who is winning the football tonight", "did you see the match yesterday",
    "what's the weather like today", "it's raining here", "I'm hungry", "pizza or burgers",
    "how do I sort a list in python", "why is my javascript promise pending", "npm install fails on windows",
    "my gpu is overheating", "should I buy a 4070 or 4080", "phone battery drains fast",
    "what's a good movie tonight", "have you heard the new album", "my cat is asleep",
    "I'm tired", "what time is it", "any plans for the weekend", "good luck with the exam",
    "how tall is mount everest", "what is 17 times 23", "convert 90 fahrenheit to celsius",
    "who won the election", "is it going to snow tomorrow", "how do I change my discord avatar",
    "why is discord lagging", "my internet is down", "how do I unmute myself",
    "minecraft mods are fun", "which minecraft mod adds shaders", "is factorio better than satisfactory",
    "stellaris ai is dumb", "eu4 mana system is weird", "ck3 is on sale", "victoria 3 economy is confusing",
    "rimworld mods keep breaking", "cities skylines traffic is hell", "terraria hardmode is brutal",
    "skyrim mods on console", "fallout 4 settlement build", "gta 6 when", "fortnite season is boring",
    "valorant rank reset", "league patch notes", "dota meta", "wow classic launch",
    "age of empires 4 is good", "total war warhammer 3 unit balance", "civilization 7 leaders",
    "baldurs gate 3 patch", "cyberpunk dlc was great", "satisfactory update",
    "oxygen not included pipes", "project zomboid survival tips", "mount and blade bannerlord sieges",
    "hearts of iron 3 was better", "darkest hour is old", "kaiserreich lore is wild",
    "the new order mod is huge", "old world blues is funny",
    "what do you think of the other bot", "that bot in the other server is better",
    "this server is quiet", "who is the owner here", "can someone dm me",
    "anyone play chess", "what's your favourite food", "tell me a joke", "sing me a song",
    "how do I make friends", "should I quit my job", "my girlfriend is angry",
    "do you like cats or dogs", "what is the meaning of life", "how old are you",
    # adversarial: generic domain words in clearly non-project contexts
    "what state are you in", "which state has the best beaches", "how do I balance my budget",
    "any ideas for dinner", "my ai assistant keeps interrupting", "is the map accurate on google maps",
    "how do I save my progress in zelda", "the model looks great on the runway",
    "how do I update my phone", "which version of windows is best", "my download is slow",
    "how do I fix my server at work", "what music should I listen to", "who won the election",
    "is the new iphone worth it", "how do I change my email password", "what is the capital of brazil",
    "my country has bad weather", "how do I cook rice", "can you recommend a gym",
    "what is a good book to read", "how long is a marathon", "why is my wifi slow",
    "how do I apply for a job", "what's the best pizza topping", "do I need a visa for japan",
    "is coffee bad for you", "how many hours should I sleep", "what is a good laptop for school",
    "my cat keeps meowing", "how do I fix a leaking tap", "what is the score of the game",
    "who is the president of france", "how do I learn guitar", "what is the tallest building",
]


def build_corpus(per_topic: int, negatives: int, seed: int = 20261024) -> list[tuple[str, bool, str]]:
    rng = random.Random(seed)
    cases: list[tuple[str, bool, str]] = []
    for topic, templates in TOPICS:
        variants = templates[:per_topic]
        while len(variants) < per_topic:  # top up with rephrasings rather than duplicates
            variants.append(rng.choice(templates))
        for template in variants:
            sentence = template.format(t=topic)
            prefix = rng.choice(POSITIVE_PREFIXES)
            suffix = rng.choice(POSITIVE_SUFFIXES)
            text = f"{prefix}{sentence}{suffix}".strip()
            cases.append((text, True, topic))
        for variant in POSITIVE_VARIANTS:
            sentence = variant(templates[0].format(t=topic))
            if sentence == templates[0].format(t=topic):
                continue
            cases.append((sentence, True, topic))
    while len(cases) < negatives or len([c for c in cases if not c[1]]) < negatives:
        base = rng.choice(NEGATIVE_MESSAGES)
        decorations = [
            base,
            f"{rng.choice(['hey', 'so', 'okay,', 'you know,'])} {base}",
            f"{base}?",
            f"{base} lol",
            f"{base} btw",
        ]
        cases.append((rng.choice(decorations), False, "chat"))
        if len([c for c in cases if not c[1]]) >= negatives:
            break
    rng.shuffle(cases)
    return cases


# The stub simulates retrieval: a project question retrieves a document about its topic (which is what
# the live index does for these topics, checked separately with --knowledge real), and ordinary chat
# retrieves only the generic digest. That is the signal the gate's overlap check sees, without a 1.2s
# round-trip per case.
CONTEXT_HINT = {"topic": ""}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-topic", type=int, default=10)
    parser.add_argument("--negatives", type=int, default=2000)
    parser.add_argument("--show", type=int, default=20)
    parser.add_argument(
        "--knowledge",
        choices=("stub", "real"),
        default="stub",
        help="stub = fixed non-empty context (gate precision/recall, fast); real = the live index",
    )
    parser.add_argument("--min-recall", type=float, default=0.90)
    parser.add_argument("--max-fpr", type=float, default=0.05)
    args = parser.parse_args()

    settings = load_settings()
    knowledge = Knowledge(
        settings.chaos_redux_repo,
        settings.db_path,
        settings.obsidian_vault_path,
        settings.qoder_repowiki_path,
        catalog_repo=settings.focus_tree_repo or settings.chaos_redux_repo,
        extra_roots=parse_extra_roots(settings.extra_knowledge_roots),
    )
    # The index is already built for this run; skipping the per-call freshness check (a git subprocess)
    # keeps a few thousand cases to seconds instead of minutes.
    Knowledge.ensure_index = lambda self: None  # type: ignore[method-assign]

    if args.knowledge == "stub":
        # Knowledge is a frozen dataclass, so the fast path patches the class: public_ask_context never
        # returns empty on a healthy index, so a non-empty stub gives the gate the same signal without an
        # index round-trip per case. Everything else (catalog lookups, server answers) stays real.
        def _stub_context(self, query: str, limit: int = 6, include_sources: bool = False) -> str:
            topic = CONTEXT_HINT["topic"]
            if topic:
                return (
                    f"1. {topic}: Chaos Redux reference material. The {topic} is documented in this mod; "
                    "this passage explains how it works and what to check."
                )
            return "1. Chaos Redux has events, scenarios, clusters and testing notes; general mod reference."

        Knowledge.public_ask_context = _stub_context  # type: ignore[method-assign]

    cases = build_corpus(args.per_topic, args.negatives)
    positives = [c for c in cases if c[1]]
    negatives = [c for c in cases if not c[1]]

    missed: list[tuple[str, str]] = []
    caught = 0
    for index, (text, _expected, topic) in enumerate(positives, 1):
        CONTEXT_HINT["topic"] = topic
        if index % 200 == 0:
            print(f"  ... {index}/{len(positives)} positives", flush=True)
        decision = classify_message(text, knowledge=knowledge, settings=settings)
        if decision.acted:
            caught += 1
        else:
            missed.append((topic, text))

    false_positives: list[tuple[str, str]] = []
    quiet = 0
    for text, _expected, _topic in negatives:
        CONTEXT_HINT["topic"] = ""
        decision = classify_message(text, knowledge=knowledge, settings=settings)
        if decision.acted:
            false_positives.append((decision.source or decision.action, text))
        else:
            quiet += 1

    recall = caught / len(positives) if positives else 1.0
    fpr = len(false_positives) / len(negatives) if negatives else 0.0
    print(f"cases: {len(cases)} (positives {len(positives)}, negatives {len(negatives)})")
    print(f"recall: {recall:.3f} ({caught}/{len(positives)} project questions answered uninvited)")
    print(f"false-positive rate: {fpr:.3f} ({len(false_positives)}/{len(negatives)} ordinary messages answered)")
    if missed:
        print(f"\nmissed positives ({len(missed)}), first {args.show}:")
        for topic, text in missed[: args.show]:
            print(f"  [{topic}] {text}")
    if false_positives:
        print(f"\nfalse positives ({len(false_positives)}), first {args.show}:")
        for source, text in false_positives[: args.show]:
            print(f"  [{source}] {text}")
    ok = recall >= args.min_recall and fpr <= args.max_fpr
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
