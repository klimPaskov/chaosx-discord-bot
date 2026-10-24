"""Runtime-switchable answer models for the public and protected admin surfaces.

An admin command repoints either surface at a different model without a redeploy: the
choice is persisted in ``runtime_settings`` and applied to the live Settings object, so
every existing ``settings.ask_model`` read (public asks) or ``settings.operator_model``
read (protected owner runs) picks it up immediately. The public surface has to stay
cheap, while the owner surface keeps the smart model (Hoops, 2026-09-27).

Prices are published list rates in USD per 1M tokens, checked 2026-09-27. DeepSeek bills
peak and off-peak windows -- peak is Mon-Fri 01:00-04:00 and 06:00-10:00 UTC and doubles
the rate quoted here -- so scheduling bulk work outside those windows halves the bill.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping

PUBLIC_SCOPE = "public"
ADMIN_SCOPE = "admin"
SCOPES: tuple[str, ...] = (PUBLIC_SCOPE, ADMIN_SCOPE)

# scope -> (model field, provider field, reasoning field) on Settings
SCOPE_FIELDS: dict[str, tuple[str, str, str]] = {
    PUBLIC_SCOPE: ("ask_model", "ask_provider", "ask_reasoning_effort"),
    ADMIN_SCOPE: ("operator_model", "operator_provider", "operator_reasoning_effort"),
}

REASONING_EFFORTS: tuple[str, ...] = (
    "", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
)

# provider -> (OpenAI-compatible base URL, env var holding its key)
PROVIDER_ENDPOINTS: dict[str, tuple[str, str]] = {
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "CHAOSX_GEMINI_TEXT_API_KEY"),
}


@dataclass(frozen=True)
class ModelOption:
    """One switchable model with the rate the switch reports."""

    model: str
    provider: str
    label: str
    input_per_m: float
    output_per_m: float
    note: str = ""

    @property
    def rate(self) -> str:
        return f"${self.input_per_m:g} in / ${self.output_per_m:g} out per 1M tokens"


OPTIONS: tuple[ModelOption, ...] = (
    ModelOption(
        "deepseek-flash", "deepseek", "DeepSeek V4.1 Flash", 0.15, 0.60,
        "off-peak rate; peak is double",
    ),
    ModelOption(
        "deepseek-v4-pro", "deepseek", "DeepSeek V4 Pro", 0.66, 1.98,
        "smarter and slower, ~4x the price",
    ),
    ModelOption(
        "gemini-2.5-flash-lite", "gemini", "Gemini 2.5 Flash-Lite", 0.10, 0.40,
        "needs a text-capable Gemini key (keep the TTS key TTS-only)",
    ),
    ModelOption(
        "gemini-3.8-flash", "gemini", "Gemini 3.8 Flash", 0.75, 3.75,
        "needs a text-capable Gemini key",
    ),
)


def override_key(scope: str) -> str:
    """Database key holding one scope's persisted override."""
    return f"model_override_{scope}"


def get_option(model_id: str) -> ModelOption | None:
    """Return the allowlist entry for a model id, or None when it is not offered."""
    wanted = (model_id or "").strip()
    for option in OPTIONS:
        if option.model == wanted:
            return option
    return None


def endpoint_for(provider: str) -> tuple[str, str]:
    """Return (base_url, key_env) for a provider, defaulting to DeepSeek."""
    return PROVIDER_ENDPOINTS.get((provider or "").strip().lower(), PROVIDER_ENDPOINTS["deepseek"])


def key_available(env_name: str, environ: Mapping[str, str] | None = None) -> bool:
    """True when a provider key resolves from the environment or a known .env file."""
    from .ask_api import resolve_api_key

    return bool(resolve_api_key(env_name, environ=environ))


def usable_options(environ: Mapping[str, str] | None = None) -> list[ModelOption]:
    """Options whose provider key is actually present, so the switch never offers a dead model."""
    if environ is None:
        environ = os.environ
    usable: list[ModelOption] = []
    for option in OPTIONS:
        key_env = endpoint_for(option.provider)[1]
        if key_env in environ and not str(environ.get(key_env, "")).strip():
            continue
        if key_available(key_env, environ=environ):
            usable.append(option)
    return usable


def snapshot(settings: Any, scope: str) -> dict[str, str]:
    """Current effective model/provider/reasoning for one scope."""
    model_field, provider_field, reasoning_field = SCOPE_FIELDS[scope]
    return {
        "scope": scope,
        "model": str(getattr(settings, model_field, "") or ""),
        "provider": str(getattr(settings, provider_field, "") or ""),
        "reasoning": str(getattr(settings, reasoning_field, "") or ""),
    }


def _write_fields(settings: Any, scope: str, model: str, provider: str, reasoning: str) -> None:
    model_field, provider_field, reasoning_field = SCOPE_FIELDS[scope]
    setattr(settings, model_field, model)
    setattr(settings, provider_field, provider)
    if reasoning in REASONING_EFFORTS:
        setattr(settings, reasoning_field, reasoning)


async def apply_stored(store: Any, settings: Any) -> dict[str, dict[str, str]]:
    """Re-apply persisted overrides at startup; returns the scopes that were applied."""
    applied: dict[str, dict[str, str]] = {}
    for scope in SCOPES:
        raw = await store.runtime_setting(override_key(scope))
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        option = get_option(str(payload.get("model") or ""))
        if option is None:
            continue
        _write_fields(settings, scope, option.model, option.provider, str(payload.get("reasoning") or ""))
        applied[scope] = snapshot(settings, scope)
    return applied


async def set_model(
    store: Any,
    settings: Any,
    scope: str,
    model_id: str,
    reasoning: str | None = None,
) -> dict[str, str]:
    """Validate, persist, and apply a model switch for one scope."""
    if scope not in SCOPES:
        raise ValueError(f"unknown scope: {scope}")
    option = get_option(model_id)
    if option is None:
        allowed = ", ".join(o.model for o in OPTIONS)
        raise ValueError(f"`{model_id}` is not in the switch list ({allowed})")
    key_env = endpoint_for(option.provider)[1]
    if not key_available(key_env):
        raise ValueError(f"{option.label} needs {key_env} to be set before switching")
    effort = (reasoning if reasoning is not None else snapshot(settings, scope)["reasoning"]).strip().lower()
    if effort not in REASONING_EFFORTS:
        raise ValueError(f"reasoning effort must be one of {', '.join(x for x in REASONING_EFFORTS if x)}")
    _write_fields(settings, scope, option.model, option.provider, effort)
    await store.set_runtime_setting(
        override_key(scope),
        json.dumps({"model": option.model, "reasoning": effort}, sort_keys=True),
    )
    return snapshot(settings, scope)


async def reset_model(store: Any, settings: Any, scope: str) -> dict[str, str]:
    """Drop the override and restore the values the environment/.env provides."""
    if scope not in SCOPES:
        raise ValueError(f"unknown scope: {scope}")
    from .config import load_settings

    fresh = load_settings()
    for field in SCOPE_FIELDS[scope]:
        setattr(settings, field, getattr(fresh, field, getattr(settings, field, "")))
    await store.clear_runtime_setting(override_key(scope))
    return snapshot(settings, scope)
