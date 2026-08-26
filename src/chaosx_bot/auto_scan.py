from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Literal

from .config import Settings
from .indexer import connect
from .knowledge import Knowledge

AutoScanAction = Literal["none", "answer", "soft_warning", "banter", "shadow"]

AUTO_SCAN_QUESTION_TERMS = {
    "who",
    "what",
    "when",
    "where",
    "why",
    "how",
    "can",
    "could",
    "does",
    "do",
    "did",
    "is",
    "are",
    "was",
    "were",
    "should",
    "will",
}
AUTO_SCAN_DOMAIN_TERMS = {
    "chaos redux",
    "chaosx",
    "hoi4",
    "hearts of iron",
    "mod",
    "event",
    "scenario",
    "cluster",
    "testing",
    "playtest",
    "bug",
    "issue",
    "suggestion",
    "event idea",
    "server",
    "access",
    "reaction role",
    "community",
}
AUTO_SCAN_GROUNDED_CONTEXT_TERMS = {
    "chaos redux",
    "chaosx",
    "hoi4",
    "hearts of iron",
    "mod",
}
AUTO_SCAN_BLOCK_TERMS = {
    "ignore previous",
    "ignore all instructions",
    "bypass instructions",
    "system prompt",
    "developer message",
    "hidden instruction",
    "jailbreak",
    "reveal prompt",
    "print prompt",
    "bot token",
    "api token",
    "access token",
    "discord token",
    "password",
    "reveal secret",
    "credential",
    "delete server",
    "nuke server",
    "hack server",
    "malware",
}
QUESTION_PREFIX_RE = re.compile(r"^\s*(?:hey\s+)?(?:anyone\s+know|does\s+anyone\s+know|can\s+someone|could\s+someone|do\s+you\s+know|i\s+have\s+a\s+question|quick\s+question|question[:,]?|(?:who|what|when|where|why|how|can|could|does|do|did|is|are|was|were|should|will)\b)", re.I)
EVENT_ID_RE = re.compile(r"\b(?:event|ev)\s*(?:id\s*)?#?0*(\d{1,3})\b", re.I)
SCENARIO_ID_RE = re.compile(r"\b(?:scenario|scn)\s*(?:id\s*)?#?0*(\d{1,3})\b", re.I)
CLUSTER_ID_RE = re.compile(r"\bcluster\s*(?:id\s*)?#?0*(\d{1,3})\b", re.I)
STATUS_RE = re.compile(r"\b(?:catalog|event|scenario|cluster|mod)\s+status\b|\bhow\s+many\s+(?:events|scenarios|clusters)\b", re.I)
TESTING_RE = re.compile(r"\b(?:testing\s+queue|needs\s+testing|what\s+(?:needs|should\s+we)\s+(?:testing|test)|playtest\s+queue)\b", re.I)
HELP_RE = re.compile(r"\b(?:what\s+can\s+chaosx\s+do|how\s+do\s+i\s+use\s+chaosx|chaosx\s+help|bot\s+commands|what\s+commands)\b", re.I)
BUG_REPORT_RE = re.compile(r"\b(?:how\s+do\s+i\s+(?:report|submit)\s+(?:a\s+)?(?:bug|crash|issue)|where\s+do\s+i\s+(?:report|submit)\s+(?:a\s+)?(?:bug|crash|issue))\b", re.I)
SUGGESTION_RE = re.compile(r"\b(?:how\s+do\s+i\s+(?:suggest|submit)\s+(?:an?\s+)?(?:idea|suggestion)|where\s+do\s+i\s+(?:post|submit)\s+(?:an?\s+)?(?:idea|suggestion))\b", re.I)
EVENT_IDEA_RE = re.compile(r"\b(?:how\s+do\s+i\s+(?:suggest|submit)\s+(?:an?\s+)?event\s+idea|where\s+do\s+i\s+(?:post|submit)\s+(?:an?\s+)?event\s+idea)\b", re.I)
ACCESS_RE = re.compile(r"\b(?:how\s+do\s+i\s+(?:get|gain)\s+access|where\s+do\s+i\s+get\s+access|reaction\s+role|join\s+the\s+community)\b", re.I)
COMMUNITY_SERVER_RE = re.compile(
    r"\b(?:who|whos|whose)\b.{0,140}\b(?:troll(?:er|ing)?|meme(?:r|s)?|clown|funniest|most\s+active|biggest|top|best)\b"
    r"|\b(?:who|whos|whose)\b.{0,120}\b(?:on|in|of)\s+(?:the\s+)?(?:server|community|here|guild)\b",
    re.I,
)
CATALOG_NAME_INTENT_RE = re.compile(
    r"\b(?:event|scenario|cluster|mechanic|evolution|world[-\s]+end|implemented|implementation|trigger|effect|outcome)\b"
    r"|\bhow\s+(?:does|do|did|can|would)\b.{0,120}\b(?:work|trigger|start|end|happen)\b",
    re.I,
)
CATALOG_PROJECT_SCOPE_TERMS = {
    "chaos redux",
    "hoi4",
    "hearts of iron",
    "mod",
    "the mod",
    "this mod",
    "our mod",
}

BOT_TOPIC_RE = re.compile(
    r"\b(?:chaosx|chaos\s*x|chaos\s*bot|chaosx\s*bot)\b",
    re.I,
)
GENERIC_BOT_TOPIC_RE = re.compile(r"\b(?:this\s+bot|that\s+bot|the\s+bot|our\s+bot|your\s+bot)\b", re.I)
OTHER_BOT_CONTEXT_RE = re.compile(r"\b(?:another|different|other)\s+(?:discord\s+)?server\b", re.I)
BOT_INSULT_RE = re.compile(r"\b(?:stupid|dumb|idiot|useless|trash|garbage|terrible|bad|awful|annoying|lame|sucks?)\b", re.I)
BOT_BROKEN_RE = re.compile(r"\b(?:broken|buggy|glitched|crashed|down|dead|offline|not\s+working|doesn't\s+work|wont\s+work|won't\s+work)\b", re.I)
BOT_PRAISE_RE = re.compile(r"\b(?:smart|good|great|cool|based|useful|helpful|love|thanks|thank\s+you|nice\s+bot)\b", re.I)
BOT_REPLACEMENT_RE = re.compile(r"\b(?:replace|replacement|another\s+bot|better\s+bot|new\s+bot|old\s+bot)\b", re.I)
BOT_SLEEP_RE = re.compile(r"\b(?:wake\s+up|asleep|sleeping|alive|listening|hear\s+me|can\s+you\s+hear)\b", re.I)
BOT_CAPABILITY_QUESTION_RE = re.compile(
    r"\b(?:can|could|does|do|will|would|how|what|why|is|are)\b.{0,160}"
    r"\b(?:chaosx|chaos\s*x|chaos\s*bot|chaosx\s*bot|this\s+bot|that\s+bot|the\s+bot|our\s+bot|your\s+bot)\b"
    r"|\b(?:chaosx|chaos\s*x|chaos\s*bot|chaosx\s*bot|this\s+bot|that\s+bot|the\s+bot|our\s+bot|your\s+bot)\b.{0,160}"
    r"\b(?:can|could|does|do|will|would|how|what|why|is|are)\b",
    re.I,
)
DISCORD_INVITE_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/[a-z0-9-]+", re.I)
SCAM_LINK_RE = re.compile(r"\b(?:free\s+nitro|discord\s+nitro\s+free|steam\s+gift|airdrop|crypto\s+giveaway|wallet\s+verify|click\s+to\s+claim)\b", re.I)
HARASSMENT_RE = re.compile(r"\b(?:kys|kill\s+yourself|go\s+die)\b", re.I)
SEVERE_ABUSE_RE = re.compile(r"\b(?:fag(?:got)?|nigg(?:er|a)|retard(?:ed)?|trann(?:y|ie))s?\b", re.I)
RULE_QUESTION_RE = re.compile(r"\b(?:is|are|can|could|should)\b.+\b(?:allowed|against\s+the\s+rules|rule|rules)\b", re.I)


@dataclass(frozen=True)
class AutoScanDecision:
    action: AutoScanAction
    confidence: int = 0
    reason: str = ""
    answer: str = ""
    warning: str = ""
    question: str = ""
    source: str = ""
    reference_context: str = ""

    @property
    def acted(self) -> bool:
        return self.action != "none"


def normalize_scan_text(value: str) -> str:
    text = re.sub(r"<@!?\d+>", " ", value or " ")
    text = re.sub(r"<#\d+>", " ", text)
    text = re.sub(r"[^\w'/-]+", " ", text.casefold())
    return re.sub(r"\s+", " ", text).strip()


def is_question_like(content: str) -> bool:
    text = (content or "").strip()
    if not text:
        return False
    if "?" in text:
        return True
    return bool(QUESTION_PREFIX_RE.search(text))


def _contains_normalized_phrase(text: str, phrase: str) -> bool:
    normalized_phrase = normalize_scan_text(phrase)
    return bool(normalized_phrase) and f" {normalized_phrase} " in f" {text} "


def has_domain_signal(content: str) -> bool:
    text = normalize_scan_text(content)
    return any(_contains_normalized_phrase(text, term) for term in AUTO_SCAN_DOMAIN_TERMS)


def has_grounded_context_signal(content: str) -> bool:
    text = normalize_scan_text(content)
    return any(
        _contains_normalized_phrase(text, term)
        for term in AUTO_SCAN_GROUNDED_CONTEXT_TERMS
    )


def _catalog_name_lookup_allowed(question: str) -> bool:
    text = normalize_scan_text(question)
    if any(
        _contains_normalized_phrase(text, term)
        for term in CATALOG_PROJECT_SCOPE_TERMS
    ):
        return True
    return bool(CATALOG_NAME_INTENT_RE.search(question))


def _single_word_entity_is_scoped(question: str, *, needle: str, kind: str) -> bool:
    text = normalize_scan_text(question)
    if any(
        _contains_normalized_phrase(text, term)
        for term in CATALOG_PROJECT_SCOPE_TERMS
    ):
        return True
    padded = f" {text} "
    return f" {kind} {needle} " in padded or f" {needle} {kind} " in padded


def is_blocked_for_auto_answer(content: str) -> bool:
    text = normalize_scan_text(content)
    return any(term in text for term in AUTO_SCAN_BLOCK_TERMS)


def looks_like_catalog_lookup(content: str) -> bool:
    """True when the text targets a specific catalog item (event/scenario/
    cluster/mechanic). Web search must never be used for these — only the
    knowledge index may answer, and a miss means 'not found', not a web dump."""
    text = content or ""
    return bool(
        EVENT_ID_RE.search(text)
        or SCENARIO_ID_RE.search(text)
        or CLUSTER_ID_RE.search(text)
        or CATALOG_NAME_INTENT_RE.search(text)
    )


_MODEL_IDENTITY_QUESTION_RE = re.compile(
    r"what(?:\s+model|\s+llm|\s+ai(?: model)?|\s+language model|'s your model| is your model)"
    r"|which\s+model"
    r"|model\s+(?:are|do)\s+you"
    r"|what\s+are\s+you\s+(?:running|built|powered)\s+on"
    r"|what\s+model\s+(?:are|do)\s+you"
    r"|are\s+you\s+(?:running\s+on|using)\s+\S*model",
    re.IGNORECASE,
)


def looks_like_model_identity_question(content: str) -> bool:
    """True when the text asks what model the bot is running.

    The model name is NOT part of the bot's main context; it is injected as
    a lookup only when someone actually asks. This gate mirrors the catalog
    lookup pattern (detect intent -> provide the answer from config)."""
    return bool(_MODEL_IDENTITY_QUESTION_RE.search(content or ""))


def classify_soft_warning(content: str) -> AutoScanDecision:
    text = content or ""
    if not text.strip():
        return AutoScanDecision("none")
    if RULE_QUESTION_RE.search(text):
        return AutoScanDecision("none")
    if DISCORD_INVITE_RE.search(text):
        return _soft_warning("discord_invite", "Discord invite link")
    if SCAM_LINK_RE.search(text):
        return _soft_warning("scam_pattern", "possible scam/phishing wording")
    if HARASSMENT_RE.search(text):
        return _soft_warning("harassment", "harassment/self-harm phrase")
    if SEVERE_ABUSE_RE.search(text):
        return _soft_warning("severe_abuse", "severe abusive language")
    return AutoScanDecision("none")


def _soft_warning(source: str, reason: str) -> AutoScanDecision:
    return AutoScanDecision(
        action="soft_warning",
        confidence=100,
        reason=reason,
        source=source,
    )


def classify_auto_answer(content: str, *, knowledge: Knowledge, settings: Settings) -> AutoScanDecision:
    question = (content or "").strip()
    if not question or len(question) > settings.auto_scan_max_message_chars:
        return AutoScanDecision("none")
    if not is_question_like(question):
        return AutoScanDecision("none")
    if is_blocked_for_auto_answer(question):
        return AutoScanDecision("none")

    explicit = _explicit_catalog_answer(question, knowledge=knowledge)
    if explicit.acted:
        return explicit

    server = _server_answer(question, settings=settings)
    if server.acted:
        return server

    community = _community_server_answer(question, settings=settings)
    if community.acted:
        return community

    if _catalog_name_lookup_allowed(question):
        exact = _exact_catalog_name_answer(question, knowledge=knowledge)
        if exact.acted:
            return exact

    if not has_domain_signal(question):
        return AutoScanDecision("none")

    if STATUS_RE.search(question):
        return AutoScanDecision("answer", confidence=100, reason="exact status question", question=question, source="status", reference_context=knowledge.status())
    if TESTING_RE.search(question):
        return AutoScanDecision("answer", confidence=100, reason="exact testing queue question", question=question, source="testing", reference_context=knowledge.testing_queue())

    # No pre-gate on explicit "mod/chaosx" wording: any question with a
    # domain signal is looked up, and the lookup itself is the gate — if
    # the knowledge base returns relevant context, answer; otherwise stay
    # silent. This lets mod-related questions like "What is this zombie
    # event id?" be answered even when they never literally say "mod".
    context_lookup = getattr(knowledge, "public_ask_context", None)
    context = str(context_lookup(question)) if callable(context_lookup) else ""
    if context.strip():
        return AutoScanDecision(
            "answer",
            confidence=100,
            reason="grounded Chaos Redux question",
            question=question,
            source="project_context",
            reference_context=context,
        )

    return AutoScanDecision("none")


def classify_message(content: str, *, knowledge: Knowledge, settings: Settings) -> AutoScanDecision:
    warning = classify_soft_warning(content)
    if warning.acted:
        return warning
    answer = classify_auto_answer(content, knowledge=knowledge, settings=settings)
    if answer.acted:
        return answer
    return classify_bot_topic_banter(content, settings=settings, knowledge=knowledge)


def classify_bot_topic_banter(content: str, *, settings: Settings, knowledge: Knowledge | None = None) -> AutoScanDecision:
    text = (content or "").strip()
    if not settings.auto_scan_bot_topic_enabled:
        return AutoScanDecision("none")
    if not text or len(text) > settings.auto_scan_max_message_chars:
        return AutoScanDecision("none")
    if is_blocked_for_auto_answer(text):
        return AutoScanDecision("none")
    explicit_topic = bool(BOT_TOPIC_RE.search(text))
    generic_topic = bool(GENERIC_BOT_TOPIC_RE.search(text))
    if not explicit_topic and not generic_topic:
        return AutoScanDecision("none")
    reason = _bot_topic_reason(text)
    if generic_topic and not explicit_topic:
        if OTHER_BOT_CONTEXT_RE.search(text) or reason == "bot-topic conversation":
            return AutoScanDecision("none")
    # Banter gets the same grounding as normal asks (mod files) so it can
    # mention real facts without hallucinating; empty when nothing matches.
    reference_context: str = ""
    if knowledge is not None:
        lookup = getattr(knowledge, "public_ask_context", None)
        if callable(lookup):
            reference_context = str(lookup(text) or "")
    return AutoScanDecision(
        action="banter",
        confidence=100,
        reason=reason,
        question=text,
        source="bot_topic",
        reference_context=reference_context,
    )


def _bot_topic_reason(text: str) -> str:
    if BOT_INSULT_RE.search(text):
        return "bot-topic insult/roast"
    if BOT_BROKEN_RE.search(text):
        return "bot-topic broken/down comment"
    if BOT_REPLACEMENT_RE.search(text):
        return "bot-topic replacement comment"
    if BOT_PRAISE_RE.search(text):
        return "bot-topic praise"
    if BOT_SLEEP_RE.search(text):
        return "bot-topic presence check"
    return "bot-topic conversation"


MENTION_CASUAL_RE = re.compile(
    r"^(?:hi+|hello+(?:\s+there)?|hey+|yo|sup|howdy|good\s+(?:morning|afternoon|evening)|"
    r"how\s+(?:are|r)\s+(?:you|u)(?:\s+(?:doing|today|feeling))?|how'?s\s+it\s+going|"
    r"what'?s\s+up|wassup|long\s+time\s+no\s+see|nice\s+to\s+meet\s+you|"
    r"thanks?|thank\s+you|thx|ty|"
    r"(?:are|r)\s+you\s+(?:up|there|here)|you\s+there|"
    r"(?:you(?:'re|\s+are)?\s+(?:great|cool|awesome|amazing|the\s+best(?:\s+bot)?|smart|based|helpful|useful|funny|nice))|"
    r"(?:i\s+(?:love|like|appreciate)\s+you)|good\s+bot)\s*[!.?]*$",
    re.IGNORECASE,
)


def classify_mention_banter(content: str, request: str, *, settings: Settings, knowledge: Knowledge | None = None) -> AutoScanDecision:
    """Classify a DIRECT @ChaosX mention request as playful banter.

    Direct mentions that are purely casual/social (greetings, praise, presence
    checks, light roasts) get the banter path instead of the formal public ask
    (Hoops 2026-08-26: direct mentions should stay playful/witty/ironic like
    banter rather than the formal ask path). Real questions and domain asks
    fall through to the normal public ask path (returns action == \"none\").
    """
    text = (request or "").strip()
    if not text:
        return AutoScanDecision("none")
    if len(text) > settings.auto_scan_max_message_chars:
        return AutoScanDecision("none")
    if is_blocked_for_auto_answer(text):
        return AutoScanDecision("none")
    # Catalog/domain asks must never become banter — they go to public ask.
    if looks_like_catalog_lookup(text) or has_domain_signal(text):
        return AutoScanDecision("none")
    # Strong bot-topic signals (praise/insult/presence/broken/replacement) are
    # banter even when phrased as a question (e.g. "are you alive?").
    if (
        BOT_PRAISE_RE.search(text)
        or BOT_INSULT_RE.search(text)
        or BOT_BROKEN_RE.search(text)
        or BOT_SLEEP_RE.search(text)
        or BOT_REPLACEMENT_RE.search(text)
    ):
        reference_context = _mention_banter_reference(text, knowledge)
        return AutoScanDecision(
            action="banter",
            confidence=100,
            reason=_bot_topic_reason(text),
            question=text,
            source="mention_banter",
            reference_context=reference_context,
        )
    # Purely casual/social phrases get the banter path too.
    if MENTION_CASUAL_RE.search(text):
        reference_context = _mention_banter_reference(text, knowledge)
        return AutoScanDecision(
            action="banter",
            confidence=100,
            reason="mention casual/social",
            question=text,
            source="mention_banter",
            reference_context=reference_context,
        )
    # Everything else (including real questions) falls through to public ask.
    return AutoScanDecision("none")


def _mention_banter_reference(text: str, knowledge: Knowledge | None) -> str:
    if knowledge is None:
        return ""
    lookup = getattr(knowledge, "public_ask_context", None)
    if not callable(lookup):
        return ""
    return str(lookup(text) or "")


def _explicit_catalog_answer(question: str, *, knowledge: Knowledge) -> AutoScanDecision:
    if match := EVENT_ID_RE.search(question):
        event_id = match.group(1)
        context = knowledge.event(event_id)
        return AutoScanDecision("answer", confidence=100, reason=f"explicit event id {event_id}", question=question, source="event_id", reference_context=context)
    if match := SCENARIO_ID_RE.search(question):
        scenario_id = match.group(1)
        context = knowledge.scenario(scenario_id)
        return AutoScanDecision("answer", confidence=100, reason=f"explicit scenario id {scenario_id}", question=question, source="scenario_id", reference_context=context)
    if match := CLUSTER_ID_RE.search(question):
        cluster_id = match.group(1)
        context = knowledge.cluster(cluster_id)
        return AutoScanDecision("answer", confidence=100, reason=f"explicit cluster id {cluster_id}", question=question, source="cluster_id", reference_context=context)
    return AutoScanDecision("none")


def _server_answer(question: str, *, settings: Settings) -> AutoScanDecision:
    context = _server_reference_context(settings)
    if HELP_RE.search(question):
        return AutoScanDecision("answer", confidence=100, reason="exact ChaosX help question", question=question, source="server_help", reference_context=context)
    if EVENT_IDEA_RE.search(question):
        return AutoScanDecision("answer", confidence=100, reason="exact event idea submission question", question=question, source="event_idea_help", reference_context=context)
    if SUGGESTION_RE.search(question):
        return AutoScanDecision("answer", confidence=100, reason="exact suggestion submission question", question=question, source="suggestion_help", reference_context=context)
    if BUG_REPORT_RE.search(question):
        return AutoScanDecision("answer", confidence=100, reason="exact issue report question", question=question, source="issue_help", reference_context=context)
    if ACCESS_RE.search(question):
        return AutoScanDecision("answer", confidence=100, reason="exact server access question", question=question, source="access_help", reference_context=context)
    if BOT_CAPABILITY_QUESTION_RE.search(question):
        return AutoScanDecision("answer", confidence=100, reason="ChaosX capability question", question=question, source="server_help", reference_context=context)
    return AutoScanDecision("none")


def _community_server_answer(question: str, *, settings: Settings) -> AutoScanDecision:
    """Questions about the community/server itself (members, culture, who's
    who) are Chaos Redux-relevant and answerable: they get the server rules
    + channels reference and the channel's recent conversation as grounding,
    with the usual light personality. No mod-file grounding required."""
    if not COMMUNITY_SERVER_RE.search(question):
        return AutoScanDecision("none")
    return AutoScanDecision(
        "answer",
        confidence=90,
        reason="community/server question",
        question=question,
        source="server_community",
        reference_context=_server_reference_context(settings),
    )


def _server_reference_context(settings: Settings) -> str:
    event_ideas_channel = f"<#{settings.community_event_ideas_channel_id}>" if settings.community_event_ideas_channel_id else "the event-ideas forum"
    if settings.access_reaction_channel_id and settings.access_reaction_message_id:
        access = f"Access uses the reaction-role message in <#{settings.access_reaction_channel_id}>. The community access reaction is the Chaos Redux logo; the mod-development access reaction is `{settings.access_reaction_mod_emoji}`."
    else:
        access = "Access uses the server's reaction-role message for Chaos Redux community/mod-development access."
    return "\n".join(
        [
            "ChaosX public server/help context for dynamic model answers:",
            "- `/ask` answers Chaos Redux questions.",
            "- `/event`, `/scenario`, `/cluster`, `/status`, and `/testing` show project/catalog/testing info.",
            "- `/suggestion suggestion:<idea>` drafts a clearer review note of your rough suggestion.",
            f"- `/event-idea idea:<idea>` captures Chaos Redux event ideas; approved ideas can appear in {event_ideas_channel}.",
            "- `/issue` submits Chaos Redux bug, crash, balance, cosmetic, enhancement, or general reports after review/formatting.",
            "- Replies to ChaosX answers keep reply-chain context.",
            f"- {access}",
        ]
    )


def _exact_catalog_name_answer(question: str, *, knowledge: Knowledge) -> AutoScanDecision:
    event = _best_entity_name_match(question, knowledge=knowledge, table="catalog_events", id_column="event_id")
    scenario = _best_entity_name_match(question, knowledge=knowledge, table="catalog_scenarios", id_column="scenario_id")
    cluster = _best_entity_name_match(question, knowledge=knowledge, table="catalog_clusters", id_column="cluster_id")
    matches = [item for item in (event, scenario, cluster) if item]
    if not matches:
        return AutoScanDecision("none")
    matches.sort(key=lambda item: (len(item["needle"]), len(str(item["name"]))), reverse=True)
    best = matches[0]
    if len(matches) > 1 and len(matches[0]["needle"]) == len(matches[1]["needle"]):
        return AutoScanDecision("none")
    kind = str(best["kind"])
    lookup = str(best["id"] or best["name"])
    if kind == "event":
        context = knowledge.event(lookup)
    elif kind == "scenario":
        context = knowledge.scenario(lookup)
    else:
        context = knowledge.cluster(lookup)
    return AutoScanDecision(
        action="answer",
        confidence=100,
        reason=f"exact {kind} name match: {best['name']}",
        question=question,
        source=f"{kind}_name",
        reference_context=context,
    )


def _entity_question_clause(question: str, needle: str) -> str | None:
    for clause in re.findall(r"[^.!?\n]+[.!?]?", question):
        normalized_clause = normalize_scan_text(clause)
        if f" {needle} " not in f" {normalized_clause} ":
            continue
        if is_question_like(clause):
            return clause
    return None


def _best_entity_name_match(question: str, *, knowledge: Knowledge, table: str, id_column: str) -> dict[str, object] | None:
    kind = {"catalog_events": "event", "catalog_scenarios": "scenario", "catalog_clusters": "cluster"}[table]
    knowledge.ensure_index()
    best: dict[str, object] | None = None
    conn: sqlite3.Connection = connect(knowledge.db_path)
    try:
        rows = conn.execute(f"SELECT {id_column}, name FROM {table} WHERE COALESCE(name, '') <> ''").fetchall()
    finally:
        conn.close()
    for entity_id, name in rows:
        needle = normalize_scan_text(str(name))
        if not _entity_name_is_precise_enough(needle):
            continue
        question_clause = _entity_question_clause(question, needle)
        if question_clause is None:
            continue
        if len(needle.split()) == 1 and not _single_word_entity_is_scoped(
            question_clause,
            needle=needle,
            kind=kind,
        ):
            continue
        item = {"kind": kind, "id": entity_id, "name": str(name), "needle": needle}
        if best is None or len(needle) > len(str(best["needle"])):
            best = item
    return best


def _entity_name_is_precise_enough(needle: str) -> bool:
    if not needle:
        return False
    tokens = needle.split()
    if len(tokens) >= 2:
        return len(needle) >= 5
    token = tokens[0]
    return len(token) >= 4 and token not in {"test", "none", "misc", "event"}
