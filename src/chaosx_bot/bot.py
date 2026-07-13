from __future__ import annotations

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
    return """## ChaosX community commands
`/help` — show this guide.
`/ask` — broad rate-limited Chaos Redux question.
`/event` — event lookup by ID/name.
`/scenario` — scenario lookup.
`/cluster` — cluster lookup.
`/mechanic` — mechanic lookup.
`/search` — search public-facing indexed info.
`/status` — known event/cluster index status.
`/testing` — testing queue search.
`/work suggestion`, `/work event-idea` — draft/check ideas without creating GitHub issues.
`/playtest queue`, `/playtest report`, `/playtest summary` — playtest info/reporting.

General questions are rate-limited; lookups are usually faster and more reliable for event, scenario, mechanic, and testing info."""


def operator_help_text(settings: Settings) -> str:
    return f"""## ChaosX protected operator commands
Models:
- Public broad ask: `{settings.ask_provider}` / `{settings.ask_model}`
- Public broad ask reasoning: `{settings.ask_reasoning_effort or 'default'}`
- Autonomous server ops: `{settings.operator_provider}` / `{settings.operator_model}`
- Autonomous server reasoning: `{settings.operator_reasoning_effort or 'default'}`

Root protected:
`/health`, `/inventory`, `/say`

Protected server/admin:
`/admin help` — this private operator guide.
`/admin ask` — protected project/server request through the configured ask model.
`/server ask` — autonomous server-management request using the smarter operator model.
`/server role-audit`, `/server scan-behaviour`, `/server member-info`, `/server add-role`, `/server remove-role`, `/server timeout`
`/admin health`, `/admin sync`, `/admin reindex`, `/admin automation`, `/admin config`, `/admin permissions-audit`, `/admin jobs`, `/admin rollback`

Protected project ops:
`/work issue-draft`, `/work handoff`, `/work changelog`, `/work release-draft`
`/playtest schedule`, `/playtest cancel`
`/hermes route`, `/hermes task`, `/hermes status`, `/hermes cancel`, `/hermes audit`, `/hermes review-pr`

Important: `/server ask` can reason autonomously, but Discord/GitHub side effects still depend on bot permissions, role hierarchy, configured secrets, and approval gates."""


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
        print(f"ChaosX logged in as {self.user} owner_id={self.settings.owner_id}")

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
    prompt = (
        build_owner_prompt(owner_request=request, guild_name=guild_name, channel_name=channel_name)
        if owner_only
        else build_public_prompt(user_request=request, guild_name=guild_name, channel_name=channel_name)
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

    @bot.tree.command(name="health", description="Protected ChaosX runtime health check.")
    @app_commands.default_permissions(administrator=True)
    async def health(interaction: discord.Interaction) -> None:
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
        await bot.store.audit(actor_id=interaction.user.id, guild_id=interaction.guild_id, channel_id=interaction.channel_id, command="health", summary="health check")
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=safe_allowed_mentions())

    @bot.tree.command(name="inventory", description="Read-only inventory of the current guild.")
    @app_commands.default_permissions(administrator=True)
    async def inventory(interaction: discord.Interaction) -> None:
        if not await owner_gate(interaction, settings):
            return
        if not interaction.guild:
            await interaction.response.send_message("Run this inside a guild.", ephemeral=True)
            return
        guild = interaction.guild
        lines = [
            f"Guild: {guild.name} (`{guild.id}`)",
            f"Owner ID: `{guild.owner_id}`",
            f"Channels: `{len(guild.channels)}`",
            f"Roles: `{len(guild.roles)}`",
            "",
            "Channels:",
        ]
        for ch in sorted(guild.channels, key=lambda c: (str(c.type), c.position, c.id)):
            lines.append(f"- {ch.name} `{ch.id}` type={ch.type} position={ch.position}")
        lines.append("\nRoles:")
        for role in sorted(guild.roles, key=lambda r: r.position, reverse=True):
            perms = role.permissions
            flags = []
            for attr in ("administrator", "manage_guild", "manage_channels", "manage_roles", "manage_webhooks"):
                if getattr(perms, attr):
                    flags.append(attr)
            lines.append(f"- {role.name} `{role.id}` position={role.position}" + (f" perms={','.join(flags)}" if flags else ""))
        await bot.store.audit(actor_id=interaction.user.id, guild_id=guild.id, channel_id=interaction.channel_id, command="inventory", summary="read-only guild inventory")
        await interaction.response.send_message("Inventory generated privately.", ephemeral=True)
        for part in _chunk("\n".join(lines)):
            await interaction.followup.send(f"```text\n{part}\n```", ephemeral=True, allowed_mentions=safe_allowed_mentions())

    @bot.tree.command(name="say", description="Protected: post an exact message to the current channel without mention parsing.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(message="Message to post. Mentions are not parsed.")
    async def say(interaction: discord.Interaction, message: str) -> None:
        if not await owner_gate(interaction, settings):
            return
        await bot.store.audit(actor_id=interaction.user.id, guild_id=interaction.guild_id, channel_id=interaction.channel_id, command="say", summary=message)
        await interaction.response.send_message("Posting message with mentions disabled.", ephemeral=True)
        await interaction.channel.send(message, allowed_mentions=safe_allowed_mentions())  # type: ignore[union-attr]

    work = app_commands.Group(name="work", description="Chaos Redux work-item drafting commands")
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

    @bot.tree.command(name="scenario", description="Look up a scenario by ID or name.")
    async def chaosx_scenario(interaction: discord.Interaction, scenario: str, view: str = "overview") -> None:
        await send_scripted_response(bot, interaction, command_name="chaosx scenario", summary=scenario, render=lambda: bot.knowledge.event(scenario, view), owner_render=lambda: bot.knowledge.event(scenario, view, show_evidence=True))

    @bot.tree.command(name="cluster", description="Look up an event cluster.")
    async def chaosx_cluster(interaction: discord.Interaction, cluster: str) -> None:
        await send_scripted_response(bot, interaction, command_name="chaosx cluster", summary=cluster, render=lambda: bot.knowledge.cluster(cluster), owner_render=lambda: bot.knowledge.cluster(cluster, show_evidence=True))

    @bot.tree.command(name="mechanic", description="Explain a Chaos Redux mechanic.")
    async def chaosx_mechanic(interaction: discord.Interaction, mechanic: str) -> None:
        await send_scripted_response(bot, interaction, command_name="chaosx mechanic", summary=mechanic, render=lambda: bot.knowledge.search(mechanic, scope="all", limit=6), owner_render=lambda: bot.knowledge.search(mechanic, scope="all", limit=6, show_evidence=True))

    @bot.tree.command(name="search", description="Search public-facing Chaos Redux info.")
    async def chaosx_search(interaction: discord.Interaction, query: str, scope: str = "all") -> None:
        await send_scripted_response(bot, interaction, command_name="chaosx search", summary=query, render=lambda: bot.knowledge.search(query, scope=scope, limit=8), owner_render=lambda: bot.knowledge.search(query, scope=scope, limit=8, show_evidence=True))

    @bot.tree.command(name="status", description="Show known event/cluster index status.")
    async def chaosx_status(interaction: discord.Interaction, entity: str = "global", surface: str = "all") -> None:
        await send_scripted_response(bot, interaction, command_name="chaosx status", summary=entity, render=bot.knowledge.status, owner_render=bot.knowledge.status)

    @bot.tree.command(name="testing", description="Show prioritized testing queue.")
    async def chaosx_testing(interaction: discord.Interaction, kind: str = "all", limit: int = 10) -> None:
        await send_scripted_response(bot, interaction, command_name="chaosx testing", summary=kind, render=lambda: bot.knowledge.search('Needs Testing', scope='catalog', limit=limit), owner_render=lambda: bot.knowledge.search('Needs Testing', scope='catalog', limit=limit, show_evidence=True))


    @work.command(name="issue-draft", description="Draft a GitHub issue from text/message context.")
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

    @work.command(name="suggestion", description="Structure a suggestion and check duplicates.")
    async def work_suggestion(interaction: discord.Interaction, suggestion: str) -> None:
        await run_hermes_command(bot, interaction, f"/work suggestion suggestion={suggestion!r}. Structure and duplicate-check; do not promote to accepted design.", command_name="work suggestion")

    @work.command(name="event-idea", description="Check an event idea against assigned/unassigned catalogs.")
    async def work_event_idea(interaction: discord.Interaction, idea: str) -> None:
        await run_hermes_command(bot, interaction, f"/work event-idea idea={idea!r}. Search assigned events and unassigned ideas; never allocate ID.", command_name="work event-idea")

    @work.command(name="handoff", description="Create a Codex/Hermes handoff summary.")
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
        await interaction.response.send_message(operator_help_text(settings), ephemeral=True, allowed_mentions=safe_allowed_mentions())

    @admin.command(name="ask", description="Protected project/server request through Hermes.")
    async def admin_ask(interaction: discord.Interaction, request: str) -> None:
        await run_owner_hermes(bot, interaction, request, command_name="admin ask", use_ask_model=True)

    @admin.command(name="health", description="Admin health check.")
    async def admin_health(interaction: discord.Interaction) -> None:
        await health(interaction)

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
