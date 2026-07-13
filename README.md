# ChaosX Discord Bot

Community-facing Discord knowledge bot for the Chaos Redux server, with separate admin tools for operators.

ChaosX is intended for the Chaos Redux community to ask bounded project questions while keeping operational/admin actions restricted to the configured owner Discord user ID. Public token-consuming commands are rate-limited and length-limited.

## What it does now

- Registers slash commands with Discord.
- Community commands are available only in the configured Chaos Redux guild; the bot leaves unauthorized guilds on join/startup.
- Public lookup/project-question commands auto-refresh the local index when repo/catalog files change.
- Admin/automation/server-write commands refuse every user unless `interaction.user.id == CHAOSX_OWNER_ID`.
- Optional guild lock with `CHAOSX_ALLOWED_GUILD_ID`.
- Uses no Message Content privileged intent by default.
- Uses safe `AllowedMentions` so `@everyone`, `@here`, users, and roles are not parsed by default.
- Bot presence/description: `Chaos Redux community knowledge bot` / watching `Chaos Redux ops`.
- Bot profile description: `Ask ChaosX questions about Chaos Redux events, scenarios, mechanics, testing, and mod info.`
- Public limits by default: 10 broad `/ask` calls per user/hour, 20 scripted read-only commands per user/hour, 600-character public prompt cap.
- Broad ask model override: `CHAOSX_ASK_PROVIDER=openai-codex`, `CHAOSX_ASK_MODEL=gpt-5.6-luna`, `CHAOSX_ASK_REASONING_EFFORT=medium`.
- Public Hermes-backed commands run with the `safe` toolset and a public prompt boundary: answer only Chaos Redux/mod/server-use questions, refuse dangerous/off-topic requests, do not perform external actions, and include repo/spec/code paths only when explicitly asked.
- Protected autonomous server-management model override: `CHAOSX_OPERATOR_PROVIDER=openai-codex`, `CHAOSX_OPERATOR_MODEL=gpt-5.6-luna`, `CHAOSX_OPERATOR_REASONING_EFFORT=xhigh`.
- Provides:
  - `/help` — public community command guide.
  - `/ask`, `/suggestion`, `/event-idea` — public AI-backed Chaos Redux question/drafting commands.
  - `/event`, `/scenario`, `/cluster`, `/status`, `/testing` — public scripted Chaos Redux knowledge/testing commands. `/cluster` names member events, `/testing` shows events marked as needing playtesting, and `/scenario` reads triggerable SCN scenario docs, not event IDs.
  - `/event-idea` formats a rough event idea with name, ID placeholder, optional type/cluster/evolutions/world-end/scenario/easter-egg fields, baseline description, testing notes, and overlap/gap notes.
  - `/issue` — opens a report form, uses AI to review it, then formats approved bug/crash/enhancement/balance/cosmetic/general reports into GitHub issues in `CHAOSX_GITHUB_REPO`; bug/crash forms require relevant `error.log` lines, while other report types use expected/desired-result fields instead.
  - `/work ...` — protected issue-style drafts, handoffs, changelog, and release draft command family.
  - `/testing` shows the tester queue. `/playtest report observation:<text> [event_id:<id>]` records informal tester observations that are not ready for GitHub; `/playtest summary` recaps reports. Scheduling/cancel helpers are protected.
  - `/hermes ...` — route/task/status/cancel/audit/review-pr command family.
  - `/admin ...` — private owner/operator help, ask, health/sync/reindex/config/permissions/jobs/rollback command family. `/admin ask` is the main private catch-all for server and project operations; `/admin help` explains when to use the smaller shortcuts.
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

## Protected Hermes bridge

`/ask` is the public/community broad question command. Protected owner/operator asks live under `/admin ask` and `/server ask`.

Protected Hermes-backed commands execute:

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
4. Add command groups incrementally: playtest scheduling, issue drafts, GitHub webhook receiver.
5. Keep write-capable features behind explicit owner confirmation.
