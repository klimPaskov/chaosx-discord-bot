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
    owner_id: int = Field(default=789502982122373150, description="Only this Discord user ID may use ChaosX")
    allowed_guild_id: Optional[int] = Field(default=None)
    command_guild_id: Optional[int] = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def blank_optional_ints_to_none(cls, data):
        if isinstance(data, dict):
            for key in ("allowed_guild_id", "command_guild_id"):
                if data.get(key) == "":
                    data[key] = None
        return data

    chaos_redux_repo: Path = Field(default=Path("/home/klim/projects/chaos_redux"))
    hermes_bin: Path = Field(default=Path("/home/klim/.local/bin/hermes"))
    hermes_profile: str = Field(default="chaos_redux")
    hermes_timeout_seconds: int = Field(default=300, ge=30, le=1800)
    db_path: Path = Field(default=Path("./chaosx.db"))

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


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
