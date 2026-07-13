from __future__ import annotations

import discord
from discord import app_commands

from .auth import deny_reason, safe_allowed_mentions
from .config import Settings
from .hermes_bridge import build_owner_prompt, run_hermes
from .storage import Store


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

    async def setup_hook(self) -> None:
        await self.store.init()
        register_commands(self)
        if self.settings.command_guild_id:
            guild = discord.Object(id=self.settings.command_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        print(f"ChaosX logged in as {self.user} owner_id={self.settings.owner_id}")


async def owner_gate(interaction: discord.Interaction, settings: Settings) -> bool:
    reason = deny_reason(
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


def register_commands(bot: ChaosXBot) -> None:
    settings = bot.settings

    @bot.tree.command(name="health", description="Owner-only ChaosX runtime health check.")
    async def health(interaction: discord.Interaction) -> None:
        if not await owner_gate(interaction, settings):
            return
        guilds = ", ".join(f"{g.name}({g.id})" for g in bot.guilds) or "none"
        text = (
            "ChaosX online.\n"
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
        if not await owner_gate(interaction, settings):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_name, channel_name = _guild_channel(interaction)
        prompt = build_owner_prompt(owner_request=request, guild_name=guild_name, channel_name=channel_name)
        result = await run_hermes(
            hermes_bin=settings.hermes_bin,
            profile=settings.hermes_profile,
            repo=settings.chaos_redux_repo,
            prompt=prompt,
            timeout_seconds=settings.hermes_timeout_seconds,
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
        header = f"Hermes run `{status}` hash `{result.prompt_hash[:12]}` returncode `{result.returncode}`"
        for i, part in enumerate(_chunk(output)):
            await interaction.followup.send((header + "\n" if i == 0 else "") + part, ephemeral=True, allowed_mentions=safe_allowed_mentions())

    @bot.tree.command(name="say", description="Owner-only: post an exact message to the current channel without mention parsing.")
    @app_commands.describe(message="Message to post. Mentions are not parsed.")
    async def say(interaction: discord.Interaction, message: str) -> None:
        if not await owner_gate(interaction, settings):
            return
        await bot.store.audit(actor_id=interaction.user.id, guild_id=interaction.guild_id, channel_id=interaction.channel_id, command="say", summary=message)
        await interaction.response.send_message("Posting message with mentions disabled.", ephemeral=True)
        await interaction.channel.send(message, allowed_mentions=safe_allowed_mentions())  # type: ignore[union-attr]
