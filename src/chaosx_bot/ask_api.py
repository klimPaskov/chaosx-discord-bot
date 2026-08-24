"""Direct chat-completion fast path for public ChaosX answers.

Public asks are pure text completion: the bot already gathers grounding
context, conversation memory, reply-chain context, and the safety boundary
before prompting, and the public answer must never use tools. A direct
chat-completion API call avoids the multi-second Hermes CLI startup that a
subprocess costs per answer (measured on the VPS: ~4s Hermes overhead vs
~1.3s raw DeepSeek v4 Flash latency for a trivial prompt). Running zero
tools also makes this path strictly safer than a safe-toolset Hermes
subprocess: there is no code execution surface at all.

The Hermes subprocess path stays for operator/admin asks, which genuinely
need tools and project-rule context.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import AsyncIterator

import httpx

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_TIMEOUT_S = 45.0
DEFAULT_MAX_TOKENS = 1200

_KEY_LINE = re.compile(r"^DEEPSEEK_API_KEY=(.*)$", re.IGNORECASE)
_KEY_CANDIDATES = (
    Path(".env"),
    Path("/srv/chaosx/chaosx-discord-bot/.env"),
    Path.home() / ".hermes" / "profiles" / "chaos_redux" / ".env",
)


def resolve_api_key() -> str:
    """Return the DeepSeek API key from env or the bot/profile .env files."""
    value = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if value:
        return value
    for candidate in _KEY_CANDIDATES:
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            match = _KEY_LINE.match(line.strip())
            if match and match.group(1).strip():
                return match.group(1).strip()
    return ""


class DirectAskError(RuntimeError):
    """Raised when the direct completion path fails and callers may fall back."""


async def direct_chat_completion(
    *,
    system: str,
    user: str,
    model: str = "deepseek-v4-flash",
    base_url: str = DEEPSEEK_BASE_URL,
    reasoning_effort: str = "low",
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.7,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> str:
    """Run a single chat completion and return the trimmed assistant text."""
    key = resolve_api_key()
    if not key:
        raise DirectAskError("DEEPSEEK_API_KEY not found for the direct ask fast path")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    if reasoning_effort and reasoning_effort.strip().lower() in {
        "low",
        "medium",
        "high",
        "xhigh",
    }:
        payload["reasoning_effort"] = reasoning_effort.strip().lower()

    headers = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        response = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=payload,
        )
        if response.status_code != 200:
            raise DirectAskError(
                f"direct completion HTTP {response.status_code}: {response.text[:300]}"
            )
        data = response.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:  # pragma: no cover - defensive
        raise DirectAskError(f"unexpected completion payload: {str(data)[:300]}") from exc
    return (content or "").strip()


async def direct_chat_completion_stream(
    *,
    system: str,
    user: str,
    model: str = "deepseek-v4-flash",
    base_url: str = DEEPSEEK_BASE_URL,
    reasoning_effort: str = "low",
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.7,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> AsyncIterator[tuple[str, str]]:
    """Stream a chat completion, yielding (reasoning_delta, content_delta).

    Each yielded tuple holds the new reasoning text (the model's visible
    chain-of-thought, empty when reasoning is off) and the new answer text.
    This powers the owner's private live-thinking feed.
    """
    key = resolve_api_key()
    if not key:
        raise DirectAskError("DEEPSEEK_API_KEY not found for the direct ask fast path")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    if reasoning_effort and reasoning_effort.strip().lower() in {
        "low",
        "medium",
        "high",
        "xhigh",
    }:
        payload["reasoning_effort"] = reasoning_effort.strip().lower()

    headers = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        async with client.stream(
            "POST",
            f"{base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=payload,
        ) as response:
            if response.status_code != 200:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise DirectAskError(f"direct stream HTTP {response.status_code}: {body[:300]}")
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk["choices"][0]["delta"]
                except (ValueError, KeyError, IndexError, TypeError):
                    continue
                reasoning = delta.get("reasoning_content") or ""
                content = delta.get("content") or ""
                if reasoning or content:
                    yield reasoning, content
