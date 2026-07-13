# ChaosX implementation notes

## Current architecture

- `discord.py` interaction-only bot.
- No Message Content intent.
- Public read-only command gate plus protected admin/automation gate.
- Optional single-guild lock.
- Local SQLite audit log.
- Hermes subprocess bridge for owner-directed autonomous project operations.

## Design choices

- ChaosX is its own Discord bot identity and runtime, not an automation of a normal user account.
- It is not public-facing: command authorization is based on the owner Discord user ID, not community roles.
- Server-management functionality should be added as explicit commands with previews and confirmation, not broad natural-language execution of arbitrary Discord mutations.
- Repository/project reasoning is delegated to local Hermes profile `chaos_redux` through a bounded prompt.

## Approval gates to preserve

- Do not add Message Content intent unless explicitly approved.
- Do not add `Administrator`, `Manage Roles`, `Manage Channels`, `Manage Guild`, or `Manage Webhooks` permissions.
- Do not create/delete/rename/reorder channels or roles from generic `/ask` output.
- Do not create GitHub issues, Scheduled Events, announcements, or PRs without a preview + owner confirmation flow.

## Command expansion plan

- `/repo status`, `/repo search`, `/repo file`
- `/chaosx event`, `/chaosx scenario`, `/chaosx search`
- `/work issue-draft`, `/work suggestion`, `/work event-idea`
- `/playtest schedule`, `/playtest report`, `/playtest summary`
- `/admin permissions-audit`, `/admin config-show`, `/admin sync`

Keep the owner gate centralized and covered by tests whenever adding commands.
