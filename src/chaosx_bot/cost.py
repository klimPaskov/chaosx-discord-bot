"""Cost & usage awareness for the ChaosX bot.

The bot can answer "how much does answering cost / how much per api call" by
looking up real token usage it has recorded (DeepSeek streams back a ``usage``
block per completion), priced with a per-model table. Like the model-name
lookup, this is *not* baked into every prompt — it is injected only when
someone actually asks, via :func:`looks_like_cost_question`.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_PRICING: dict[str, dict[str, float]] = {
    # Per-million-token USD prices. Configurable via CHAOSX_MODEL_PRICING.
    "deepseek-v4-flash-vision-exp": {"input": 0.50, "output": 1.50},
}

_EMPTY_BUCKET = {
    "calls": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "reasoning_tokens": 0,
    "cost_estimate": 0.0,
}


def compute_cost(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    pricing: dict[str, dict[str, float]] | None = None,
) -> float:
    """Estimate USD cost for one call using the per-million-token price table."""
    table = pricing or DEFAULT_PRICING
    prices = table.get(model) or {k: 0.0 for k in ("input", "output")}
    input_price = float(prices.get("input", 0.0) or 0.0)
    output_price = float(prices.get("output", 0.0) or 0.0)
    return (
        (prompt_tokens / 1_000_000) * input_price
        + (completion_tokens / 1_000_000) * output_price
    )


def _normalize_usage(usage: dict[str, Any] | None) -> tuple[int, int, int]:
    """Return (prompt_tokens, completion_tokens, reasoning_tokens) from a usage block."""
    usage = usage or {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    completion_details = usage.get("completion_tokens_details") or {}
    reasoning_tokens = int(completion_details.get("reasoning_tokens") or 0)
    return prompt_tokens, completion_tokens, reasoning_tokens


class CostTracker:
    """Accumulate per-call token usage and estimated cost, persisted to JSON.

    Thread-safe (the bot records usage from async completions). Survival across
    restarts means the bot can report cumulative spend, not just this process.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            data.setdefault("total", dict(_EMPTY_BUCKET))
            data.setdefault("models", {})
            data.setdefault("last", None)
            return data
        except (OSError, ValueError):
            return {"total": dict(_EMPTY_BUCKET), "models": {}, "last": None}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2)
            os.replace(tmp, self._path)
        except OSError:
            pass

    def record(
        self,
        *,
        model: str,
        usage: dict[str, Any] | None,
        pricing: dict[str, dict[str, float]] | None = None,
    ) -> dict[str, Any]:
        """Record one completion's usage; returns the per-call cost breakdown."""
        prompt_tokens, completion_tokens, reasoning_tokens = _normalize_usage(usage)
        cost = compute_cost(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            pricing=pricing,
        )
        with self._lock:
            bucket = self._data["models"].setdefault(model, dict(_EMPTY_BUCKET))
            bucket["calls"] += 1
            bucket["prompt_tokens"] += prompt_tokens
            bucket["completion_tokens"] += completion_tokens
            bucket["reasoning_tokens"] += reasoning_tokens
            bucket["cost_estimate"] += cost

            total = self._data["total"]
            total["calls"] += 1
            total["prompt_tokens"] += prompt_tokens
            total["completion_tokens"] += completion_tokens
            total["reasoning_tokens"] += reasoning_tokens
            total["cost_estimate"] += cost

            self._data["last"] = {
                "model": model,
                "timestamp": time.time(),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "reasoning_tokens": reasoning_tokens,
                "cost_estimate": cost,
            }
            self._save()
        return {
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "reasoning_tokens": reasoning_tokens,
            "cost": cost,
        }

    def summary(
        self,
        *,
        pricing: dict[str, dict[str, float]] | None = None,
    ) -> str:
        """Render a human-readable usage/cost summary the bot can quote."""
        pricing = pricing or DEFAULT_PRICING
        with self._lock:
            total = self._data["total"]
            last = self._data["last"]
            models = self._data["models"]

        lines: list[str] = []
        if last:
            last_cost = float(last.get("cost_estimate") or 0.0)
            lines.append(
                f"Last call: {last['model']} used "
                f"{last['prompt_tokens']} prompt + {last['completion_tokens']} completion "
                f"({last.get('reasoning_tokens', 0)} reasoning) tokens ≈ ${last_cost:.4f}"
            )
        if models:
            for model, b in sorted(models.items()):
                p = pricing.get(model) or {k: 0.0 for k in ("input", "output")}
                lines.append(
                    f"{model}: {b['calls']} call(s), "
                    f"{b['prompt_tokens']} prompt + {b['completion_tokens']} completion "
                    f"({b['reasoning_tokens']} reasoning) tokens, "
                    f"≈ ${b['cost_estimate']:.4f} "
                    f"(@ ${p.get('input', 0.0):.2f}/1M in, ${p.get('output', 0.0):.2f}/1M out)"
                )
        lines.append(
            f"All-time: {total['calls']} call(s), "
            f"{total['prompt_tokens']} prompt + {total['completion_tokens']} completion "
            f"({total['reasoning_tokens']} reasoning) tokens ≈ ${total['cost_estimate']:.4f}"
        )
        if not lines:
            return "No API usage has been recorded yet."
        return "\n".join(lines)
