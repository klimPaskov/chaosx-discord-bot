# ChaosX setup links

Use these in your browser. Do not paste the bot token into Discord.

## 1. Create/open the app

- Discord Developer Portal: <https://discord.com/developers/applications>

Create an application named `ChaosX`, then open **Bot** and reset/copy the bot token into local `.env` only.

## 2. Required bot settings

In the Developer Portal:

- Bot → Public Bot: recommended off if Discord allows it for your app/workflow.
- Privileged Gateway Intents:
  - Presence Intent: off
  - Server Members Intent: off for the current scaffold
  - Message Content Intent: off for the current scaffold

## 3. OAuth invite URL template

Replace `<CLIENT_ID>` with the app/client ID from General Information.

Baseline current scaffold permissions:

- View Channels
- Send Messages
- Embed Links
- Read Message History

Permission integer: `84992`

```text
https://discord.com/oauth2/authorize?client_id=<CLIENT_ID>&scope=bot+applications.commands&permissions=84992
```

If you already know the Chaos Redux server ID, lock the invite to it:

```text
https://discord.com/oauth2/authorize?client_id=<CLIENT_ID>&scope=bot+applications.commands&permissions=84992&guild_id=<GUILD_ID>&disable_guild_select=true
```

## 4. Generate the exact link locally

```bash
cd /mnt/c/Users/klimp/Documents/Projects/chaosx-discord-bot
uv run python scripts/discord_invite_url.py <CLIENT_ID> --guild-id <GUILD_ID>
```

For later playtest scheduling, use:

```bash
uv run python scripts/discord_invite_url.py <CLIENT_ID> --guild-id <GUILD_ID> --profile playtest
```

That adds Create Events and produces a larger permission integer.

## 5. Local run

```bash
cd /mnt/c/Users/klimp/Documents/Projects/chaosx-discord-bot
cp .env.example .env  # if .env does not already exist
# Edit .env and set CHAOSX_DISCORD_TOKEN, CHAOSX_ALLOWED_GUILD_ID, CHAOSX_COMMAND_GUILD_ID
uv run chaosx-bot
```

## 6. First Discord checks

In the target server, as Hoops/Klim only:

```text
/health
/inventory
```

Other users should be denied only for protected operator commands.
