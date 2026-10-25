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

import urllib.request

import inspect
import json
import os
import re
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping

import httpx

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


def _endpoint_for(provider: str) -> tuple[str, str]:
    """Resolve a provider to (OpenAI-compatible base URL, key env var).

    Imported lazily to keep model_switch (which reads this module for key lookup) free of
    an import cycle at module load.
    """
    from .model_switch import endpoint_for

    return endpoint_for(provider)
DEFAULT_TIMEOUT_S = 45.0
DEFAULT_MAX_TOKENS = 4096

def _key_pattern(env_name: str) -> re.Pattern[str]:
    """Match one ``KEY=value`` line, ignoring case and surrounding whitespace."""
    return re.compile(rf"^{re.escape(env_name)}\s*=\s*(.*)$", re.IGNORECASE)

_KEY_CANDIDATES = (
    Path(".env"),
    Path("/srv/chaosx/chaosx-discord-bot/.env"),
    Path.home() / ".hermes" / "profiles" / "chaos_redux" / ".env",
)


async def _emit_usage(on_usage, usage: dict[str, Any] | None) -> None:
    """Dispatch a usage block to an optional sync/async callback (fire-and-forget safe)."""
    if on_usage is None or not usage:
        return
    result = on_usage(usage)
    if inspect.isawaitable(result):
        await result


def resolve_api_key(env_name: str = "DEEPSEEK_API_KEY", environ: Mapping[str, str] | None = None) -> str:
    """Return a provider API key from the environment or the bot/profile .env files."""
    env = os.environ if environ is None else environ
    value = str(env.get(env_name, "")).strip()
    if value:
        return value
    pattern = _key_pattern(env_name)
    for candidate in _KEY_CANDIDATES:
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            match = pattern.match(line.strip())
            if match and match.group(1).strip():
                return match.group(1).strip()
    return ""


class DirectAskError(RuntimeError):
    """Raised when the direct completion path fails and callers may fall back."""


def _build_user_content(user: str, images: list[str] | None = None):
    """Build the user message content passed to the model.

    With no images this is plain text (the existing fast path). When images
    are supplied (user screenshots, mod assets, MCP-rendered PNGs, etc.), use
    an OpenAI-style content-part list so a vision-capable model can analyze
    them: text first, then one image_url part per image. The text carries the
    full system boundary + grounding context; images ride alongside it.
    """
    if not images:
        return user
    parts: list[dict] = [{"type": "text", "text": user}]
    for image in images:
        parts.append({"type": "image_url", "image_url": {"url": image}})
    return parts


async def direct_chat_completion(
    *,
    system: str,
    user: str,
    model: str = "deepseek-flash",
    provider: str = "deepseek",
    base_url: str | None = None,
    reasoning_effort: str = "low",
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.7,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    images: list[str] | None = None,
    on_usage: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
) -> str:
    """Run a single chat completion and return the trimmed assistant text."""
    resolved_base, key_env = _endpoint_for(provider)
    base_url = (base_url or resolved_base).rstrip("/")
    key = resolve_api_key(key_env)
    if not key:
        raise DirectAskError(f"{key_env} not found for the direct ask fast path")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": _build_user_content(user, images)},
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
    await _emit_usage(on_usage, data.get("usage"))
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:  # pragma: no cover - defensive
        raise DirectAskError(f"unexpected completion payload: {str(data)[:300]}") from exc
    return (content or "").strip()


async def direct_chat_completion_stream(
    *,
    system: str,
    user: str,
    model: str = "deepseek-flash",
    provider: str = "deepseek",
    base_url: str | None = None,
    reasoning_effort: str = "low",
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.7,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    images: list[str] | None = None,
    on_usage: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
) -> AsyncIterator[tuple[str, str]]:
    """Stream a chat completion, yielding (reasoning_delta, content_delta).

    Each yielded tuple holds the new reasoning text (the model's visible
    chain-of-thought, empty when reasoning is off) and the new answer text.
    This powers the owner's private live-thinking feed.
    """
    resolved_base, key_env = _endpoint_for(provider)
    base_url = (base_url or resolved_base).rstrip("/")
    key = resolve_api_key(key_env)
    if not key:
        raise DirectAskError(f"{key_env} not found for the direct ask fast path")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": _build_user_content(user, images)},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
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
                except ValueError:
                    continue
                if chunk.get("usage"):
                    await _emit_usage(on_usage, chunk.get("usage"))
                    continue
                try:
                    delta = chunk["choices"][0]["delta"]
                except (KeyError, IndexError, TypeError):
                    continue
                reasoning = delta.get("reasoning_content") or ""
                content = delta.get("content") or ""
                if reasoning or content:
                    yield reasoning, content


_IMAGE_SAFETY_SYSTEM = (
    "You are a strict content-safety classifier for a Discord server about a Hearts of Iron IV mod. "
    "Look at the image and answer with JSON only: "
    '{"sexual": true|false, "gore": true|false, "nazi": true|false, "confidence": 0.0-1.0, "what": "<= 12 words"}. '
    "sexual = nudity, sexual acts, sexualised minors, or explicit imagery. gore = mutilated bodies, real wounds, "
    "graphic death. nazi = swastikas, SS insignia or other hate symbols. A historical game screenshot, a map, a "
    "uniform photo or a stylised portrait is NOT gore or sexual. When unsure, set confidence below 0.5."
)


async def describe_image_safety(image_url: str, *, model: str = "", provider: str = "") -> dict:
    """Ask the model what an attachment contains. Returns the parsed verdict, or {} when it cannot tell.

    Verified against the live provider on 2026-10-25: deepseek-flash accepts image_url content parts and described
    a test image correctly, so NSFW/gore screening needs no extra service.
    """
    import base64
    import json as _json

    from .config import load_settings

    settings = load_settings()

    def _key_name() -> str:
        joined = f"{provider or getattr(settings, 'ask_provider', '')}{model or getattr(settings, 'ask_model', '')}".lower()
        return "DEEPSEEK_API_KEY" if "deepseek" in joined else "OPENAI_API_KEY"

    def _base() -> str:
        joined = f"{provider or getattr(settings, 'ask_provider', '')}{model or getattr(settings, 'ask_model', '')}".lower()
        return "https://api.deepseek.com/v1" if "deepseek" in joined else "https://api.openai.com/v1"

    url = image_url
    if not url.startswith("http"):
        try:
            with open(url, "rb") as handle:
                url = "data:image/png;base64," + base64.b64encode(handle.read()).decode()
        except OSError:
            return {}
    payload = {
        "model": model or getattr(settings, "ask_model", ""),
        "max_tokens": 120,
        "messages": [
            {"role": "system", "content": _IMAGE_SAFETY_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Classify this attachment."},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            },
        ],
    }
    request = urllib.request.Request(
        f"{_base()}/chat/completions",
        data=_json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {resolve_api_key(_key_name())}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            body = _json.load(response)
        text = (body["choices"][0]["message"].get("content") or "").strip()
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return {}
        return _json.loads(text[start : end + 1])
    except Exception as exc:
        print(f"[vision] classify failed: {type(exc).__name__}: {exc}", flush=True)
        return {}
