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
    mention_ask_enabled: bool = Field(default=True, description="Allow direct @ChaosX mentions to act like public /ask; requires Discord Message Content Intent")
    auto_scan_enabled: bool = Field(default=True, description="Scan every new guild message with local rules, then use the public model for any posted auto-scan response")
    auto_scan_auto_answer_enabled: bool = Field(default=True, description="Allow auto-scan to route clearly in-domain questions plus local/catalog context through the public model")
    auto_scan_soft_warning_enabled: bool = Field(default=True, description="Allow auto-scan to route obvious rule problems through the public model for soft warnings")
    auto_scan_bot_topic_enabled: bool = Field(default=True, description="Allow auto-scan to route explicit ChaosX/the-bot conversation through the public model for dynamic banter")
    auto_scan_shadow_mode: bool = Field(default=False, description="Classify and log auto-scan actions without posting public replies")
    auto_scan_max_message_chars: int = Field(default=800, ge=80, le=4000)
    auto_scan_min_confidence: int = Field(default=100, ge=1, le=100)
    auto_scan_answer_limit_per_user_hour: int = Field(default=6, ge=0, le=100)
    auto_scan_warning_limit_per_user_hour: int = Field(default=3, ge=0, le=50)
    auto_scan_banter_limit_per_user_hour: int = Field(default=8, ge=0, le=100)
    auto_scan_notify_channel_id: Optional[int] = Field(default=None, description="Channel for auto-scan moderation notices; defaults to automation_reminder_channel_id")
    auto_scan_excluded_channel_ids: str = Field(default="", description="Comma-separated Discord channel/thread IDs ignored by auto-scan")
    admin_context_message_limit: int = Field(default=120, ge=10, le=500, description="Max recent Discord messages /admin ask may fetch for explicit analysis requests")

    @model_validator(mode="before")
    @classmethod
    def blank_optional_ints_to_none(cls, data):
        if isinstance(data, dict):
            for key in (
                "allowed_guild_id",
                "command_guild_id",
                "automation_reminder_channel_id",
                "content_dump_channel_id",
                "community_event_ideas_channel_id",
                "access_reaction_channel_id",
                "access_reaction_message_id",
                "access_reaction_chaos_emoji_id",
                "access_reaction_member_role_id",
                "access_reaction_modder_role_id",
                "auto_scan_notify_channel_id",
            ):
                if data.get(key) == "":
                    data[key] = None
        return data

    chaos_redux_repo: Path = Field(default=Path("/home/klim/projects/chaos_redux"))
    hermes_bin: Path = Field(default=Path("/home/klim/.local/bin/hermes"))
    hermes_profile: str = Field(default="chaos_redux")
    hermes_timeout_seconds: int = Field(default=900, ge=30, le=1800)
    admin_ask_timeout_seconds: int = Field(default=0, ge=0, le=86400, description="Max seconds for /admin ask Hermes runs; 0 disables the subprocess timeout")
    admin_ask_memory_turns: int = Field(default=5, ge=0, le=20, description="Previous /admin ask turns to inject for owner follow-up context; 0 disables follow-up memory")
    admin_ask_memory_keep_last: int = Field(default=20, ge=1, le=100, description="Stored /admin ask turns to retain per owner/channel/thread")
    reply_context_turns: int = Field(default=6, ge=0, le=20, description="Previous model-backed ChaosX reply-chain turns to inject when a user replies to a stored bot answer; 0 disables reply-chain context")
    reply_memory_keep_last: int = Field(default=0, ge=0, le=10000, description="Stored model-backed message asks to retain per guild/channel; 0 keeps all stored asks")
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
    obsidian_vault_path: Path = Field(default=Path("/mnt/c/Users/klimp/Documents/Chaos Redux Vault"), description="Chaos Redux Obsidian/LLM wiki vault path")
    community_notes_enabled: bool = Field(default=True, description="Write approved public suggestions/event ideas to the Chaos Redux vault")
    community_event_specs_folder: str = Field(default="Events/Event Specs", description="Vault-relative folder for approved community event idea specs")
    community_suggestions_folder: str = Field(default="Planning/Community Suggestions", description="Vault-relative folder for approved community suggestion notes")
    community_event_ideas_channel_id: Optional[int] = Field(default=1395464994639839356, description="Discord forum/text channel for approved /event-idea posts; blank disables auto-posting")
    automation_reminder_channel_id: Optional[int] = Field(default=1395464062367698977, description="Discord channel for automation reminders/digests")
    content_dump_channel_id: Optional[int] = Field(default=1516054706286235768, description="Discord channel for weekly image-led content dumps")
    access_reaction_channel_id: Optional[int] = Field(default=1396027815786188890, description="Info channel for the access reaction-role message")
    access_reaction_message_id: Optional[int] = Field(default=1526508030886154331, description="Message whose reactions control community access roles")
    access_reaction_chaos_emoji_id: Optional[int] = Field(default=1525495423949864960, description="Custom Chaos Redux logo emoji ID for the community-only role")
    access_reaction_chaos_emoji_name: str = Field(default="chaosx_logo", description="Custom Chaos Redux logo emoji name")
    access_reaction_mod_emoji: str = Field(default="💻", description="Unicode computer emoji for mod-development access")
    access_reaction_member_role_id: Optional[int] = Field(default=1526507892310675539, description="Role granted for Chaos Redux community access")
    access_reaction_modder_role_id: Optional[int] = Field(default=1526507893837529138, description="Role granted for mod-development access")

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
