# ChaosX implementation notes

## Current architecture

- `discord.py` interaction-only bot.
- No Message Content intent.
- Public community-knowledge command gate plus a small protected admin/automation gate.
- Single-guild lock for the Chaos Redux server.
- Local SQLite audit log and automation/job records.
- Hermes subprocess bridge for rate-limited community ask plus the private `/admin ask` catch-all.

## Design choices

- ChaosX is its own Discord bot identity and runtime, not an automation of a normal user account.
- Community knowledge/tester commands are public inside the configured guild, but source/spec/repo-file views are not public because implementation specs are for Klim and coding agents.
- Owner/operator work should mostly go through `/admin ask`; avoid exposing tiny one-off admin/server commands unless Hoops explicitly asks for them.
- Repository/project reasoning is delegated to local Hermes profile `chaos_redux` through a bounded prompt.
- Reminder/digest-style automation output defaults to the configured automation reminder channel.

## Approval gates to preserve

- Do not add Message Content intent unless explicitly approved.
- Do not add `Administrator`, `Manage Roles`, `Manage Channels`, `Manage Guild`, or `Manage Webhooks` permissions unless a specific approved feature needs them.
- Do not create/delete/rename/reorder channels or roles from generic public `/ask` output.
- Do not create announcements, scheduled events, or PRs without a preview + owner confirmation flow.
- Public `/issue` may create GitHub issues only through its validated AI-reviewed report form.

## Current command shape

- Public: `/help`, `/ask`, `/event`, `/scenario`, `/cluster`, `/status`, `/testing`, `/suggestion`, `/event-idea`, `/issue`, `/playtest report`, `/playtest summary`.
- Protected owner shortcuts: `/admin ask`, `/admin help`, `/admin health`, `/admin sync`, `/admin reindex`, `/admin automation`, `/admin jobs`, `/admin permissions-audit`, `/work ...`, protected `/playtest schedule`/`cancel`.
- Removed from the user command surface: `/server`, `/hermes`, `/admin config`, `/admin rollback`, `/search`, `/mechanic`, and tiny role-management commands.

Keep the owner gate centralized and covered by tests whenever adding commands.
