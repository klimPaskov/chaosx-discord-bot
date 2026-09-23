from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .cost import DEFAULT_PRICING


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
    public_ask_limit_per_hour: int = Field(default=-1, ge=-1, le=100, description="Public ask rate limit per user per hour; -1 = unlimited (no check), 0 = disabled")
    web_search_enabled: bool = Field(default=True, description="Allow server-side web-search grounding for public asks when local notes have no answer")
    web_evidence_enabled: bool = Field(default=True, description="Fetch the top web-search result pages and use their real page text/tables; attaches a source-table image when one is found")
    web_evidence_max_pages: int = Field(default=2, ge=0, le=3, description="How many search-result pages to fetch as source evidence; 0 disables page fetching")
    thinking_feed_enabled: bool = Field(default=False, description="Show a per-user dismissable thinking feed to the user the bot is answering (owner always gets the raw ephemeral feed); False hides thinking from everyone else")
    server_owner_name: str = Field(default="Hoops McCann", description="Display name of the Discord server owner")
    bot_maker_name: str = Field(default="Hoops McCann", description="Who made/maintains the ChaosX bot")
    main_dev_name: str = Field(default="Hoops McCann", description="Main developer of Chaos Redux")
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
    video_processing_enabled: bool = Field(default=True, description="Analyse video attachments and video links (sampled frames + speech transcript)")
    video_max_bytes: int = Field(default=40_000_000, ge=0, le=500_000_000, description="Max bytes of a video attachment/link to download and analyse")
    video_max_seconds: int = Field(default=300, ge=0, le=3600, description="Max video seconds analysed; longer clips are cut to this window")
    video_frame_count: int = Field(default=4, ge=0, le=8, description="Frames sampled per video for the vision model")
    video_frame_width: int = Field(default=768, ge=240, le=1600, description="Width of each sampled frame in pixels")
    video_max_processed_images: int = Field(default=6, ge=1, le=10, description="Total images (attachments + video frames) sent to the model per message")
    video_link_ytdlp_enabled: bool = Field(default=True, description="Use yt-dlp for platform video links (YouTube etc.) when the binary is available")
    context_attachment_enabled: bool = Field(default=True, description="Pull attachments from the replied-to message or recent channel messages when the ask itself carries none")
    context_attachment_lookback_messages: int = Field(default=10, ge=1, le=50, description="How many recent channel messages to scan for a context attachment")
    context_attachment_lookback_minutes: int = Field(default=120, ge=1, le=1440, description="Ignore context attachments older than this many minutes")
    context_attachment_guild_scan_enabled: bool = Field(default=True, description="Look for an already-sent attachment in other server channels when the request points at media")
    context_attachment_guild_channels: int = Field(default=25, ge=1, le=100, description="Max other channels scanned for an already-sent attachment")
    context_attachment_guild_messages: int = Field(default=20, ge=1, le=100, description="Messages read per channel during the server-wide attachment scan")
    context_attachment_guild_max_age_minutes: int = Field(default=360, ge=5, le=10080, description="Ignore server-wide attachments older than this many minutes")
    stt_enabled: bool = Field(default=True, description="Transcribe speech in videos/audio with the local speech model")
    stt_model: str = Field(default="base", description="Local faster-whisper model size (tiny|base|small|medium|large-v3)")
    stt_language: str = Field(default="en", description="Speech language hint; blank = auto-detect")
    stt_max_seconds: int = Field(default=180, ge=0, le=1800, description="Max seconds of audio transcribed per video")
    stt_timeout_seconds: int = Field(default=300, ge=10, le=1800, description="Hard cap for a single transcription run")

    @model_validator(mode="before")
    @classmethod
    def blank_optionals_to_none(cls, data):
        if isinstance(data, dict):
            for key in (
                "allowed_guild_id",
                "command_guild_id",
                "automation_reminder_channel_id",
                "content_dump_channel_id",
                "routine_posts_channel_id",
                "routine_release_channel_id",
                "announcements_channel_id",
                "community_event_ideas_channel_id",
                "access_reaction_channel_id",
                "access_reaction_message_id",
                "access_reaction_chaos_emoji_id",
                "access_reaction_member_role_id",
                "access_reaction_modder_role_id",
                "auto_scan_notify_channel_id",
                "focus_tree_repo",
            ):
                if data.get(key) == "":
                    data[key] = None
        return data

    chaos_redux_repo: Path = Field(default=Path("/home/klim/projects/chaos_redux"))
    focus_tree_repo: Path | None = Field(default=None, description="Optional live mod checkout used to discover focus trees; defaults to chaos_redux_repo")
    focus_tree_graphs_enabled: bool = Field(default=True, description="Render public focus-tree graphs through HOI4 Agent Tools MCP")
    focus_mcp_command: str = Field(default="npx", description="Executable used to launch the HOI4 Agent Tools MCP server")
    focus_mcp_args: str = Field(default="-y hoi4-agent-tools@1.2.0", description="Shell-style arguments for the HOI4 Agent Tools MCP command")
    focus_mcp_config_path: Path = Field(default=Path("/home/klim/.config/hoi4-agent-tools/config.json"))
    focus_mcp_workspace_id: str = Field(default="", description="Optional exact HOI4 Agent Tools workspace ID")
    focus_mcp_workspace_name: str = Field(default="chaos_redux", description="Workspace name discovered through hoi4.mods when no exact ID is configured")
    focus_mcp_timeout_seconds: int = Field(default=300, ge=30, le=900)
    focus_tree_max_graphs: int = Field(default=6, ge=1, le=20)
    focus_tree_review_scale: float = Field(default=1.0, ge=0.25, le=1.0)
    focus_tree_max_attachment_bytes: int = Field(default=8_000_000, ge=1024, le=25_000_000)
    event_chain_graphs_enabled: bool = Field(default=True, description="Attach MCP-rendered event-chain diagrams to event lookups")
    event_chain_max_depth: int = Field(default=2, ge=1, le=12)
    event_chain_max_nodes: int = Field(default=60, ge=1, le=240)
    event_chain_graphviz_command: str = Field(default="dot", min_length=1, max_length=256)
    event_chain_graphviz_dpi: int = Field(default=192, ge=96, le=300)
    scripted_gui_previews_enabled: bool = Field(default=True, description="Attach MCP-rendered offline scripted-GUI previews")
    scripted_gui_max_previews: int = Field(default=2, ge=1, le=6)
    scripted_gui_preview_width: int = Field(default=1920, ge=320, le=3840)
    scripted_gui_preview_height: int = Field(default=1080, ge=200, le=2160)
    hermes_bin: Path = Field(default=Path("/home/klim/.local/bin/hermes"))
    hermes_profile: str = Field(default="chaos_redux")
    hermes_timeout_seconds: int = Field(default=900, ge=30, le=1800)
    admin_ask_timeout_seconds: int = Field(default=0, ge=0, le=86400, description="Max seconds for /admin ask Hermes runs; 0 disables the subprocess timeout")
    admin_ask_memory_turns: int = Field(default=5, ge=0, le=20, description="Previous /admin ask turns to inject for owner follow-up context; 0 disables follow-up memory")
    admin_ask_memory_keep_last: int = Field(default=20, ge=1, le=100, description="Stored /admin ask turns to retain per owner/channel/thread")
    reply_context_turns: int = Field(default=6, ge=0, le=20, description="Previous model-backed ChaosX reply-chain turns to inject when a user replies to a stored bot answer; 0 disables reply-chain context")
    reply_memory_keep_last: int = Field(default=0, ge=0, le=10000, description="Stored model-backed message asks to retain per guild/channel; 0 keeps all stored asks")
    ask_model: str = Field(default="deepseek-flash", description="Model override for broad ask commands")
    ask_provider: str = Field(default="deepseek", description="Provider override for broad ask commands")
    ask_reasoning_effort: str = Field(default="low", description="Reasoning effort for broad ask commands")
    operator_model: str = Field(default="deepseek-flash", description="Model override for protected autonomous server operations")
    operator_provider: str = Field(default="deepseek", description="Provider override for protected autonomous server operations")
    operator_reasoning_effort: str = Field(default="high", description="Reasoning effort for protected autonomous server operations")
    model_pricing: dict[str, dict[str, float]] = Field(
        default_factory=lambda: dict(DEFAULT_PRICING),
        description="Per-million-token USD price table: model -> {input, output}. JSON string via CHAOSX_MODEL_PRICING.",
    )
    cost_usage_path: Path = Field(
        default=Path("./data/cost_usage.json"),
        description="JSON file accumulating per-call token usage + estimated cost for self-awareness lookups.",
    )
    webhook_host: str = Field(default="127.0.0.1")
    webhook_port: int = Field(default=8787, ge=1, le=65535)
    github_webhook_secret: str = Field(default="", repr=False)
    webhook_public_base_url: str = Field(default="")
    db_path: Path = Field(default=Path("./chaosx.db"))
    github_repo: str = Field(default="klimPaskov/Chaos-Redux", description="GitHub repo for public /issue creation")
    obsidian_vault_path: Path = Field(default=Path("/mnt/c/Users/klimp/Documents/Chaos Redux Vault"), description="Chaos Redux Obsidian/LLM wiki vault path")
    qoder_repowiki_path: Path | None = Field(default=None, description="Qoder repo-knowledge store root (e.g. .../knowledges/chaos_redux/master__en-US) indexed as an extra knowledge root")
    community_notes_enabled: bool = Field(default=True, description="Write approved public suggestions/event ideas to the Chaos Redux vault")
    community_event_specs_folder: str = Field(default="Events/Event Specs", description="Vault-relative folder for approved community event idea specs")
    community_suggestions_folder: str = Field(default="Planning/Community Suggestions", description="Vault-relative folder for approved community suggestion notes")
    community_event_ideas_channel_id: Optional[int] = Field(default=1395464994639839356, description="Discord forum/text channel for approved /event-idea posts; blank disables auto-posting")
    automation_reminder_channel_id: Optional[int] = Field(default=1395464062367698977, description="Discord channel for automation reminders/digests")
    rules_channel_id: Optional[int] = Field(default=1395464062367698974, description="Discord #rules channel whose announcements the bot learns for rule questions and soft warnings")
    content_dump_channel_id: Optional[int] = Field(default=1516054706286235768, description="Discord channel for weekly image-led content dumps")
    routine_posts_channel_id: Optional[int] = Field(default=1516054706286235768, description="Discord channel for autonomous routine posts (weekly dev digest)")
    routine_release_channel_id: Optional[int] = Field(default=None, description="Discord channel for autonomous release announcements; blank reuses routine_posts_channel_id")
    announcements_channel_id: Optional[int] = Field(default=1395467364916662392, description="Discord announcements channel where owner-requested announcements are posted")
    routine_posts_enabled: bool = Field(default=True, description="Master switch for autonomous routine posts (weekly dev digest + release announcements)")
    dev_digest_weekday: int = Field(default=0, ge=0, le=6, description="Weekday for the weekly dev digest post (0=Monday)")
    dev_digest_hour_utc: int = Field(default=12, ge=0, le=23, description="UTC hour for the weekly dev digest post")
    intel_digest_weekday: int = Field(default=6, ge=0, le=6, description="Weekday for the private server-intel digest DM (0=Monday)")
    intel_digest_hour_utc: int = Field(default=18, ge=0, le=23, description="UTC hour for the private server-intel digest DM")
    playtest_reminder_lead_minutes: int = Field(default=30, ge=5, le=240, description="How long before a playtest the reminder post goes out")
    playtest_result_grace_minutes: int = Field(default=10, ge=0, le=240, description="How long after a playtest window to ask for results")
    activity_rollup_tick_seconds: int = Field(
        default=600, ge=120, le=3600,
        description="How often the chaos-tier activity rollup folds new archive messages in",
    )
    tier_roles_enabled: bool = Field(
        default=True,
        description="Give each member a coloured chaos-tier role (the mod's tier colours)",
    )
    idle_banter_excluded_ids: list[int] = Field(
        default_factory=lambda: [110546365032968192],  # Holly — never a banter target (Hoops, 2026-09-23)
        description="Members idle banter must never target (the owner is always excluded in code as well)",
    )
    idle_banter_enabled: bool = Field(
        default=False,
        description="Idle banter: bot may open a conversation in the banter channel after real silence. Off by default (Hoops 2026-09-23).",
    )
    idle_banter_channel_id: int = Field(
        default=1395459672055480344, description="#jazz-and-conversation — the only channel idle banter may post in"
    )
    idle_banter_shadow: bool = Field(
        default=True,
        description="Idle banter shadow mode: log what it would post without posting anything",
    )
    idle_banter_silence_minutes: int = Field(default=45, ge=15, le=360, description="Required silence before an idle banter post")
    idle_banter_max_per_day: int = Field(default=3, ge=1, le=12, description="Daily cap on idle banter posts")
    idle_banter_min_gap_minutes: int = Field(default=180, ge=30, le=1440, description="Minimum gap between idle banter posts")
    routine_posts_tick_seconds: int = Field(default=600, ge=60, le=3600, description="How often the bot checks whether a routine post is due")
    release_check_interval_hours: int = Field(default=6, ge=1, le=48, description="How often to check for a new mod version/GitHub release")
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
