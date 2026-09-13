"""Human-readable names for the DeepSeek models the bot runs on.

The DeepSeek API id is an internal slug (deepseek-flash); when ChaosX is asked
what model it runs on it should answer with the real release name. DeepSeek
retired V4 Flash and V4 Flash Vision Exp on 2026-09-10 and temporarily routes
their ids (deepseek-v4-flash, deepseek-v4-flash-vision-exp, deepseek-chat,
deepseek-reasoner) to V4.1 Flash, so those ids share the same display name.
"""

from __future__ import annotations

MODEL_DISPLAY_NAMES: dict[str, str] = {
    "deepseek-flash": "DeepSeek V4.1 Flash",
    "deepseek-v4-flash": "DeepSeek V4.1 Flash",
    "deepseek-v4-flash-vision-exp": "DeepSeek V4.1 Flash",
    "deepseek-chat": "DeepSeek V4.1 Flash",
    "deepseek-reasoner": "DeepSeek V4.1 Flash",
    "deepseek-v4-pro": "DeepSeek V4 Pro",
}


def display_model_name(model_id: str | None) -> str:
    """Public release name for a model id; unknown ids pass through unchanged."""
    key = (model_id or "").strip()
    return MODEL_DISPLAY_NAMES.get(key, key)
