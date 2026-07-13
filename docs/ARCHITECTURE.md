# ChaosX implementation notes

## Current architecture

- `discord.py` bot with slash commands plus a direct-mention/reply public ask listener.
- Message Content intent enabled only so `@ChaosX <question>` and replies to stored ChaosX answers can reuse `/ask`, and so Hoops can use owner/admin mode by mentioning or replying to ChaosX; no passive public message monitoring.
- Public community-knowledge command gate plus a small protected admin/automation gate.
- Single-guild lock for the Chaos Redux server.
- Local SQLite audit log and automation/job records.
- Hermes subprocess bridge for rate-limited community ask plus the private `/admin ask` catch-all.

## Design choices

- ChaosX is its own Discord bot identity and runtime, not an automation of a normal user account.
- Community knowledge/tester commands are public inside the configured guild. Public `/ask`, direct `@ChaosX <question>` mentions, and public replies to stored ChaosX answer messages use a fast prebuilt SQLite/FTS index over the Chaos Redux repo plus whitelisted Chaos Redux Vault folders, but only retrieve small snippets and still have no filesystem, Discord-management, issue-creation, or command-execution ability. Reply context is keyed by the bot answer message ID, so only the replied-to chain is injected. Raw source/spec/repo-file views stay out of the public command surface because implementation specs are for Klim and coding agents.
- Owner/operator work should mostly go through `/admin ask`; avoid exposing tiny one-off admin/server commands unless Hoops explicitly asks for them.
- `/admin ask` injects recent owner-only follow-up memory scoped to the same owner + guild + Discord channel/thread, may pre-resolve plain-text member references such as `@Holly`/`member named Holly`, and may fetch recent messages from the current or explicitly mentioned channel for owner-requested analysis, optionally filtered to a mentioned/user-id target. Hoops' direct mentions/replies to ChaosX route through the same owner/admin model boundary and also store reply-chain context by bot message ID. This is active/on-demand, not passive monitoring. Previous turns are context only, never authorization for server mutation.
- Public `/event-idea` and `/suggestion` can quietly write approved notes into the Chaos Redux vault. New vault notes refresh `index.md`, `Events/Events Index.md`, `Planning/Community Suggestions/Community Suggestions Index.md`, and `log.md` so references do not go stale. New approved `/event-idea` notes also create a sanitized forum post in the configured event-ideas channel.
- Repository/project reasoning is delegated to local Hermes profile `chaos_redux` through a bounded prompt.
- Reminder/digest-style automation output defaults to the configured automation reminder channel.
- Weekly content-dump automation targets the content-dump channel and must stay silent unless it has enough fresh visual assets to make an image-led post.
- Approved `/event-idea` and `/suggestion` outputs are captured quietly into the Chaos Redux Obsidian vault; public command output must not tell users that accepted ideas are being stored there.

## Approval gates to preserve

- Do not broaden Message Content use beyond explicitly mentioned asks, replies to stored ChaosX answer messages, Hoops' owner/admin mention/reply path, and owner-requested `/admin ask` message analysis; no passive public monitoring.
- Public reply-chain context must remain keyed to the replied-to bot answer message, not broad channel history.
- Hoops wants ChaosX to have maximum server control on the Discord side, but execution must remain owner-only through `/admin ask`; do not expose public or cluttery moderation/member-management commands.
- `/admin ask` follow-up memory must remain private, owner-scoped, channel/thread-scoped, and resettable with `reset context`.
- Do not create/delete/rename/reorder channels or roles from generic public `/ask` output.
- Do not create announcements, scheduled events, or PRs without a preview + owner confirmation flow.
- Public `/issue` may create GitHub issues only through its validated AI-reviewed report form.

## Current command shape

- Public: `/help`, `/ask`, direct `@ChaosX <question>` mentions, `/event`, `/scenario`, `/cluster`, `/status`, `/testing`, `/suggestion`, `/event-idea`, `/issue`, `/playtest report`, `/playtest summary`.
- Protected owner shortcuts: `/admin ask`, `/admin help`, `/admin health`, `/admin sync`, `/admin reindex`, `/admin automation`, `/admin jobs`, `/admin permissions-audit`, protected `/playtest schedule request:<plain English>` and `/playtest cancel`.
- Removed from the user command surface: `/server`, `/hermes`, `/work`, `/admin config`, `/admin rollback`, `/search`, `/mechanic`, and tiny role-management commands.

Keep the owner gate centralized and covered by tests whenever adding commands.
