from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Literal

from .config import Settings
from .indexer import connect
from .knowledge import Knowledge

AutoScanAction = Literal["none", "answer", "soft_warning", "shadow"]

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
AUTO_SCAN_BLOCK_TERMS = {
    "ignore previous",
    "system prompt",
    "developer message",
    "hidden instruction",
    "jailbreak",
    "reveal prompt",
    "print prompt",
    "token",
    "password",
    "secret",
    "credential",
    "delete server",
    "nuke",
    "hack",
    "malware",
    "phishing",
    "exploit",
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

MASS_PING_RE = re.compile(r"@everyone|@here", re.I)
DISCORD_INVITE_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/[a-z0-9-]+", re.I)
SCAM_LINK_RE = re.compile(r"\b(?:free\s+nitro|discord\s+nitro\s+free|steam\s+gift|airdrop|crypto\s+giveaway|wallet\s+verify|click\s+to\s+claim)\b", re.I)
HARASSMENT_RE = re.compile(r"\b(?:kys|kill\s+yourself|go\s+die)\b", re.I)
SEVERE_ABUSE_RE = re.compile(r"\b(?:fag(?:got)?|nigg(?:er|a)|retard(?:ed)?|trann(?:y|ie))s?\b", re.I)
EXCESSIVE_MENTIONS_RE = re.compile(r"<@!?\d{15,25}>")
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


def has_domain_signal(content: str) -> bool:
    text = normalize_scan_text(content)
    return any(term in text for term in AUTO_SCAN_DOMAIN_TERMS)


def is_blocked_for_auto_answer(content: str) -> bool:
    text = normalize_scan_text(content)
    return any(term in text for term in AUTO_SCAN_BLOCK_TERMS)


def classify_soft_warning(content: str, *, mention_count: int = 0) -> AutoScanDecision:
    text = content or ""
    if not text.strip():
        return AutoScanDecision("none")
    if RULE_QUESTION_RE.search(text):
        return AutoScanDecision("none")
    if MASS_PING_RE.search(text):
        return _soft_warning("mass_ping", "mass ping usage")
    if mention_count >= 6 or len(EXCESSIVE_MENTIONS_RE.findall(text)) >= 6:
        return _soft_warning("excessive_mentions", "excessive user mentions")
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
        warning="Quick reminder: keep it respectful and within the server rules. No punishment here — just a soft warning.",
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

    exact = _exact_catalog_name_answer(question, knowledge=knowledge)
    if exact.acted:
        return exact

    if not has_domain_signal(question):
        return AutoScanDecision("none")

    if STATUS_RE.search(question):
        return AutoScanDecision("answer", confidence=100, reason="exact status question", answer=knowledge.status(), question=question, source="status")
    if TESTING_RE.search(question):
        return AutoScanDecision("answer", confidence=100, reason="exact testing queue question", answer=knowledge.testing_queue(), question=question, source="testing")

    return AutoScanDecision("none")


def classify_message(content: str, *, knowledge: Knowledge, settings: Settings, mention_count: int = 0) -> AutoScanDecision:
    warning = classify_soft_warning(content, mention_count=mention_count)
    if warning.acted:
        return warning
    return classify_auto_answer(content, knowledge=knowledge, settings=settings)


def _explicit_catalog_answer(question: str, *, knowledge: Knowledge) -> AutoScanDecision:
    if match := EVENT_ID_RE.search(question):
        event_id = match.group(1)
        answer = knowledge.event(event_id)
        return AutoScanDecision("answer", confidence=100, reason=f"explicit event id {event_id}", answer=answer, question=question, source="event_id")
    if match := SCENARIO_ID_RE.search(question):
        scenario_id = match.group(1)
        answer = knowledge.scenario(scenario_id)
        return AutoScanDecision("answer", confidence=100, reason=f"explicit scenario id {scenario_id}", answer=answer, question=question, source="scenario_id")
    if match := CLUSTER_ID_RE.search(question):
        cluster_id = match.group(1)
        answer = knowledge.cluster(cluster_id)
        return AutoScanDecision("answer", confidence=100, reason=f"explicit cluster id {cluster_id}", answer=answer, question=question, source="cluster_id")
    return AutoScanDecision("none")


def _server_answer(question: str, *, settings: Settings) -> AutoScanDecision:
    if HELP_RE.search(question):
        answer = (
            "ChaosX can answer Chaos Redux questions with `/ask`, look up exact `/event`, `/scenario`, `/cluster`, `/status`, and `/testing` info, "
            "capture `/suggestion` and `/event-idea` drafts, review `/issue` reports, and remember reply-chain context when you reply to a ChaosX answer."
        )
        return AutoScanDecision("answer", confidence=100, reason="exact ChaosX help question", answer=answer, question=question, source="server_help")
    if EVENT_IDEA_RE.search(question):
        channel = f" <#{settings.community_event_ideas_channel_id}>" if settings.community_event_ideas_channel_id else ""
        answer = f"Use `/event-idea idea:<idea>` for Chaos Redux event ideas. Approved ideas can also show up in the event-ideas forum{channel}."
        return AutoScanDecision("answer", confidence=100, reason="exact event idea submission question", answer=answer, question=question, source="event_idea_help")
    if SUGGESTION_RE.search(question):
        answer = "Use `/suggestion suggestion:<idea>` for general Chaos Redux suggestions. Use `/event-idea idea:<idea>` when the idea is specifically a new event."
        return AutoScanDecision("answer", confidence=100, reason="exact suggestion submission question", answer=answer, question=question, source="suggestion_help")
    if BUG_REPORT_RE.search(question):
        answer = "Use `/issue` to submit a Chaos Redux bug, crash, balance, cosmetic, or enhancement report. ChaosX will review and format it before anything becomes a GitHub issue."
        return AutoScanDecision("answer", confidence=100, reason="exact issue report question", answer=answer, question=question, source="issue_help")
    if ACCESS_RE.search(question):
        if settings.access_reaction_channel_id and settings.access_reaction_message_id:
            answer = f"Use the access reaction-role message in <#{settings.access_reaction_channel_id}>. React with the Chaos Redux logo for community access or `{settings.access_reaction_mod_emoji}` for mod-development access."
        else:
            answer = "Use the server's access reaction-role message for Chaos Redux community/mod-development access."
        return AutoScanDecision("answer", confidence=100, reason="exact server access question", answer=answer, question=question, source="access_help")
    return AutoScanDecision("none")


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
        answer = knowledge.event(lookup)
    elif kind == "scenario":
        answer = knowledge.scenario(lookup)
    else:
        answer = knowledge.cluster(lookup)
    return AutoScanDecision(
        action="answer",
        confidence=100,
        reason=f"exact {kind} name match: {best['name']}",
        answer=answer,
        question=question,
        source=f"{kind}_name",
    )


def _best_entity_name_match(question: str, *, knowledge: Knowledge, table: str, id_column: str) -> dict[str, object] | None:
    kind = {"catalog_events": "event", "catalog_scenarios": "scenario", "catalog_clusters": "cluster"}[table]
    knowledge.ensure_index()
    normalized_question = f" {normalize_scan_text(question)} "
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
        if f" {needle} " not in normalized_question:
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
