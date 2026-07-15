from __future__ import annotations

import asyncio
import difflib
import json
import re
from dataclasses import dataclass
from typing import Any

WORD_RE = re.compile(r"[a-z0-9]+")
TYPE_PREFIX_RE = re.compile(r"^\s*\[[^\]]{1,30}\]\s*")
STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "bug",
    "chaos",
    "crash",
    "enhancement",
    "error",
    "for",
    "from",
    "game",
    "general",
    "hoi4",
    "in",
    "is",
    "issue",
    "it",
    "mod",
    "not",
    "of",
    "on",
    "or",
    "redux",
    "request",
    "that",
    "the",
    "this",
    "to",
    "when",
    "with",
}


@dataclass(frozen=True)
class SimilarGitHubIssue:
    number: int
    title: str
    body: str
    url: str
    state: str
    score: float


def rank_similar_issues(
    issues: list[dict[str, Any]],
    *,
    title: str,
    description: str,
    limit: int = 5,
    minimum_score: float = 0.34,
) -> list[SimilarGitHubIssue]:
    query_title = _normalized_text(TYPE_PREFIX_RE.sub("", title))
    query_title_tokens = _tokens(query_title)
    query_tokens = query_title_tokens | _tokens(description[:3000])
    ranked: list[SimilarGitHubIssue] = []
    for raw in issues:
        try:
            number = int(raw["number"])
            candidate_title = str(raw["title"]).strip()
            url = str(raw["url"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if number <= 0 or not candidate_title or not url:
            continue
        comparable_title = _normalized_text(TYPE_PREFIX_RE.sub("", candidate_title))
        candidate_title_tokens = _tokens(comparable_title)
        body = str(raw.get("body") or "")
        candidate_tokens = candidate_title_tokens | _tokens(body[:5000])
        title_ratio = difflib.SequenceMatcher(None, query_title, comparable_title).ratio()
        title_overlap = _jaccard(query_title_tokens, candidate_title_tokens)
        combined_overlap = _jaccard(query_tokens, candidate_tokens)
        shared = len(query_tokens & candidate_tokens)
        containment = shared / max(1, min(len(query_tokens), len(candidate_tokens)))
        score = max(
            title_ratio,
            title_overlap,
            combined_overlap,
            0.72 * title_overlap + 0.28 * combined_overlap,
            0.82 * containment,
        )
        if shared < 2 and title_ratio < 0.5:
            continue
        if score < minimum_score:
            continue
        ranked.append(
            SimilarGitHubIssue(
                number=number,
                title=candidate_title,
                body=body,
                url=url,
                state=str(raw.get("state") or "UNKNOWN").upper(),
                score=score,
            )
        )
    ranked.sort(key=lambda item: (-item.score, item.number))
    return ranked[:limit]


async def find_similar_github_issues(
    repo: str,
    *,
    title: str,
    description: str,
    timeout_seconds: float = 25.0,
) -> tuple[bool, list[SimilarGitHubIssue], str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--limit",
            "500",
            "--json",
            "number,title,body,url,state",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return False, [], f"GitHub issue lookup could not start: {type(exc).__name__}."
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        return False, [], "GitHub issue lookup timed out."
    out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        return False, [], (err or out or f"GitHub issue lookup failed with exit code {proc.returncode}.")[:1000]
    try:
        payload = json.loads(out or "[]")
    except json.JSONDecodeError:
        return False, [], "GitHub issue lookup returned invalid data."
    if not isinstance(payload, list):
        return False, [], "GitHub issue lookup returned an unexpected response."
    return True, rank_similar_issues(payload, title=title, description=description), ""


def candidate_review_context(candidates: list[SimilarGitHubIssue]) -> str:
    if not candidates:
        return "No meaningfully similar existing GitHub issues were found by the local candidate search."
    sections = ["Potential existing GitHub issues (untrusted report text):"]
    for candidate in candidates:
        compact_body = re.sub(r"\s+", " ", candidate.body).strip()[:900]
        sections.append(
            f"- #{candidate.number} [{candidate.state}] {candidate.title}\n"
            f"  URL: {candidate.url}\n"
            f"  Existing report: {compact_body or '(no body)'}"
        )
    return "\n".join(sections)


def clear_duplicate_candidate(
    candidates: list[SimilarGitHubIssue],
    *,
    minimum_score: float = 0.92,
) -> SimilarGitHubIssue | None:
    """Return only an exact/near-exact title match safe to reject without a model."""

    if not candidates or candidates[0].score < minimum_score:
        return None
    return candidates[0]


def parse_duplicate_decision(
    line: str,
    candidates: list[SimilarGitHubIssue],
) -> SimilarGitHubIssue | None:
    match = re.match(r"^DUPLICATE\s*:?[\s#]*(\d+)\b", line.strip(), re.IGNORECASE)
    if not match:
        return None
    number = int(match.group(1))
    return next((candidate for candidate in candidates if candidate.number == number), None)


def _normalized_text(value: str) -> str:
    return " ".join(WORD_RE.findall(value.casefold()))


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in WORD_RE.findall(value.casefold())
        if len(token) >= 3 and token not in STOP_WORDS
    }


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)
