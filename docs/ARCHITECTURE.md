# ChaosX implementation notes

## Current architecture

- `discord.py` interaction-only bot.
- No Message Content intent.
- Public community-knowledge command gate plus protected admin/automation gate.
- Optional single-guild lock.
- Local SQLite audit log.
- Hermes subprocess bridge for rate-limited community ask plus protected owner-directed autonomous project/server operations.

## Design choices

- ChaosX is its own Discord bot identity and runtime, not an automation of a normal user account.
- Community knowledge/tester commands are public inside the configured guild, but source/spec/repo-file views are not public because implementation specs are for Klim and coding agents.
- Server-management functionality stays under protected `/admin`, `/server`, and `/hermes` command groups with previews/permission gates for risky side effects.
- Repository/project reasoning is delegated to local Hermes profile `chaos_redux` through a bounded prompt.

## Approval gates to preserve

- Do not add Message Content intent unless explicitly approved.
- Do not add `Administrator`, `Manage Roles`, `Manage Channels`, `Manage Guild`, or `Manage Webhooks` permissions.
- Do not create/delete/rename/reorder channels or roles from generic public `/ask` output.
- Do not create GitHub issues, Scheduled Events, announcements, or PRs without a preview + owner confirmation flow.

## Command expansion plan

- `/help`, `/ask`, `/event`, `/scenario`, `/search`, `/testing`
- `/work issue-draft`, `/work suggestion`, `/work event-idea`
- `/playtest schedule`, `/playtest report`, `/playtest summary`
- `/admin permissions-audit`, `/admin config-show`, `/admin sync`

Keep the owner gate centralized and covered by tests whenever adding commands.
