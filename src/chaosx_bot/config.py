from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment or .env."""

    model_config = SettingsConfigDict(
        env_prefix="CHAOSX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    discord_token: str = Field(default="", description="Discord bot token", repr=False)
    application_description: str = Field(default="Ask ChaosX questions about Chaos Redux events, scenarios, mechanics, testing, and mod info.")
    owner_id: int = Field(default=789502982122373150, description="Discord user ID with admin/automation access")
    allowed_guild_id: Optional[int] = Field(default=1395459671598436533)
    command_guild_id: Optional[int] = Field(default=1395459671598436533)
    public_ask_limit_per_hour: int = Field(default=10, ge=0, le=100)
    public_scripted_limit_per_hour: int = Field(default=20, ge=0, le=500)
    public_prompt_max_chars: int = Field(default=600, ge=100, le=4000)

    @model_validator(mode="before")
    @classmethod
    def blank_optional_ints_to_none(cls, data):
        if isinstance(data, dict):
            for key in ("allowed_guild_id", "command_guild_id", "automation_reminder_channel_id", "content_dump_channel_id"):
                if data.get(key) == "":
                    data[key] = None
        return data

    chaos_redux_repo: Path = Field(default=Path("/home/klim/projects/chaos_redux"))
    hermes_bin: Path = Field(default=Path("/home/klim/.local/bin/hermes"))
    hermes_profile: str = Field(default="chaos_redux")
    hermes_timeout_seconds: int = Field(default=300, ge=30, le=1800)
    ask_model: str = Field(default="gpt-5.6-luna", description="Model override for broad ask commands")
    ask_provider: str = Field(default="openai-codex", description="Provider override for broad ask commands")
    ask_reasoning_effort: str = Field(default="medium", description="Reasoning effort for broad ask commands")
    operator_model: str = Field(default="gpt-5.6-luna", description="Model override for protected autonomous server operations")
    operator_provider: str = Field(default="openai-codex", description="Provider override for protected autonomous server operations")
    operator_reasoning_effort: str = Field(default="xhigh", description="Reasoning effort for protected autonomous server operations")
    webhook_host: str = Field(default="127.0.0.1")
    webhook_port: int = Field(default=8787, ge=1, le=65535)
    github_webhook_secret: str = Field(default="", repr=False)
    webhook_public_base_url: str = Field(default="")
    db_path: Path = Field(default=Path("./chaosx.db"))
    github_repo: str = Field(default="klimPaskov/Chaos-Redux", description="GitHub repo for public /issue creation")
    automation_reminder_channel_id: Optional[int] = Field(default=1395464062367698977, description="Discord channel for automation reminders/digests")
    content_dump_channel_id: Optional[int] = Field(default=1516054706286235768, description="Discord channel for weekly image-led content dumps")

    @model_validator(mode="after")
    def default_allowed_guild_to_command_guild(self):
        if self.allowed_guild_id is None and self.command_guild_id is not None:
            self.allowed_guild_id = self.command_guild_id
        return self

    @field_validator("discord_token")
    @classmethod
    def token_not_placeholder(cls, value: str) -> str:
        if value.strip() in {"", "changeme", "paste-token-here"}:
            return ""
        return value.strip()

    @field_validator("hermes_profile")
    @classmethod
    def profile_is_simple(cls, value: str) -> str:
        value = value.strip()
        if not value.replace("_", "").replace("-", "").isalnum():
            raise ValueError("Hermes profile may contain only letters, numbers, dash, underscore")
        return value

    @field_validator("ask_reasoning_effort", "operator_reasoning_effort")
    @classmethod
    def reasoning_effort_is_supported(cls, value: str) -> str:
        value = value.strip().lower()
        allowed = {"", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
        if value not in allowed:
            raise ValueError(f"reasoning effort must be one of {sorted(allowed)}")
        return value


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
