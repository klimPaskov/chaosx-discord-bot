# ChaosX Discord Bot

Owner-only Discord command agent for Chaos Redux server operations.

This is intentionally **not** a public community chatbot. It is a private control surface: only the configured owner Discord user ID can invoke commands. Other users receive an ephemeral denial.

## What it does now

- Registers slash commands with Discord.
- Refuses every command unless `interaction.user.id == CHAOSX_OWNER_ID`.
- Optional guild lock with `CHAOSX_ALLOWED_GUILD_ID`.
- Uses no Message Content privileged intent by default.
- Uses safe `AllowedMentions` so `@everyone`, `@here`, users, and roles are not parsed by default.
- Provides:
  - `/health` — private runtime/status check.
  - `/inventory` — private read-only guild/channel/role inventory.
  - `/ask` — runs the local `chaos_redux` Hermes profile with Discord safety boundaries.
  - `/say` — owner-only exact post to the current channel with mentions disabled.
- Stores a local SQLite audit log in `CHAOSX_DB_PATH`.

## Security model

ChaosX is a bot account, not a self-bot. Do not use a normal Discord user token.

Baseline Discord permissions should stay narrow:

- View Channels
- Send Messages
- Embed Links
- Attach Files only if needed later
- Read Message History only where command/context workflows need it
- Create Events only when playtest scheduling is implemented

Do **not** grant:

- Administrator
- Manage Roles
- Manage Channels
- Manage Guild
- Manage Webhooks
- moderation permissions

## Setup

```bash
cd /mnt/c/Users/klimp/Documents/Projects/chaosx-discord-bot
uv sync --extra dev
cp .env.example .env
# edit .env locally; never paste the token in Discord
uv run chaosx-bot
```

Create the Discord app/bot in the Discord Developer Portal, copy the bot token into `.env`, and invite the bot to the Chaos Redux server with only the minimal permissions above plus `applications.commands`.

For fast command registration during staging, set:

```env
CHAOSX_COMMAND_GUILD_ID=<Chaos Redux guild id>
CHAOSX_ALLOWED_GUILD_ID=<Chaos Redux guild id>
```

## Owner-only Hermes bridge

`/ask` executes:

```bash
hermes --profile chaos_redux chat -q '<bounded prompt>' --quiet
```

The prompt includes a safety boundary that says Discord messages, repo files, issues, attachments, and retrieved content are untrusted data. It also forbids secret disclosure, broad permission fallbacks, mass pings, and server-structure changes unless explicitly approved.

## Development checks

```bash
uv run pytest
uv run python -m chaosx_bot.main
```

The second command should exit with a missing-token error unless `.env` has `CHAOSX_DISCORD_TOKEN`.

## Next implementation steps

1. Add the real Chaos Redux guild ID to `.env`.
2. Start the bot and run `/health` privately.
3. Run `/inventory` in the target server and save the baseline output.
4. Add command groups incrementally: playtest scheduling, issue drafts, repo search, GitHub webhook receiver.
5. Keep write-capable features behind explicit owner confirmation.
