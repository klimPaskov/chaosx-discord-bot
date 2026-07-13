from __future__ import annotations

import asyncio
import hashlib
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord import app_commands

from .auth import owner_deny_reason, public_deny_reason, safe_allowed_mentions
from .config import Settings
from .hermes_bridge import build_owner_prompt, build_public_prompt, run_hermes
from .knowledge import Knowledge
from .rate_limit import FixedWindowRateLimiter
from .storage import Store
from .webhook_server import GitHubWebhookServer

BOT_DESCRIPTION = "Chaos Redux community knowledge bot"
PUBLIC_ASK_REDIRECT = "I can only answer Chaos Redux questions. Try asking about events, scenarios, mechanics, testing, or mod info."
PUBLIC_ASK_DOMAIN_TERMS = {
    "chaos redux", "chaosx", "hoi4", "hearts of iron", "mod", "event", "scenario", "cluster", "mechanic",
    "testing", "playtest", "bug", "balance", "focus", "country", "lore", "zombie", "infection", "outbreak",
    "biowarfare", "chemical", "nuclear", "super event", "evolution", "catalog", "redux",
}
PUBLIC_ASK_BLOCK_TERMS = {
    "ignore previous", "ignore all previous", "system prompt", "developer message", "hidden instruction",
    "original instruction", "internal instruction", "jailbreak", "godmode", "dan mode", "you are now", "act as",
    "sudo", "admin mode", "reveal prompt", "print prompt", "show prompt", "secret", "token", "password",
    "credential", "delete server", "nuke", "hack", "malware", "phishing", "exploit", "bypass", "mass ping",
    "@everyone", "@here", "ban everyone", "delete channel", "delete role", "manage server", "moderation",
}
PUBLIC_ASK_OFFTOPIC_TERMS = {
    "recipe", "ingredients", "measurements", "exact measurements", "steps for", "instructions for",
    "cooking", "baking", "capital of", "haiku", "poem", "song", "essay", "homework", "unrelated test phrase",
    "medical advice", "legal advice", "financial advice", "relationship advice",
}
PUBLIC_ASK_INJECTION_PATTERNS = {
    "answer this", "answer only", "reply with exactly", "respond with exactly", "decode and answer",
    "translate this", "continue the dialogue", "include real", "for authenticity", "formatting test",
    "not an instruction", "sample user content", "fictional dialogue", "lore-writing exercise",
}
PUBLIC_OUTPUT_FORBIDDEN_TERMS = {
    "safe server moderation", "channel organization", "reporting abuse",
    "ingredients:", "method:", "recipe", "baking steps", "cooking steps",
}
PUBLIC_ASK_SOURCE_REQUEST_TERMS = {
    "path", "paths", "file", "files", "source", "sources", "repo", "repository", "code", "implementation",
    "where is", "where are", "stored", "located", "spec", "specs", "documentation", "docs",
}
ISSUE_TYPES = {"bug", "crash", "enhancement", "balance", "content", "general"}
ISSUE_TYPES_REQUIRING_LOG = {"bug", "crash"}


def _guild_channel(interaction: discord.Interaction) -> tuple[str | None, str | None]:
    guild_name = interaction.guild.name if interaction.guild else None
    channel = interaction.channel
    channel_name = getattr(channel, "name", None)
    return guild_name, channel_name


def _chunk(text: str, limit: int = 1900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        cut = text.rfind("\n", 0, limit)
        if cut < 200:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    return chunks


def _format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{sec}s"


def public_ask_rejection_reason(request: str) -> str | None:
    text = request.casefold()
    if any(term in text for term in PUBLIC_ASK_BLOCK_TERMS):
        return PUBLIC_ASK_REDIRECT
    if any(term in text for term in PUBLIC_ASK_OFFTOPIC_TERMS):
        return PUBLIC_ASK_REDIRECT
    if any(term in text for term in PUBLIC_ASK_INJECTION_PATTERNS):
        return PUBLIC_ASK_REDIRECT
    if not any(term in text for term in PUBLIC_ASK_DOMAIN_TERMS):
        return PUBLIC_ASK_REDIRECT
    return None


def sanitize_public_ask_output(output: str) -> str:
    text = output.casefold()
    if any(term in text for term in PUBLIC_OUTPUT_FORBIDDEN_TERMS):
        return PUBLIC_ASK_REDIRECT
    return output


def public_ask_wants_sources(request: str) -> bool:
    text = request.casefold()
    return any(term in text for term in PUBLIC_ASK_SOURCE_REQUEST_TERMS)


def validate_issue_report(*, issue_type: str, title: str, description: str, steps: str = "", expected: str = "", actual: str = "", error_log_lines: str = "") -> str | None:
    kind = issue_type.casefold().strip()
    if kind not in ISSUE_TYPES:
        return f"Unsupported issue type `{issue_type}`. Use one of: {', '.join(sorted(ISSUE_TYPES))}."
    if len(title.strip()) < 8:
        return "Please use a clearer title, at least 8 characters."
    if len(description.strip()) < 20:
        return "Please include a fuller description of what happened or what should change."
    if kind in ISSUE_TYPES_REQUIRING_LOG:
        if len(error_log_lines.strip()) < 20:
            return "Bug/crash reports need the relevant `error.log` lines pasted into `error_log_lines`."
        if len((steps or "").strip()) < 10:
            return "Bug/crash reports need reproduction steps in `steps`."
        if len((actual or "").strip()) < 10:
            return "Bug/crash reports need `actual` behavior."
    return None


def format_github_issue_body(*, issue_type: str, title: str, description: str, steps: str = "", expected: str = "", actual: str = "", error_log_lines: str = "", reporter: str = "", source: str = "Discord /issue") -> str:
    kind = issue_type.casefold().strip()
    sections = [
        f"## Type\n{kind}",
        f"## Summary\n{description.strip()}",
    ]
    if steps.strip():
        sections.append(f"## Reproduction steps\n{steps.strip()}")
    if expected.strip():
        sections.append(f"## Expected behavior\n{expected.strip()}")
    if actual.strip():
        sections.append(f"## Actual behavior\n{actual.strip()}")
    if error_log_lines.strip():
        sections.append(f"## Relevant error.log lines\n```text\n{error_log_lines.strip()[:3500]}\n```")
    sections.append(f"## Reporter / source\n- Reporter: {reporter or 'Discord user'}\n- Source: {source}\n- Created by ChaosX after validating required fields.")
    return "\n\n".join(sections)


async def create_github_issue(repo: str, *, title: str, body: str) -> tuple[bool, str]:
    proc = await asyncio.create_subprocess_exec(
        "gh", "issue", "create", "--repo", repo, "--title", title, "--body", body,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode == 0:
        return True, out or "GitHub issue created."
    return False, (err or out or f"gh issue create failed with exit code {proc.returncode}")[:1800]


def _can_manage_role(guild: discord.Guild, actor: discord.Member, bot_member: discord.Member, role: discord.Role) -> tuple[bool, str]:
    if role.is_default():
        return False, "Cannot manage the @everyone role."
    if role >= bot_member.top_role:
        return False, "ChaosX bot role is not above the target role."
    if guild.owner_id != actor.id and role >= actor.top_role:
        return False, "Your top role is not above the target role."
    return True, "ok"


def _dangerous_role_flags(role: discord.Role) -> list[str]:
    perms = role.permissions
    flags = []
    for attr in ("administrator", "manage_guild", "manage_channels", "manage_roles", "manage_webhooks", "ban_members", "kick_members", "moderate_members", "mention_everyone"):
        if getattr(perms, attr):
            flags.append(attr)
    return flags


def _stable_id(prefix: str, *parts: object) -> str:
    raw = "|".join(str(p) for p in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}"


def community_help_text() -> str:
    return """## ChaosX community help
Use ChaosX for Chaos Redux event info, scenario info, project search, issue reports, testing notes, and cleaner idea/report drafts.

### Ask or search
- `/ask question:<text>` — uses AI to answer broader Chaos Redux questions.
- `/search query:<text>` — search events, mechanics, docs, specs, and testing info when you do not know the exact command.

### Look things up
- `/event event:<id or name>` — event catalog entry: status, type, cluster, severity, details, and evolutions.
- `/scenario scenario:<SCN id or name>` — triggerable/manual scenario entry.
- `/cluster cluster:<id or name>` — event cluster summary with member event names.
- `/status` — project catalog totals and event breakdowns.
- `/testing query:<text>` — find testing notes or queues matching a topic.

### Report or draft feedback
- `/issue` — create a formatted GitHub issue after ChaosX validates the report fields. Bugs/crashes require relevant `error.log` lines.
- `/suggestion suggestion:<idea>` — uses AI to turn a rough suggestion into a clearer review note.
- `/event-idea idea:<idea>` — uses AI to format an event idea with a name, ID placeholder, type, baseline description, evolutions, and scenario hooks.

### Playtest notes
- `/playtest queue` — use before testing to see what needs attention.
- `/playtest report` — use after testing something to record what happened, what broke, or what felt off.
- `/playtest summary` — use after multiple reports to recap the current findings.

Tip: use `/search` for mechanics, event systems, and general project lookup."""


def operator_help_text(settings: Settings) -> str:
    return f"""## ChaosX admin help
Use this when you want private controls. Regular users should mostly use `/help`, `/ask`, and lookup commands.

### Start here
- `/admin health` — is the bot online, which repo/profile is it using, and what guilds can it see.
- `/admin config` — show safe config: model, rate limits, guild IDs, webhook on/off. Secrets are not printed.
- `/admin sync` — light index sync/maintenance request. Use after docs/catalogs changed.
- `/admin reindex` — rebuild local Chaos Redux search/catalog index. Use if lookups look stale or broken.
- `/admin permissions-audit` — ask for a permissions/security check. Use after role/bot permission changes.

### Ask models
- Public `/ask`: `{settings.ask_provider}` / `{settings.ask_model}` reasoning `{settings.ask_reasoning_effort or 'default'}`. No file/Discord actions.
- `/admin ask request:<text>` — private project/operator ask. Use for repo/project operations questions, not public chat.
- `/server ask request:<text>` — smarter server-management ask: `{settings.operator_provider}` / `{settings.operator_model}` reasoning `{settings.operator_reasoning_effort or 'default'}`. Use for Discord-server tasks. It should stop before destructive/broad actions unless explicitly approved.

### Server tools
- `/server role-audit` — list elevated roles and hierarchy risks.
- `/server scan-behaviour` — scan recent visible messages for obvious abuse/spam signals.
- `/server member-info user:<user>` — private moderation context for one member.
- `/server add-role` / `/server remove-role` — role changes if Discord permissions and hierarchy allow it.
- `/server timeout` — timeout a member if permissions allow it.

### Project/work tools
- Public `/issue` — validates a report and creates a GitHub issue in `{settings.github_repo}`. Bugs/crashes require relevant `error.log` lines.
- Public `/suggestion` / `/event-idea` — AI-assisted community suggestion cleanup and structured event-idea formatting.
- `/work issue-draft` — private draft only; no GitHub issue is filed.
- `/work handoff` — make a protected Codex/Hermes handoff prompt.
- `/work changelog` — draft player-facing changelog text.
- `/work release-draft` — draft announcement/release notes; does not publish.
- `/playtest schedule` / `/playtest cancel` — manage playtest records.

### Hermes tools
- `/hermes route` — choose the right agent/skill route for a task.
- `/hermes task` — create an agent-task preview.
- `/hermes status` / `/hermes cancel` — inspect or cancel agent work.
- `/hermes audit` — route a review/audit.
- `/hermes review-pr` — review a PR; cannot approve or merge.

### Automation
- `/admin automation action:list` — list automations.
- `/admin automation action:enable name:<name>` or `disable` — toggle a named automation.
- `/admin jobs` — list/retry tracked jobs.
- `/admin rollback` — prepare rollback instructions; does not perform destructive rollback by itself.
"""


class ChaosXBot(discord.Client):
    def __init__(self, settings: Settings):
        intents = discord.Intents.default()
        # No Message Content intent by default; ChaosX is interaction-first.
        super().__init__(intents=intents, allowed_mentions=safe_allowed_mentions())
        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        self.store = Store(settings.db_path)
        self.rate_limiter = FixedWindowRateLimiter()
        self.knowledge = Knowledge(settings.chaos_redux_repo, settings.db_path)
        self.webhook_server = GitHubWebhookServer(
            store=self.store,
            secret=settings.github_webhook_secret,
            host=settings.webhook_host,
            port=settings.webhook_port,
        )

    async def setup_hook(self) -> None:
        await self.store.init()
        await self.webhook_server.start()
        await self.update_application_description()
        register_commands(self)
        if self.settings.command_guild_id:
            guild = discord.Object(id=self.settings.command_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            # Avoid duplicate slash commands: during initial setup we briefly
            # synced globals, so clear global commands once guild-scoped commands
            # are registered. ChaosX is intended to live only in the configured guild.
            self.tree.clear_commands(guild=None)
            await self.tree.sync(guild=None)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="Chaos Redux ops"),
            status=discord.Status.online,
        )
        await self.leave_unauthorized_guilds()
        print(f"ChaosX logged in as {self.user} owner_id={self.settings.owner_id}")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        allowed = self.settings.allowed_guild_id or self.settings.command_guild_id
        if allowed and guild.id != allowed:
            print(f"ChaosX leaving unauthorized guild {guild.id} ({guild.name})")
            await guild.leave()

    async def leave_unauthorized_guilds(self) -> None:
        allowed = self.settings.allowed_guild_id or self.settings.command_guild_id
        if not allowed:
            return
        for guild in list(self.guilds):
            if guild.id != allowed:
                print(f"ChaosX leaving unauthorized guild {guild.id} ({guild.name})")
                await guild.leave()

    async def close(self) -> None:
        await self.webhook_server.stop()
        await super().close()

    async def update_application_description(self) -> None:
        description = self.settings.application_description.strip()
        if not description:
            return
        headers = {"Authorization": f"Bot {self.settings.discord_token}", "Content-Type": "application/json"}
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.patch("https://discord.com/api/v10/applications/@me", json={"description": description}) as response:
                    if response.status >= 400:
                        body = await response.text()
                        print(f"ChaosX application description update failed: HTTP {response.status} {body[:200]}")
        except Exception as exc:
            print(f"ChaosX application description update failed: {type(exc).__name__}: {exc}")

async def owner_gate(interaction: discord.Interaction, settings: Settings) -> bool:
    reason = owner_deny_reason(
        interaction.user.id,
        settings.owner_id,
        interaction.guild_id,
        settings.allowed_guild_id,
    )
    if reason:
        if interaction.response.is_done():
            await interaction.followup.send(reason, ephemeral=True, allowed_mentions=safe_allowed_mentions())
        else:
            await interaction.response.send_message(reason, ephemeral=True, allowed_mentions=safe_allowed_mentions())
        return False
    return True


async def public_gate(interaction: discord.Interaction, settings: Settings) -> bool:
    reason = public_deny_reason(interaction.guild_id, settings.allowed_guild_id)
    if reason:
        if interaction.response.is_done():
            await interaction.followup.send(reason, ephemeral=True, allowed_mentions=safe_allowed_mentions())
        else:
            await interaction.response.send_message(reason, ephemeral=True, allowed_mentions=safe_allowed_mentions())
        return False
    return True


async def send_scripted_response(
    bot: ChaosXBot,
    interaction: discord.Interaction,
    *,
    command_name: str,
    summary: str,
    render,
    owner_render=None,
    public: bool = True,
) -> None:
    if not await public_gate(interaction, bot.settings):
        return
    limit = bot.settings.public_scripted_limit_per_hour
    rate = bot.rate_limiter.check(bucket="scripted", user_id=interaction.user.id, limit=limit, window_seconds=3600)
    if not rate.allowed:
        minutes = max(1, rate.retry_after_seconds // 60)
        await interaction.response.send_message(f"Rate limit hit for ChaosX scripted commands. Try again in about {minutes} minute(s).", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=not public, thinking=True)
    try:
        output = render()
    except Exception as exc:
        output = f"ChaosX scripted command failed: `{type(exc).__name__}: {exc}`"
    await bot.store.audit(actor_id=interaction.user.id, guild_id=interaction.guild_id, channel_id=interaction.channel_id, command=command_name, summary=summary)
    for part in _chunk(output):
        await interaction.followup.send(part, ephemeral=not public, allowed_mentions=safe_allowed_mentions())
    if owner_render and public and interaction.user.id == bot.settings.owner_id:
        try:
            owner_output = owner_render()
        except Exception as exc:
            owner_output = f"Private details failed: `{type(exc).__name__}: {exc}`"
        if owner_output and owner_output != output:
            for part in _chunk("## Private details\n" + owner_output):
                await interaction.followup.send(part, ephemeral=True, allowed_mentions=safe_allowed_mentions())


async def run_hermes_command(
    bot: ChaosXBot,
    interaction: discord.Interaction,
    request: str,
    *,
    command_name: str,
    public: bool = True,
    owner_only: bool = False,
    rate_bucket: str = "scripted",
    use_ask_model: bool = False,
    use_operator_model: bool = False,
) -> None:
    rate = None
    if owner_only:
        if not await owner_gate(interaction, bot.settings):
            return
    elif not await public_gate(interaction, bot.settings):
        return

    if not owner_only:
        if rate_bucket == "ask":
            rejection = public_ask_rejection_reason(request)
            if rejection:
                await interaction.response.send_message(rejection, ephemeral=not public, allowed_mentions=safe_allowed_mentions())
                await bot.store.audit(actor_id=interaction.user.id, guild_id=interaction.guild_id, channel_id=interaction.channel_id, command=command_name, summary="public ask rejected")
                return
        max_chars = bot.settings.public_prompt_max_chars
        if len(request) > max_chars:
            await interaction.response.send_message(
                f"Request is too long for public ChaosX commands. Limit: {max_chars} characters.",
                ephemeral=True,
                allowed_mentions=safe_allowed_mentions(),
            )
            return
        if rate_bucket == "ask":
            limit = bot.settings.public_ask_limit_per_hour
        else:
            limit = bot.settings.public_scripted_limit_per_hour
        if limit <= 0:
            await interaction.response.send_message("This public command is currently disabled.", ephemeral=True)
            return
        rate = bot.rate_limiter.check(bucket=rate_bucket, user_id=interaction.user.id, limit=limit, window_seconds=3600)
        if not rate.allowed:
            minutes = max(1, rate.retry_after_seconds // 60)
            await interaction.response.send_message(
                f"Rate limit hit for ChaosX `{rate_bucket}` commands. Try again in about {minutes} minute(s).",
                ephemeral=True,
                allowed_mentions=safe_allowed_mentions(),
            )
            return

    await interaction.response.defer(ephemeral=not public, thinking=True)
    guild_name, channel_name = _guild_channel(interaction)
    source_paths_allowed = public_ask_wants_sources(request) if not owner_only and rate_bucket == "ask" else False
    prompt = (
        build_owner_prompt(owner_request=request, guild_name=guild_name, channel_name=channel_name)
        if owner_only
        else build_public_prompt(
            user_request=request,
            guild_name=guild_name,
            channel_name=channel_name,
            reference_context=bot.knowledge.public_ask_context(request, include_sources=source_paths_allowed) if rate_bucket == "ask" else "",
            source_paths_allowed=source_paths_allowed,
        )
    )
    model = provider = reasoning_effort = toolsets = None
    if use_operator_model:
        model, provider = bot.settings.operator_model, bot.settings.operator_provider
        reasoning_effort = bot.settings.operator_reasoning_effort
    elif use_ask_model:
        model, provider = bot.settings.ask_model, bot.settings.ask_provider
        reasoning_effort = bot.settings.ask_reasoning_effort
    if not owner_only:
        toolsets = "safe"
        ignore_rules = True
    else:
        ignore_rules = False
    result = await run_hermes(
        hermes_bin=bot.settings.hermes_bin,
        profile=bot.settings.hermes_profile,
        repo=bot.settings.chaos_redux_repo,
        prompt=prompt,
        timeout_seconds=bot.settings.hermes_timeout_seconds,
        model=model,
        provider=provider,
        reasoning_effort=reasoning_effort,
        toolsets=toolsets,
        ignore_rules=ignore_rules,
    )
    output = result.stdout.strip() or result.stderr.strip() or "No output."
    if not owner_only and rate_bucket == "ask":
        output = sanitize_public_ask_output(output)
        if rate:
            output += f"\n\n---\nAsks left: `{rate.remaining}` · Reset in: `{_format_duration(rate.reset_after_seconds)}`"
    status = "ok" if result.ok else "failed"
    await bot.store.record_hermes_run(
        actor_id=interaction.user.id,
        guild_id=interaction.guild_id,
        channel_id=interaction.channel_id,
        prompt_hash=result.prompt_hash,
        status=status,
        output_excerpt=output,
    )
    await bot.store.audit(
        actor_id=interaction.user.id,
        guild_id=interaction.guild_id,
        channel_id=interaction.channel_id,
        command=command_name,
        summary=request,
    )
    header = "ChaosX answer" if public else f"ChaosX `{status}` hash `{result.prompt_hash[:12]}`"
    for i, part in enumerate(_chunk(output)):
        await interaction.followup.send(
            (header + "\n" if i == 0 else "") + part,
            ephemeral=not public,
            allowed_mentions=safe_allowed_mentions(),
        )


async def run_owner_hermes(
    bot: ChaosXBot,
    interaction: discord.Interaction,
    request: str,
    *,
    command_name: str,
    public: bool = False,
    use_ask_model: bool = False,
    use_operator_model: bool = False,
) -> None:
    await run_hermes_command(bot, interaction, request, command_name=command_name, public=public, owner_only=True, use_ask_model=use_ask_model, use_operator_model=use_operator_model)


def register_commands(bot: ChaosXBot) -> None:
    settings = bot.settings

    @bot.tree.command(name="help", description="Show all public ChaosX community commands.")
    async def root_help(interaction: discord.Interaction) -> None:
        if not await public_gate(interaction, settings):
            return
        await interaction.response.send_message(community_help_text(), ephemeral=False, allowed_mentions=safe_allowed_mentions())

    work = app_commands.Group(name="work", description="Protected Chaos Redux work drafts", default_permissions=discord.Permissions(administrator=True))
    playtest = app_commands.Group(name="playtest", description="Chaos Redux playtest commands")
    hermes = app_commands.Group(name="hermes", description="Hermes agent routing/task commands", default_permissions=discord.Permissions(administrator=True))
    admin = app_commands.Group(name="admin", description="ChaosX admin commands", default_permissions=discord.Permissions(administrator=True))
    server = app_commands.Group(name="server", description="Protected Discord server administration", default_permissions=discord.Permissions(administrator=True))

    @bot.tree.command(name="ask", description="Answer a Chaos Redux question.")
    async def chaosx_ask(interaction: discord.Interaction, question: str, visibility: str = "public") -> None:
        await run_hermes_command(bot, interaction, f"/ask question={question!r} visibility={visibility!r}. Answer concisely for the community; do not include internal source/debug metadata unless asked.", command_name="ask", public=visibility != "private", rate_bucket="ask", use_ask_model=True)

    @bot.tree.command(name="event", description="Look up an event by ID or name.")
    async def chaosx_event(interaction: discord.Interaction, event: str, view: str = "overview") -> None:
        await send_scripted_response(bot, interaction, command_name="chaosx event", summary=event, render=lambda: bot.knowledge.event(event, view), owner_render=lambda: bot.knowledge.event(event, view, show_evidence=True))

    @bot.tree.command(name="scenario", description="Look up a triggerable scenario by SCN ID or name.")
    async def chaosx_scenario(interaction: discord.Interaction, scenario: str, view: str = "overview") -> None:
        await send_scripted_response(bot, interaction, command_name="chaosx scenario", summary=scenario, render=lambda: bot.knowledge.scenario(scenario, view), owner_render=lambda: bot.knowledge.scenario(scenario, view, show_evidence=True))

    @bot.tree.command(name="cluster", description="Look up an event cluster.")
    async def chaosx_cluster(interaction: discord.Interaction, cluster: str) -> None:
        await send_scripted_response(bot, interaction, command_name="chaosx cluster", summary=cluster, render=lambda: bot.knowledge.cluster(cluster), owner_render=lambda: bot.knowledge.cluster(cluster, show_evidence=True))

    @bot.tree.command(name="search", description="Search public-facing Chaos Redux info.")
    async def chaosx_search(interaction: discord.Interaction, query: str, scope: str = "all") -> None:
        await send_scripted_response(bot, interaction, command_name="chaosx search", summary=query, render=lambda: bot.knowledge.search(query, scope=scope, limit=8), owner_render=lambda: bot.knowledge.search(query, scope=scope, limit=8, show_evidence=True))

    @bot.tree.command(name="status", description="Show Chaos Redux catalog totals and breakdowns.")
    async def chaosx_status(interaction: discord.Interaction, entity: str = "global", surface: str = "all") -> None:
        await send_scripted_response(bot, interaction, command_name="chaosx status", summary=entity, render=bot.knowledge.status, owner_render=bot.knowledge.status)

    @bot.tree.command(name="testing", description="Show prioritized testing queue.")
    async def chaosx_testing(interaction: discord.Interaction, kind: str = "all", limit: int = 10) -> None:
        await send_scripted_response(bot, interaction, command_name="chaosx testing", summary=kind, render=lambda: bot.knowledge.search('Needs Testing', scope='catalog', limit=limit), owner_render=lambda: bot.knowledge.search('Needs Testing', scope='catalog', limit=limit, show_evidence=True))


    @bot.tree.command(name="suggestion", description="Clean up a Chaos Redux suggestion and note likely overlap.")
    async def chaosx_suggestion(interaction: discord.Interaction, suggestion: str) -> None:
        await run_hermes_command(bot, interaction, f"/suggestion suggestion={suggestion!r}. Structure this as a concise community suggestion review note. Mention likely overlap if obvious; do not promote it to accepted design.", command_name="suggestion")

    @bot.tree.command(name="event-idea", description="Format a Chaos Redux event idea into a structured review draft.")
    async def chaosx_event_idea(interaction: discord.Interaction, idea: str) -> None:
        await run_hermes_command(
            bot,
            interaction,
            f"""/event-idea idea={idea!r}. Use AI to format this as a Chaos Redux event idea review draft, not just a duplicate check.
Include: proposed event name, ID placeholder like TBD-###, event type, baseline description, trigger/conditions if inferable, immediate effects, evolution ideas, possible world-end relationship, possible triggerable/manual scenario hooks, likely cluster/tags, testing notes, and a short overlap/gap note if relevant. Do not allocate a real ID or claim acceptance.""",
            command_name="event-idea",
        )

    @bot.tree.command(name="issue", description="Create a formatted GitHub issue after ChaosX validates the report.")
    @app_commands.choices(issue_type=[
        app_commands.Choice(name="Bug", value="bug"),
        app_commands.Choice(name="Crash", value="crash"),
        app_commands.Choice(name="Enhancement request", value="enhancement"),
        app_commands.Choice(name="Balance issue", value="balance"),
        app_commands.Choice(name="Content issue", value="content"),
        app_commands.Choice(name="General", value="general"),
    ])
    async def chaosx_issue(
        interaction: discord.Interaction,
        issue_type: app_commands.Choice[str],
        title: str,
        description: str,
        steps: str = "",
        expected: str = "",
        actual: str = "",
        error_log_lines: str = "",
    ) -> None:
        if not await public_gate(interaction, settings):
            return
        rate = bot.rate_limiter.check(bucket="issue", user_id=interaction.user.id, limit=5, window_seconds=3600)
        if not rate.allowed:
            await interaction.response.send_message(
                f"Issue-report rate limit hit. Try again in about {_format_duration(rate.retry_after_seconds)}.",
                ephemeral=True,
                allowed_mentions=safe_allowed_mentions(),
            )
            return
        kind = issue_type.value
        validation_error = validate_issue_report(
            issue_type=kind,
            title=title,
            description=description,
            steps=steps,
            expected=expected,
            actual=actual,
            error_log_lines=error_log_lines,
        )
        if validation_error:
            await interaction.response.send_message(validation_error, ephemeral=True, allowed_mentions=safe_allowed_mentions())
            return
        await interaction.response.defer(ephemeral=False, thinking=True)
        body = format_github_issue_body(
            issue_type=kind,
            title=title,
            description=description,
            steps=steps,
            expected=expected,
            actual=actual,
            error_log_lines=error_log_lines,
            reporter=str(interaction.user),
            source=f"Discord /issue in guild {interaction.guild_id}, channel {interaction.channel_id}",
        )
        issue_title = f"[{kind.title()}] {title.strip()}"
        ok, result = await create_github_issue(settings.github_repo, title=issue_title, body=body)
        await bot.store.audit(actor_id=interaction.user.id, guild_id=interaction.guild_id, channel_id=interaction.channel_id, command="issue", summary=issue_title)
        if ok:
            await interaction.followup.send(
                f"GitHub issue created: {result}\nIssue type: `{kind}` · Issue reports left: `{rate.remaining}` · Reset in: `{_format_duration(rate.reset_after_seconds)}`",
                ephemeral=False,
                allowed_mentions=safe_allowed_mentions(),
            )
        else:
            await interaction.followup.send(f"ChaosX approved the report fields, but GitHub issue creation failed:\n```text\n{result}\n```", ephemeral=True, allowed_mentions=safe_allowed_mentions())


    @work.command(name="issue-draft", description="Turn a report into a private issue-style draft for Hoops review.")
    async def work_issue_draft(interaction: discord.Interaction, summary: str, event: str = "", surface: str = "") -> None:
        if not await owner_gate(interaction, settings):
            return
        draft_id = _stable_id("issue", interaction.user.id, interaction.created_at.isoformat(), summary)
        body = (
            f"Summary: {summary}\n"
            f"Event/entity: {event or 'unknown'}\n"
            f"Surface: {surface or 'unknown'}\n\n"
            "Expected behavior:\n- TBD\n\nActual behavior:\n- TBD\n\nReproduction steps:\n1. TBD\n\nEvidence:\n- Discord draft created by ChaosX; no GitHub issue created yet."
        )
        await bot.store.create_issue_draft(draft_id=draft_id, actor_id=interaction.user.id, guild_id=interaction.guild_id, channel_id=interaction.channel_id, summary=summary, body=body)
        await bot.store.audit(actor_id=interaction.user.id, guild_id=interaction.guild_id, channel_id=interaction.channel_id, command="work issue-draft", summary=draft_id)
        await interaction.response.send_message(f"Created issue draft `{draft_id}`. No GitHub issue was created.\n```text\n{body[:1600]}\n```", ephemeral=True, allowed_mentions=safe_allowed_mentions())

    @work.command(name="handoff", description="Create a clear implementation/review handoff draft.")
    async def work_handoff(interaction: discord.Interaction, task: str) -> None:
        await run_owner_hermes(bot, interaction, f"/work handoff task={task!r}. Include files, identifiers, validation, blockers, next owner.", command_name="work handoff")

    @work.command(name="changelog", description="Draft player-facing changelog.")
    async def work_changelog(interaction: discord.Interaction, ref_a: str, ref_b: str) -> None:
        await run_owner_hermes(bot, interaction, f"/work changelog ref_a={ref_a!r} ref_b={ref_b!r}. Draft only; avoid unsupported completion claims.", command_name="work changelog")

    @work.command(name="release-draft", description="Draft release notes/announcement preview.")
    async def work_release_draft(interaction: discord.Interaction, tag: str) -> None:
        await run_owner_hermes(bot, interaction, f"/work release-draft tag={tag!r}. Draft only; do not publish or announce.", command_name="work release-draft")

    @playtest.command(name="queue", description="Show playtest queue.")
    async def playtest_queue(interaction: discord.Interaction, kind: str = "all") -> None:
        await run_hermes_command(bot, interaction, f"/playtest queue kind={kind!r}. Keep it concise and community-facing.", command_name="playtest queue")

    @playtest.command(name="schedule", description="Prepare a playtest Scheduled Event plan.")
    async def playtest_schedule(interaction: discord.Interaction, target: str, start: str, duration: int, voice: str = "none", build: str = "") -> None:
        if not await owner_gate(interaction, settings):
            return
        playtest_id = _stable_id("playtest", target, start, duration, voice, build)
        await bot.store.create_playtest(playtest_id=playtest_id, actor_id=interaction.user.id, guild_id=interaction.guild_id, channel_id=interaction.channel_id, target=target, start_time=start, duration_minutes=duration, voice=voice, build=build)
        preview = f"Playtest draft `{playtest_id}`\nTarget: `{target}`\nStart: `{start}`\nDuration: `{duration}` minutes\nVoice: `{voice}`\nBuild: `{build or 'unspecified'}`\nNo Discord Scheduled Event was created by this draft command."
        await bot.store.audit(actor_id=interaction.user.id, guild_id=interaction.guild_id, channel_id=interaction.channel_id, command="playtest schedule", summary=playtest_id)
        await interaction.response.send_message(preview, ephemeral=True, allowed_mentions=safe_allowed_mentions())

    @playtest.command(name="report", description="Draft a structured playtest report.")
    async def playtest_report(interaction: discord.Interaction, event: str, observation: str) -> None:
        report = {"event": event, "observation": observation, "reporter_id": interaction.user.id, "created_at": datetime.now(timezone.utc).isoformat()}
        await bot.store.add_playtest_report(playtest_id=event, report=report)
        await send_scripted_response(bot, interaction, command_name="playtest report", summary=event, render=lambda: f"Recorded playtest observation for `{event}` if that draft exists.\n```text\n{observation[:1500]}\n```")

    @playtest.command(name="summary", description="Summarize a playtest.")
    async def playtest_summary(interaction: discord.Interaction, event: str = "latest") -> None:
        rows = await bot.store.list_playtests(limit=10)
        lines = ["## Playtest records"]
        for playtest_id, target, start_time, duration_minutes, voice, build, status in rows:
            lines.append(f"- `{playtest_id}` target=`{target}` start=`{start_time}` duration=`{duration_minutes}` voice=`{voice}` build=`{build}` status=`{status}`")
        await send_scripted_response(bot, interaction, command_name="playtest summary", summary=event, render=lambda: "\n".join(lines))

    @playtest.command(name="cancel", description="Prepare/cancel playtest reminders/event if approved.")
    async def playtest_cancel(interaction: discord.Interaction, event: str) -> None:
        await run_owner_hermes(bot, interaction, f"/playtest cancel event={event!r}. Preserve audit record; cancel only if explicit approval and permissions exist.", command_name="playtest cancel")

    @hermes.command(name="route", description="Recommend project skill/subagent route.")
    async def hermes_route(interaction: discord.Interaction, goal: str, event: str = "", surface: str = "") -> None:
        await run_owner_hermes(bot, interaction, f"/hermes route goal={goal!r} event={event!r} surface={surface!r}. Recommendation only; do not start work.", command_name="hermes route")

    @hermes.command(name="task", description="Create an agent task preview.")
    async def hermes_task(interaction: discord.Interaction, goal: str, mode: str = "analysis", event: str = "", visibility: str = "private") -> None:
        await run_owner_hermes(bot, interaction, f"/hermes task goal={goal!r} event={event!r} mode={mode!r} visibility={visibility!r}. Preview and approval gates; no draft PR unless enabled.", command_name="hermes task", public=visibility == "channel")

    @hermes.command(name="status", description="Show Hermes task status.")
    async def hermes_status(interaction: discord.Interaction, task: str) -> None:
        await run_owner_hermes(bot, interaction, f"/hermes status task={task!r}. Use completed-with-evidence wording where appropriate.", command_name="hermes status")

    @hermes.command(name="cancel", description="Cancel a queued/running task if safe.")
    async def hermes_cancel(interaction: discord.Interaction, task: str) -> None:
        await run_owner_hermes(bot, interaction, f"/hermes cancel task={task!r}. Cancel only if safe; preserve evidence.", command_name="hermes cancel")

    @hermes.command(name="audit", description="Route an audit.")
    async def hermes_audit(interaction: discord.Interaction, target: str, surface: str = "completion") -> None:
        await run_owner_hermes(bot, interaction, f"/hermes audit target={target!r} surface={surface!r}. Show proposed route/write scope before starting.", command_name="hermes audit")

    @hermes.command(name="review-pr", description="Review a pull request against project evidence.")
    async def hermes_review_pr(interaction: discord.Interaction, number: int) -> None:
        await run_owner_hermes(bot, interaction, f"/hermes review-pr number={number}. Cannot approve or merge.", command_name="hermes review-pr")

    @admin.command(name="help", description="Show protected operator command help.")
    async def admin_help(interaction: discord.Interaction) -> None:
        if not await owner_gate(interaction, settings):
            return
        await interaction.response.defer(ephemeral=True, thinking=False)
        for part in _chunk(operator_help_text(settings)):
            await interaction.followup.send(part, ephemeral=True, allowed_mentions=safe_allowed_mentions())

    @admin.command(name="ask", description="Protected project/server request through Hermes.")
    async def admin_ask(interaction: discord.Interaction, request: str) -> None:
        await run_owner_hermes(bot, interaction, request, command_name="admin ask", use_ask_model=True)

    @admin.command(name="health", description="Check ChaosX runtime health.")
    async def admin_health(interaction: discord.Interaction) -> None:
        if not await owner_gate(interaction, settings):
            return
        guilds = ", ".join(f"{g.name}({g.id})" for g in bot.guilds) or "none"
        text = (
            "ChaosX online.\n"
            f"Description: `{BOT_DESCRIPTION}`\n"
            f"Owner: `{settings.owner_id}`\n"
            f"Allowed guild: `{settings.allowed_guild_id or 'not locked'}`\n"
            f"Hermes profile: `{settings.hermes_profile}`\n"
            f"Repo: `{settings.chaos_redux_repo}`\n"
            f"Visible guilds: {guilds}"
        )
        await bot.store.audit(actor_id=interaction.user.id, guild_id=interaction.guild_id, channel_id=interaction.channel_id, command="admin health", summary="health check")
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=safe_allowed_mentions())

    @admin.command(name="sync", description="Run/plan index sync.")
    async def admin_sync(interaction: discord.Interaction, mode: str = "incremental") -> None:
        await run_owner_hermes(bot, interaction, f"/admin sync mode={mode!r}. Idempotent; report results.", command_name="admin sync")

    @admin.command(name="reindex", description="Run/plan reindex.")
    async def admin_reindex(interaction: discord.Interaction, scope: str = "all") -> None:
        await run_owner_hermes(bot, interaction, f"/admin reindex scope={scope!r}. Keep last-known-good index on failure.", command_name="admin reindex")

    @admin.command(name="automation", description="List/enable/disable automation by name.")
    async def admin_automation(interaction: discord.Interaction, action: str = "list", name: str = "") -> None:
        if not await owner_gate(interaction, settings):
            return
        action = action.lower().strip()
        if action in {"enable", "disable"} and name:
            ok = await bot.store.set_automation(name, action == "enable")
            await interaction.response.send_message((f"Automation `{name}` set to `{action}`." if ok else f"Unknown automation `{name}`."), ephemeral=True)
            return
        rows = await bot.store.list_automations()
        text = "## ChaosX automations\n" + "\n".join(f"- `{n}` enabled=`{bool(e)}` destination=`{d or 'unset'}`" for n, e, d in rows)
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=safe_allowed_mentions())

    @admin.command(name="config", description="Show/validate config with secrets redacted.")
    async def admin_config(interaction: discord.Interaction, action: str = "show") -> None:
        if not await owner_gate(interaction, settings):
            return
        text = (
            "## ChaosX config\n"
            f"- allowed_guild_id: `{settings.allowed_guild_id}`\n"
            f"- command_guild_id: `{settings.command_guild_id}`\n"
            f"- broad ask provider/model: `{settings.ask_provider}` / `{settings.ask_model}` reasoning=`{settings.ask_reasoning_effort or 'default'}`\n"
            f"- public ask limit/hour: `{settings.public_ask_limit_per_hour}`\n"
            f"- scripted limit/hour: `{settings.public_scripted_limit_per_hour}`\n"
            f"- webhook listener: `{settings.webhook_host}:{settings.webhook_port}` enabled=`{bool(settings.github_webhook_secret)}`\n"
            f"- repo: `{settings.chaos_redux_repo}`\n"
            f"- db: `{settings.db_path}`\n"
        )
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=safe_allowed_mentions())

    @admin.command(name="permissions-audit", description="Audit Discord/GitHub permissions.")
    async def admin_permissions_audit(interaction: discord.Interaction) -> None:
        await run_owner_hermes(bot, interaction, "/admin permissions audit. Identify excessive permissions and drift.", command_name="admin permissions-audit")

    @admin.command(name="jobs", description="List/retry jobs.")
    async def admin_jobs(interaction: discord.Interaction, action: str = "list", job: str = "") -> None:
        await run_owner_hermes(bot, interaction, f"/admin jobs action={action!r} job={job!r}.", command_name="admin jobs")

    @admin.command(name="rollback", description="Prepare rollback instructions for a deployment.")
    async def admin_rollback(interaction: discord.Interaction, deployment: str) -> None:
        await run_owner_hermes(bot, interaction, f"/admin rollback deployment={deployment!r}. Do not perform destructive rollback without explicit approval.", command_name="admin rollback")

    @server.command(name="ask", description="Autonomously handle a protected server-management request.")
    async def server_ask(interaction: discord.Interaction, request: str) -> None:
        prompt = (
            "Protected autonomous Discord server-management request. Use available ChaosX/Discord capabilities where safe. "
            "Perform read-only inspection or safe bounded actions directly when permissions allow. For destructive, broad, irreversible, secret-requiring, or permission-expanding actions, stop and report the exact approval/blocker. "
            "Do not use mass pings. Keep an audit-minded summary.\n\n"
            f"Request: {request}"
        )
        await run_owner_hermes(bot, interaction, prompt, command_name="server ask", use_operator_model=True)

    @server.command(name="role-audit", description="Scan roles for elevated permissions and hierarchy risks.")
    async def server_role_audit(interaction: discord.Interaction) -> None:
        if not await owner_gate(interaction, settings):
            return
        if not interaction.guild:
            await interaction.response.send_message("Run this inside the server.", ephemeral=True)
            return
        guild = interaction.guild
        bot_member = guild.me
        lines = [f"## Role audit for {guild.name}", f"Bot top role: `{bot_member.top_role.name if bot_member else 'unknown'}`", ""]
        risky = []
        for role in sorted(guild.roles, key=lambda r: r.position, reverse=True):
            flags = _dangerous_role_flags(role)
            if flags:
                risky.append(f"- `{role.name}` `{role.id}` position={role.position} flags={', '.join(flags)} members={len(role.members)}")
        lines += risky or ["No elevated permission roles found in cache."]
        await bot.store.audit(actor_id=interaction.user.id, guild_id=guild.id, channel_id=interaction.channel_id, command="server role-audit", summary="role audit")
        await interaction.response.send_message("Role audit generated privately.", ephemeral=True)
        for part in _chunk("\n".join(lines)):
            await interaction.followup.send(part, ephemeral=True, allowed_mentions=safe_allowed_mentions())

    @server.command(name="scan-behaviour", description="Scan recent visible channel messages for obvious abuse signals.")
    async def server_scan_behaviour(interaction: discord.Interaction, limit: int = 200) -> None:
        if not await owner_gate(interaction, settings):
            return
        if not interaction.guild:
            await interaction.response.send_message("Run this inside the server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        limit = max(20, min(limit, 1000))
        channel_count = message_count = mass_mentions = attachments = 0
        author_counts: Counter[int] = Counter()
        suspicious: list[str] = []
        for channel in interaction.guild.text_channels:
            perms = channel.permissions_for(interaction.guild.me)  # type: ignore[arg-type]
            if not (perms.view_channel and perms.read_message_history):
                continue
            channel_count += 1
            try:
                async for msg in channel.history(limit=max(1, limit // max(1, len(interaction.guild.text_channels)))):
                    if msg.author.bot:
                        continue
                    message_count += 1
                    author_counts[msg.author.id] += 1
                    if msg.mention_everyone:
                        mass_mentions += 1
                        suspicious.append(f"- Mass mention by `{msg.author}` in #{channel.name} at {msg.created_at.isoformat()}")
                    if len(msg.mentions) + len(msg.role_mentions) >= 8:
                        suspicious.append(f"- Mention burst by `{msg.author}` in #{channel.name}: {len(msg.mentions)} users, {len(msg.role_mentions)} roles")
                    if msg.attachments:
                        attachments += len(msg.attachments)
            except discord.Forbidden:
                continue
            except discord.HTTPException as exc:
                suspicious.append(f"- Could not scan #{channel.name}: {exc.status}")
        top = [f"- <@{uid}>: {count} visible messages" for uid, count in author_counts.most_common(10)]
        lines = [
            f"## Behaviour scan for {interaction.guild.name}",
            f"Scanned channels: `{channel_count}`",
            f"Visible non-bot messages checked: `{message_count}`",
            f"Mass-mention messages: `{mass_mentions}`",
            f"Attachments observed: `{attachments}`",
            "",
            "### Top visible posters",
            *(top or ["No messages visible."]),
            "",
            "### Signals",
            *(suspicious[:30] or ["No obvious abuse signals found in visible recent history."]),
            "",
            "Note: without Message Content intent, this scan does not inspect message text semantics.",
        ]
        await bot.store.audit(actor_id=interaction.user.id, guild_id=interaction.guild_id, channel_id=interaction.channel_id, command="server scan-behaviour", summary=f"limit={limit}")
        for part in _chunk("\n".join(lines)):
            await interaction.followup.send(part, ephemeral=True, allowed_mentions=safe_allowed_mentions())

    @server.command(name="member-info", description="Show protected member moderation context.")
    async def server_member_info(interaction: discord.Interaction, member: discord.Member) -> None:
        if not await owner_gate(interaction, settings):
            return
        roles = [r.name for r in sorted(member.roles, key=lambda r: r.position, reverse=True) if not r.is_default()]
        text = (
            f"## Member info: `{member}`\n"
            f"- ID: `{member.id}`\n"
            f"- Bot: `{member.bot}`\n"
            f"- Joined: `{member.joined_at.isoformat() if member.joined_at else 'unknown'}`\n"
            f"- Created: `{member.created_at.isoformat()}`\n"
            f"- Top role: `{member.top_role.name}`\n"
            f"- Roles: {', '.join(roles[:30]) or 'none'}"
        )
        await bot.store.audit(actor_id=interaction.user.id, guild_id=interaction.guild_id, channel_id=interaction.channel_id, command="server member-info", summary=str(member.id))
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=safe_allowed_mentions())

    @server.command(name="add-role", description="Add a role to a member if Discord permissions allow it.")
    async def server_add_role(interaction: discord.Interaction, member: discord.Member, role: discord.Role, reason: str = "ChaosX operator action") -> None:
        if not await owner_gate(interaction, settings):
            return
        assert interaction.guild is not None
        ok, why = _can_manage_role(interaction.guild, interaction.user, interaction.guild.me, role)  # type: ignore[arg-type]
        if not ok:
            await interaction.response.send_message(f"Blocked: {why}", ephemeral=True)
            return
        try:
            await member.add_roles(role, reason=reason)
            await bot.store.audit(actor_id=interaction.user.id, guild_id=interaction.guild_id, channel_id=interaction.channel_id, command="server add-role", summary=f"{member.id}->{role.id}")
            await interaction.response.send_message(f"Added `{role.name}` to `{member}`.", ephemeral=True, allowed_mentions=safe_allowed_mentions())
        except discord.Forbidden:
            await interaction.response.send_message("Blocked by Discord permissions. Reinvite/role hierarchy may need Manage Roles and a higher bot role.", ephemeral=True)

    @server.command(name="remove-role", description="Remove a role from a member if Discord permissions allow it.")
    async def server_remove_role(interaction: discord.Interaction, member: discord.Member, role: discord.Role, reason: str = "ChaosX operator action") -> None:
        if not await owner_gate(interaction, settings):
            return
        assert interaction.guild is not None
        ok, why = _can_manage_role(interaction.guild, interaction.user, interaction.guild.me, role)  # type: ignore[arg-type]
        if not ok:
            await interaction.response.send_message(f"Blocked: {why}", ephemeral=True)
            return
        try:
            await member.remove_roles(role, reason=reason)
            await bot.store.audit(actor_id=interaction.user.id, guild_id=interaction.guild_id, channel_id=interaction.channel_id, command="server remove-role", summary=f"{member.id}->{role.id}")
            await interaction.response.send_message(f"Removed `{role.name}` from `{member}`.", ephemeral=True, allowed_mentions=safe_allowed_mentions())
        except discord.Forbidden:
            await interaction.response.send_message("Blocked by Discord permissions. Reinvite/role hierarchy may need Manage Roles and a higher bot role.", ephemeral=True)

    @server.command(name="timeout", description="Timeout a member if Discord permissions allow it.")
    async def server_timeout(interaction: discord.Interaction, member: discord.Member, minutes: int, reason: str = "ChaosX operator timeout") -> None:
        if not await owner_gate(interaction, settings):
            return
        minutes = max(1, min(minutes, 40320))
        try:
            await member.timeout(datetime.now(timezone.utc) + timedelta(minutes=minutes), reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message("Blocked by Discord permissions. ChaosX needs Moderate Members and correct role hierarchy.", ephemeral=True)
            return
        await bot.store.audit(actor_id=interaction.user.id, guild_id=interaction.guild_id, channel_id=interaction.channel_id, command="server timeout", summary=f"{member.id} {minutes}m")
        await interaction.response.send_message(f"Timed out `{member}` for `{minutes}` minute(s).", ephemeral=True, allowed_mentions=safe_allowed_mentions())

    for group in (work, playtest, hermes, admin, server):
        bot.tree.add_command(group)
