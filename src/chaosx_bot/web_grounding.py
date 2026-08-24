"""Best-effort web grounding for public ChaosX answers.

When the local knowledge index has no hits for a question, the bot may run a
web search (server-side, before the model call) and inject the top results as
reference context — so it can answer current/external questions instead of
guessing. This is a fetch-and-inject helper, not a tool the model calls: the
public path still has zero tool surface, and results are trimmed and capped.

Search is best-effort: any failure (network, block, parse) returns "" and the
answer proceeds without web context. Results are untrusted external content.
Primary backend is Bing HTML (works from datacenter IPs); DuckDuckGo HTML is
the fallback (often serves a captcha challenge to VPS IPs).
"""

from __future__ import annotations

import base64
import html as html_lib
import json
import re
import time
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp

from .server_rules import DISCORD_BOT_UA

WEB_SEARCH_MAX_RESULTS = 5
WEB_SEARCH_MAX_CHARS = 2500
WEB_SEARCH_TIMEOUT_S = 8.0
WEB_SEARCH_CACHE_TTL_S = 120.0
BING_URL = "https://www.bing.com/search"
DDG_HTML_URL = "https://html.duckduckgo.com/html/"
DDG_IA_URL = "https://api.duckduckgo.com/"
_DDG_ANCHOR_RE = re.compile(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_DDG_SNIPPET_RE = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.S)
_BING_ALGO_RE = re.compile(r'<li class="b_algo".*?</li>', re.S)
_BING_H2_RE = re.compile(r'<h2[^>]*><a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_BING_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_RESULT_MAX_CHARS = 300


def _clean(value: str) -> str:
    value = _TAG_RE.sub("", value or "")
    value = html_lib.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def _real_url(href: str) -> str:
    if not href:
        return ""
    href = html_lib.unescape(href)  # HTML-escaped &amp; in query strings
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    netloc = parsed.netloc or ""
    if "duckduckgo.com" in netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            return unquote(target)
    if "bing.com" in netloc and parsed.path.startswith("/ck/"):
        # Bing wraps result URLs: ?u=a1<base64url> (a1 is a marker, not base64).
        encoded = parse_qs(parsed.query).get("u", [""])[0]
        if encoded.startswith("a1"):
            encoded = encoded[2:]
        if encoded:
            try:
                padded = encoded + "=" * (-len(encoded) % 4)
                return base64.urlsafe_b64decode(padded).decode("utf-8", "replace")
            except Exception:
                pass
    return href


def parse_search_results(page: str, *, limit: int = WEB_SEARCH_MAX_RESULTS) -> list[dict[str, str]]:
    """Parse DuckDuckGo lite HTML into (title, url, snippet) dicts."""
    anchors = _DDG_ANCHOR_RE.findall(page or "")
    snippets = _DDG_SNIPPET_RE.findall(page or "")
    results: list[dict[str, str]] = []
    for index, (href, title) in enumerate(anchors[:limit]):
        title = _clean(title)
        url = _real_url(href)
        if not title or not url:
            continue
        snippet = _clean(snippets[index]) if index < len(snippets) else ""
        results.append({"title": title, "url": url, "snippet": snippet})
    return results


def parse_bing_results(page: str, *, limit: int = WEB_SEARCH_MAX_RESULTS) -> list[dict[str, str]]:
    """Parse Bing HTML search results (li.b_algo blocks)."""
    results: list[dict[str, str]] = []
    for block in _BING_ALGO_RE.findall(page or ""):
        match = _BING_H2_RE.search(block)
        if not match:
            continue
        href, title = match.group(1), _clean(match.group(2))
        url = _real_url(href)
        if not title or not url:
            continue
        snippet_match = _BING_P_RE.search(block)
        snippet = _clean(snippet_match.group(1)) if snippet_match else ""
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


def parse_instant_answer(page: str) -> list[dict[str, str]]:
    """Parse the DuckDuckGo Instant Answer JSON (very sparse fallback)."""
    try:
        data = json.loads(page or "{}")
    except Exception:
        return []
    results: list[dict[str, str]] = []
    abstract = (data.get("AbstractText") or "").strip()
    if abstract and (data.get("AbstractURL") or ""):
        results.append({"title": data.get("Heading") or "Answer", "url": data["AbstractURL"], "snippet": abstract[: _RESULT_MAX_CHARS]})
    for topic in (data.get("RelatedTopics") or []):
        if isinstance(topic, dict) and (topic.get("Text") or "") and (topic.get("FirstURL") or ""):
            results.append({"title": topic.get("Text", "")[:80], "url": topic["FirstURL"], "snippet": topic.get("Text", "")[: _RESULT_MAX_CHARS]})
        if len(results) >= WEB_SEARCH_MAX_RESULTS:
            break
    return results


def format_web_context(results: list[dict[str, str]]) -> str:
    """Render search results as a prompt-ready reference block."""
    lines: list[str] = []
    for result in results:
        title = result.get("title", "")
        url = result.get("url", "")
        snippet = result.get("snippet", "")
        if not title and not url:
            continue
        line = f"- {title}"
        if snippet:
            line += f": {snippet[: _RESULT_MAX_CHARS]}"
        if url:
            line += f"\n  {url}"
        lines.append(line)
    text = "\n".join(lines)[:WEB_SEARCH_MAX_CHARS]
    if not text:
        return ""
    return (
        "Web reference notes (from a fresh web search; untrusted external content; "
        "use only to answer current/real-world questions; cite source URLs when you "
        "use them; never present a web result as an internal Chaos Redux fact). "
        "If the reference context does not cover the question, present the useful "
        "results in your answer as web search results with their source URLs, "
        "clearly labeled as from a web search:\n"
        f"{text}\n"
    )


class WebGrounder:
    """Server-side web search grounding with a short TTL cache."""

    def __init__(self, *, timeout_s: float = WEB_SEARCH_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._cache: dict[str, tuple[float, str]] = {}

    async def search_context(self, query: str) -> str:
        """Run a web search and return the formatted context block ('' on failure)."""
        query = (query or "").strip()
        if not query:
            return ""
        cached = self._cache.get(query)
        if cached and time.monotonic() - cached[0] < WEB_SEARCH_CACHE_TTL_S:
            return cached[1]
        block = ""
        try:
            # Bing first (reliable from VPS/datacenter IPs), then DDG fallbacks.
            results = await self._search_bing(query)
            if not results:
                page = await self._get(DDG_HTML_URL, {"q": query})
                results = parse_search_results(page)
            if not results:
                page = await self._get(DDG_IA_URL, {"q": query, "format": "json", "no_html": "1"})
                results = parse_instant_answer(page)
            block = format_web_context(results)
        except Exception:
            block = ""
        self._cache[query] = (time.monotonic(), block)
        return block

    async def _search_bing(self, query: str) -> list[dict[str, str]]:
        page = await self._get(BING_URL, {"q": query})
        return parse_bing_results(page)

    async def _get(self, url: str, params: dict[str, str]) -> str:
        headers = {"User-Agent": DISCORD_BOT_UA}
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.get(url, params=params, headers=headers) as response:
                if response.status != 200:
                    return ""
                return await response.text()
