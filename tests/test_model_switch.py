"""The public/owner model switch: allowlist, persistence, and live application."""

from types import SimpleNamespace

import pytest

from chaosx_bot import model_switch


class FakeStore:
    """In-memory stand-in for Storage's runtime_settings methods."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def runtime_setting(self, key: str) -> str:
        return self.values.get(key, "")

    async def set_runtime_setting(self, key: str, value: str) -> None:
        self.values[key] = value

    async def clear_runtime_setting(self, key: str) -> None:
        self.values.pop(key, None)


def make_settings() -> SimpleNamespace:
    return SimpleNamespace(
        ask_model="deepseek-flash",
        ask_provider="deepseek",
        ask_reasoning_effort="low",
        operator_model="deepseek-v4-pro",
        operator_provider="deepseek",
        operator_reasoning_effort="high",
    )


def test_allowlist_reports_current_deepseek_rates():
    flash = model_switch.get_option("deepseek-flash")
    pro = model_switch.get_option("deepseek-v4-pro")
    assert flash is not None and (flash.input_per_m, flash.output_per_m) == (0.15, 0.60)
    assert pro is not None and (pro.input_per_m, pro.output_per_m) == (0.66, 1.98)
    assert "off-peak" in flash.rate or "off-peak" in flash.note


def test_scopes_map_to_public_and_operator_fields():
    assert model_switch.SCOPE_FIELDS["public"][0] == "ask_model"
    assert model_switch.SCOPE_FIELDS["admin"][0] == "operator_model"


async def test_set_persists_and_applies_live():
    store, settings = FakeStore(), make_settings()
    state = await model_switch.set_model(store, settings, "public", "deepseek-flash", "minimal")

    assert settings.ask_model == "deepseek-flash"
    assert settings.ask_reasoning_effort == "minimal"
    assert state["model"] == "deepseek-flash"
    assert model_switch.override_key("public") in store.values


async def test_public_switch_leaves_the_owner_surface_alone():
    store, settings = FakeStore(), make_settings()
    await model_switch.set_model(store, settings, "public", "deepseek-flash", "minimal")

    assert settings.operator_model == "deepseek-v4-pro"
    assert settings.operator_reasoning_effort == "high"


async def test_admin_scope_switches_the_operator_model():
    store, settings = FakeStore(), make_settings()
    await model_switch.set_model(store, settings, "admin", "deepseek-v4-pro", "high")

    assert settings.operator_provider == "deepseek"
    assert model_switch.override_key("admin") in store.values


async def test_apply_stored_survives_a_restart():
    store = FakeStore()
    first = make_settings()
    await model_switch.set_model(store, first, "public", "deepseek-flash", "medium")

    restarted = make_settings()  # .env defaults again, as after a service restart
    assert restarted.ask_reasoning_effort == "low"

    applied = await model_switch.apply_stored(store, restarted)
    assert applied["public"]["model"] == "deepseek-flash"
    assert restarted.ask_reasoning_effort == "medium"


async def test_unknown_model_is_refused_without_touching_settings():
    store, settings = FakeStore(), make_settings()
    with pytest.raises(ValueError):
        await model_switch.set_model(store, settings, "public", "gpt-9-imaginary", "low")

    assert settings.ask_model == "deepseek-flash"
    assert store.values == {}


async def test_unknown_scope_and_bad_effort_are_refused():
    store, settings = FakeStore(), make_settings()
    with pytest.raises(ValueError):
        await model_switch.set_model(store, settings, "everyone", "deepseek-flash")
    with pytest.raises(ValueError):
        await model_switch.set_model(store, settings, "public", "deepseek-flash", "galaxy-brain")


async def test_corrupt_stored_override_is_ignored():
    store, settings = FakeStore(), make_settings()
    store.values[model_switch.override_key("public")] = "{not json"

    assert await model_switch.apply_stored(store, settings) == {}
    assert settings.ask_model == "deepseek-flash"


def test_provider_endpoints_are_openai_compatible():
    base, env = model_switch.endpoint_for("deepseek")
    assert base.endswith("/v1") and env == "DEEPSEEK_API_KEY"
    gemini_base, gemini_env = model_switch.endpoint_for("gemini")
    assert "generativelanguage.googleapis.com" in gemini_base
    assert gemini_env != "GEMINI_API_KEY", "the TTS-only key must not be used for text models"
    assert model_switch.endpoint_for("nonsense") == model_switch.endpoint_for("deepseek")
