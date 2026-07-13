from __future__ import annotations

import discord
from discord import app_commands

from .auth import owner_deny_reason, public_deny_reason, safe_allowed_mentions
from .config import Settings
from .hermes_bridge import build_owner_prompt, run_hermes
from .rate_limit import FixedWindowRateLimiter
from .storage import Store

BOT_DESCRIPTION = "Community Chaos Redux knowledge bot with owner-only operations"


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


class ChaosXBot(discord.Client):
    def __init__(self, settings: Settings):
        intents = discord.Intents.default()
        # No Message Content intent by default; ChaosX is interaction-first.
        super().__init__(intents=intents, allowed_mentions=safe_allowed_mentions())
        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        self.store = Store(settings.db_path)
        self.rate_limiter = FixedWindowRateLimiter()

    async def setup_hook(self) -> None:
        await self.store.init()
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


async def run_hermes_command(
    bot: ChaosXBot,
    interaction: discord.Interaction,
    request: str,
    *,
    command_name: str,
    public: bool = False,
    owner_only: bool = False,
    rate_bucket: str = "scripted",
) -> None:
    if owner_only:
        if not await owner_gate(interaction, bot.settings):
            return
    elif not await public_gate(interaction, bot.settings):
        return

    if not owner_only:
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
    prompt = build_owner_prompt(owner_request=request, guild_name=guild_name, channel_name=channel_name)
    result = await run_hermes(
        hermes_bin=bot.settings.hermes_bin,
        profile=bot.settings.hermes_profile,
        repo=bot.settings.chaos_redux_repo,
        prompt=prompt,
        timeout_seconds=bot.settings.hermes_timeout_seconds,
    )
    output = result.stdout.strip() or result.stderr.strip() or "No output."
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
    header = f"ChaosX `{status}` hash `{result.prompt_hash[:12]}`"
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
) -> None:
    await run_hermes_command(bot, interaction, request, command_name=command_name, public=public, owner_only=True)


def register_commands(bot: ChaosXBot) -> None:
    settings = bot.settings

    @bot.tree.command(name="health", description="Owner-only ChaosX runtime health check.")
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

    @bot.tree.command(name="ask", description="Ask the local Chaos Redux Hermes profile to reason about a server/project task.")
    @app_commands.describe(request="Owner instruction. ChaosX will run local Hermes with Discord safety boundaries.")
    async def ask(interaction: discord.Interaction, request: str) -> None:
        await run_owner_hermes(bot, interaction, request, command_name="ask")

    @bot.tree.command(name="say", description="Owner-only: post an exact message to the current channel without mention parsing.")
    @app_commands.describe(message="Message to post. Mentions are not parsed.")
    async def say(interaction: discord.Interaction, message: str) -> None:
        if not await owner_gate(interaction, settings):
            return
        await bot.store.audit(actor_id=interaction.user.id, guild_id=interaction.guild_id, channel_id=interaction.channel_id, command="say", summary=message)
        await interaction.response.send_message("Posting message with mentions disabled.", ephemeral=True)
        await interaction.channel.send(message, allowed_mentions=safe_allowed_mentions())  # type: ignore[union-attr]

    chaosx = app_commands.Group(name="chaosx", description="Chaos Redux knowledge commands")
    repo = app_commands.Group(name="repo", description="Chaos Redux repository commands")
    work = app_commands.Group(name="work", description="Chaos Redux work-item drafting commands")
    playtest = app_commands.Group(name="playtest", description="Chaos Redux playtest commands")
    hermes = app_commands.Group(name="hermes", description="Hermes agent routing/task commands")
    admin = app_commands.Group(name="admin", description="ChaosX admin commands")

    @chaosx.command(name="ask", description="Answer a Chaos Redux question with evidence.")
    async def chaosx_ask(interaction: discord.Interaction, question: str, visibility: str = "private") -> None:
        await run_hermes_command(bot, interaction, f"/chaosx ask question={question!r} visibility={visibility!r}. Answer with evidence footer.", command_name="chaosx ask", public=visibility == "public", rate_bucket="ask")

    @chaosx.command(name="event", description="Look up an event by ID or name.")
    async def chaosx_event(interaction: discord.Interaction, event: str, view: str = "overview") -> None:
        await run_hermes_command(bot, interaction, f"/chaosx event event={event!r} view={view!r}. Include catalog, specs, implementation evidence, testing state.", command_name="chaosx event")

    @chaosx.command(name="scenario", description="Look up a scenario by ID or name.")
    async def chaosx_scenario(interaction: discord.Interaction, scenario: str, view: str = "overview") -> None:
        await run_hermes_command(bot, interaction, f"/chaosx scenario scenario={scenario!r} view={view!r}. Include evidence and testing status.", command_name="chaosx scenario")

    @chaosx.command(name="cluster", description="Look up an event cluster.")
    async def chaosx_cluster(interaction: discord.Interaction, cluster: str) -> None:
        await run_hermes_command(bot, interaction, f"/chaosx cluster cluster={cluster!r}. Never invent IDs for planned clusters.", command_name="chaosx cluster")

    @chaosx.command(name="mechanic", description="Explain a Chaos Redux mechanic.")
    async def chaosx_mechanic(interaction: discord.Interaction, mechanic: str) -> None:
        await run_hermes_command(bot, interaction, f"/chaosx mechanic mechanic={mechanic!r}. Distinguish design docs from implementation evidence.", command_name="chaosx mechanic")

    @chaosx.command(name="search", description="Search indexed/project sources.")
    async def chaosx_search(interaction: discord.Interaction, query: str, scope: str = "all") -> None:
        await run_hermes_command(bot, interaction, f"/chaosx search query={query!r} scope={scope!r}. Return ranked source snippets and evidence metadata.", command_name="chaosx search")

    @chaosx.command(name="source", description="Show source-of-truth map for an entity/path.")
    async def chaosx_source(interaction: discord.Interaction, query: str) -> None:
        await run_hermes_command(bot, interaction, f"/chaosx source query={query!r}. Explain intended design/current behavior/player wording/plans.", command_name="chaosx source")

    @chaosx.command(name="compare", description="Compare two sources/entities.")
    async def chaosx_compare(interaction: discord.Interaction, left: str, right: str) -> None:
        await run_hermes_command(bot, interaction, f"/chaosx compare left={left!r} right={right!r}. Highlight conflicts, missing surfaces, stale docs. Do not apply changes.", command_name="chaosx compare")

    @chaosx.command(name="status", description="Show completion/status matrix.")
    async def chaosx_status(interaction: discord.Interaction, entity: str = "global", surface: str = "all") -> None:
        await run_hermes_command(bot, interaction, f"/chaosx status entity={entity!r} surface={surface!r}. Separate finished/partial/blocked/unknown evidence.", command_name="chaosx status")

    @chaosx.command(name="testing", description="Show prioritized testing queue.")
    async def chaosx_testing(interaction: discord.Interaction, kind: str = "all", limit: int = 10) -> None:
        await run_hermes_command(bot, interaction, f"/chaosx testing kind={kind!r} limit={limit}. Prioritize Needs Testing, recent changes, issues, missing playtest evidence.", command_name="chaosx testing")

    @chaosx.command(name="help", description="Show ChaosX command help.")
    async def chaosx_help(interaction: discord.Interaction, topic: str = "all") -> None:
        await run_hermes_command(bot, interaction, f"/chaosx help topic={topic!r}. Keep compact.", command_name="chaosx help")

    @repo.command(name="status", description="Show repository/index status.")
    async def repo_status(interaction: discord.Interaction) -> None:
        await run_hermes_command(bot, interaction, "/repo status. Include branch, commit, dirty state, index health if available.", command_name="repo status")

    @repo.command(name="search", description="Search repo content/symbols.")
    async def repo_search(interaction: discord.Interaction, query: str, path: str = "") -> None:
        await run_hermes_command(bot, interaction, f"/repo search query={query!r} path={path!r}. Use exact/symbol search first.", command_name="repo search")

    @repo.command(name="file", description="Show safe excerpt from a repo file.")
    async def repo_file(interaction: discord.Interaction, path: str, lines: str = "") -> None:
        await run_hermes_command(bot, interaction, f"/repo file path={path!r} lines={lines!r}. Enforce size limits and redact secret-like values.", command_name="repo file")

    @repo.command(name="diff", description="Summarize a git diff.")
    async def repo_diff(interaction: discord.Interaction, ref_a: str, ref_b: str, path: str = "") -> None:
        await run_hermes_command(bot, interaction, f"/repo diff ref_a={ref_a!r} ref_b={ref_b!r} path={path!r}. Do not claim semantic correctness from diff alone.", command_name="repo diff")

    @repo.command(name="history", description="Show relevant history for an entity/path.")
    async def repo_history(interaction: discord.Interaction, entity: str, limit: int = 10) -> None:
        await run_hermes_command(bot, interaction, f"/repo history entity={entity!r} limit={limit}.", command_name="repo history")

    @work.command(name="issue-draft", description="Draft a GitHub issue from text/message context.")
    async def work_issue_draft(interaction: discord.Interaction, summary: str, event: str = "", surface: str = "") -> None:
        await run_owner_hermes(bot, interaction, f"/work issue-draft summary={summary!r} event={event!r} surface={surface!r}. Draft only; duplicate search; no issue creation.", command_name="work issue-draft")

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
        await run_hermes_command(bot, interaction, f"/playtest queue kind={kind!r}. Include evidence for ordering.", command_name="playtest queue")

    @playtest.command(name="schedule", description="Prepare a playtest Scheduled Event plan.")
    async def playtest_schedule(interaction: discord.Interaction, target: str, start: str, duration: int, voice: str = "none", build: str = "") -> None:
        await run_owner_hermes(bot, interaction, f"/playtest schedule target={target!r} start={start!r} duration={duration} voice={voice!r} build={build!r}. Preview first; create only if explicit current approval and permissions exist.", command_name="playtest schedule")

    @playtest.command(name="report", description="Draft a structured playtest report.")
    async def playtest_report(interaction: discord.Interaction, event: str, observation: str) -> None:
        await run_hermes_command(bot, interaction, f"/playtest report event={event!r} observation={observation!r}. Structure result and optional issue draft.", command_name="playtest report")

    @playtest.command(name="summary", description="Summarize a playtest.")
    async def playtest_summary(interaction: discord.Interaction, event: str) -> None:
        await run_hermes_command(bot, interaction, f"/playtest summary event={event!r}. Distinguish observations from reproduced defects.", command_name="playtest summary")

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
        await run_owner_hermes(bot, interaction, f"/admin automation action={action!r} name={name!r}. Config diffs and approval gates required for changes.", command_name="admin automation")

    @admin.command(name="config", description="Show/validate config with secrets redacted.")
    async def admin_config(interaction: discord.Interaction, action: str = "show") -> None:
        await run_owner_hermes(bot, interaction, f"/admin config action={action!r}. Redact secrets.", command_name="admin config")

    @admin.command(name="permissions-audit", description="Audit Discord/GitHub permissions.")
    async def admin_permissions_audit(interaction: discord.Interaction) -> None:
        await run_owner_hermes(bot, interaction, "/admin permissions audit. Identify excessive permissions and drift.", command_name="admin permissions-audit")

    @admin.command(name="jobs", description="List/retry jobs.")
    async def admin_jobs(interaction: discord.Interaction, action: str = "list", job: str = "") -> None:
        await run_owner_hermes(bot, interaction, f"/admin jobs action={action!r} job={job!r}.", command_name="admin jobs")

    @admin.command(name="rollback", description="Prepare rollback instructions for a deployment.")
    async def admin_rollback(interaction: discord.Interaction, deployment: str) -> None:
        await run_owner_hermes(bot, interaction, f"/admin rollback deployment={deployment!r}. Do not perform destructive rollback without explicit approval.", command_name="admin rollback")

    for group in (chaosx, repo, work, playtest, hermes, admin):
        bot.tree.add_command(group)
