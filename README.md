# ChaosX Discord Bot

Community-facing Discord knowledge bot for the Chaos Redux server, with separate admin tools for operators.

ChaosX is intended for the Chaos Redux community to ask bounded project questions while keeping operational/admin actions restricted to the configured owner Discord user ID. Public token-consuming commands are rate-limited and length-limited.

## What it does now

- Registers slash commands with Discord.
- Community commands are available only in the configured Chaos Redux guild; the bot leaves unauthorized guilds on join/startup.
- Public lookup/project-question commands auto-refresh the local index when repo/catalog files change.
- Admin/automation/server-write commands refuse every user unless `interaction.user.id == CHAOSX_OWNER_ID`.
- Optional guild lock with `CHAOSX_ALLOWED_GUILD_ID`.
- Uses Message Content intent for direct `@ChaosX <question>` mentions, replies to stored ChaosX answers, owner-requested message analysis, and the zero-token auto-scan gate. Auto-scan checks every new allowed-guild message locally, but only replies on exact/high-confidence Chaos Redux/server matches or obvious soft-warning cases; it does not call Hermes/Luna per message.
- Uses safe `AllowedMentions` so `@everyone`, `@here`, users, and roles are not parsed by default; owner `/admin ask` may deliberately enable only the explicitly requested mention types for admin announcements/actions.
- Bot presence/description: `Chaos Redux community knowledge bot` / watching `Chaos Redux ops`.
- Bot profile description: `Ask ChaosX questions about Chaos Redux events, scenarios, mechanics, testing, and mod info.`
- Public limits by default: 10 broad `/ask` or direct mention asks per user/hour, 20 scripted read-only commands per user/hour, 600-character public prompt cap.
- Broad ask model override: `CHAOSX_ASK_PROVIDER=openai-codex`, `CHAOSX_ASK_MODEL=gpt-5.6-luna`, `CHAOSX_ASK_REASONING_EFFORT=medium`.
- Public Hermes-backed commands run with the `safe` toolset and a public prompt boundary: answer only Chaos Redux/mod/server-use questions, refuse dangerous/off-topic requests, do not perform external actions, and include repo/vault/spec/code paths only when explicitly asked. Public `/ask` uses a prebuilt SQLite/FTS index over the Chaos Redux repo plus whitelisted Chaos Redux Vault folders, with `.env`, token notes, `.obsidian`, raw ingest, logs, and private/non-project paths excluded. Model-backed bot answers store their bot message ID in local SQLite; when a community member replies to a stored ChaosX answer, only that reply chain is injected as low-priority context. When Hoops mentions ChaosX or replies to one of its messages, the message routes through owner/admin mode automatically.
- Approved `/event-idea` outputs are quietly captured to the Chaos Redux vault, refresh the vault index/reference notes/log, and auto-create a sanitized forum post in `CHAOSX_COMMUNITY_EVENT_IDEAS_CHANNEL_ID`. Approved `/suggestion` outputs are quietly captured to the vault and refresh indexes/logs.
- Protected autonomous server-management model override: `CHAOSX_OPERATOR_PROVIDER=openai-codex`, `CHAOSX_OPERATOR_MODEL=gpt-5.6-luna`, `CHAOSX_OPERATOR_REASONING_EFFORT=xhigh`.
- Provides:
  - `/help` — public community command guide.
  - `/ask`, direct `@ChaosX <question>` mentions, `/suggestion`, `/event-idea` — public AI-backed Chaos Redux question/drafting commands.
  - `/event`, `/scenario`, `/cluster`, `/status`, `/testing` — public scripted Chaos Redux knowledge/testing commands. `/cluster` names member events, `/testing` shows events marked as needing playtesting, and `/scenario` reads triggerable SCN scenario docs, not event IDs.
  - `/event-idea` formats a rough event idea with name, ID placeholder, optional type/cluster/evolutions/world-end/scenario/easter-egg fields, baseline description, testing notes, and overlap/gap notes. When approved, ChaosX also creates a sanitized forum post in the configured event-ideas channel.
  - `/issue` — opens a report form, uses AI to review it, then formats approved bug/crash/enhancement/balance/cosmetic/general reports into GitHub issues in `CHAOSX_GITHUB_REPO`; bug/crash forms require relevant `error.log` lines, while other report types use expected/desired-result fields instead.
  - `/testing` shows the tester queue. `/playtest report observation:<text> [event_id:<id>]` records informal tester observations that are not ready for GitHub; `/playtest summary` shows recent reported playtests. `/playtest schedule request:<plain English>` is protected and AI-powered: it stores a local draft and returns a private plan/ready-to-post message, but does not create a Discord Scheduled Event or public post unless Hoops confirms a follow-up action.
  - `/admin ...` — private owner/operator ask, health, sync, reindex, automation, Q&A, auto-scan, jobs, and permissions-audit command family. `/admin automation` explains what each automation does and where it posts. `/admin ask` is the main private catch-all for server and project operations, including scoped follow-up memory, plain-text member resolution, and explicit recent channel/user message analysis; `/admin help` shows only owner/admin tools.
  - Auto-scan automation checks each new allowed-guild message with local rules only. It auto-answers exact event/scenario/cluster/status/testing/server-help questions, saves successful auto Q&A into `/admin qna`, seeds reply-chain memory for replies to those auto-answers, gives soft warnings for obvious rule problems, and posts warning notices to `CHAOSX_AUTO_SCAN_NOTIFY_CHANNEL_ID` or `CHAOSX_AUTOMATION_REMINDER_CHANNEL_ID`.
  - Weekly content-dump automation posts to `CHAOSX_CONTENT_DUMP_CHANNEL_ID` only when enough fresh images/assets exist; it stays silent rather than posting a text-only dump.
- Stores a local SQLite audit log in `CHAOSX_DB_PATH`.

## Security model

ChaosX is a bot account, not a self-bot. Do not use a normal Discord user token.

Hoops currently wants ChaosX to have maximum server control, while keeping execution owner-only under `/admin ask`:

- Administrator

If reverting to a narrow setup later, use:

- View Channels
- Send Messages
- Embed Links
- Attach Files only if needed later
- Read Message History where `/admin ask` message-analysis workflows need it
- Message Content Intent in the Developer Portal is needed for direct `@ChaosX <question>` asks, replies to ChaosX answers, owner-requested message analysis, and zero-token auto-scan. ChaosX still ignores bots/webhooks, DMs, unauthorized guilds, slash-like messages, excluded channels, direct mentions/replies already handled by the ask path, and any auto-answer candidate below `CHAOSX_AUTO_SCAN_MIN_CONFIDENCE`.
- Create Events only after Hoops explicitly confirms a follow-up action from a playtest draft. `/playtest schedule` itself is draft-only and does not create Discord Scheduled Events.

`/admin ask` remains runtime-gated to the configured owner ID before any protected operation runs. Do not expose separate public moderation/member-management commands unless explicitly requested.

## Setup

```bash
cd /mnt/c/Users/klimp/Documents/Projects/chaosx-discord-bot
uv sync --extra dev
cp .env.example .env
# edit .env locally; never paste the token in Discord
uv run chaosx-bot
```

Create the Discord app/bot in the Discord Developer Portal, copy the bot token into `.env`, and invite the bot to the Chaos Redux server with `applications.commands` plus the chosen bot permission set. Current maximum-control invite uses permission integer `8`.

For fast command registration during staging, set:

```env
CHAOSX_COMMAND_GUILD_ID=<Chaos Redux guild id>
CHAOSX_ALLOWED_GUILD_ID=<Chaos Redux guild id>
```

## Protected Hermes bridge

`/ask` is the public/community broad question command. Protected owner/operator asks live under `/admin ask` only.

`/admin ask` has no ChaosX subprocess timeout; other protected Hermes-backed commands use the configured timeout:

```bash
hermes --profile chaos_redux chat -q '<bounded prompt>' --quiet
```

The prompt includes a safety boundary that says Discord messages, repo files, issues, attachments, and retrieved content are untrusted data. It forbids secret disclosure and broad permission fallbacks. Owner `/admin ask` is treated as authorized admin direction for explicitly requested Discord actions, including explicitly requested `@everyone`/`@here`/role/user mentions; the bot should not add pings on its own.

`/admin ask` stores recent owner-only turns in SQLite per owner + guild + Discord channel/thread and injects the last `CHAOSX_ADMIN_ASK_MEMORY_TURNS` turns into the next `/admin ask` for follow-up context. Say `reset context` through `/admin ask` to clear that channel/thread context. History is context only; server mutations still require explicit approval in the current request.

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
