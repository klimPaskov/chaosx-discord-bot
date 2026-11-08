# Deploying the HOI4 Agent Tools 4.x move

What changes on the VPS when the bot moves from `hoi4-agent-tools@1.2.0` to 4.12.0. Nothing here is done by the code change itself: the bot reads these settings from its `.env` and its machine.

## 1. Node.js

4.x needs Node.js `^22.19.0 || ^24.0.0` for the `npx` that launches it. Check with `node --version` as the bot's user. If the bot's `CHAOSX_FOCUS_MCP_COMMAND` is an absolute `node`, the bot checks the version itself and refuses an older one.

## 2. `.env`

- `CHAOSX_FOCUS_MCP_ARGS`: if the line exists, set it to `-y hoi4-agent-tools@4.12.0`, or delete it to take the new default. An old `-y hoi4-agent-tools@1.2.0` there overrides the code default and keeps the bot on 1.2.0.
- Delete `HOI4_AGENT_TOOLS_CHAOSX` from any service unit or `.env`. 4.x ignores it and the bot no longer sets it.
- `CHAOSX_FOCUS_MCP_WORKSPACE_NAME` (default `chaos_redux`) is now passed straight to the tools, so it must be the mod folder's name or the workspace `id` in the server config. `CHAOSX_FOCUS_MCP_WORKSPACE_ID` still wins when set.
- Optional: `CHAOSX_FOCUS_MCP_STATE_DIR=/var/lib/chaosx/mcp-state` (any folder the bot's user can write) keeps the server's analysis cache across restarts. Measured on the real mod: the first event-chain render after a start takes about 170 s with a cold cache and about 50 s with a warm one. Rendered images still go to a disposable folder.
- Optional: `CHAOSX_FOCUS_MCP_PLUGINS` overrides the plugin path (see 4). Leave it empty to use the vault.
- New switches, both default on: `CHAOSX_REFERENCE_LOOKUP_ENABLED` (`/docs`) and `CHAOSX_UI_PREVIEW_ENABLED` (`/ui-preview`).

## 3. The server config file

`CHAOSX_FOCUS_MCP_CONFIG_PATH` (default `~/.config/hoi4-agent-tools/config.json`) was written for 1.x. 4.x rejects unknown keys and stops with `CONFIG_INVALID`. Either:

- move the file aside and let the bot write its own minimal config (the mod checkout in `CHAOSX_FOCUS_TREE_REPO`, or `CHAOSX_CHAOS_REDUX_REPO` when that is empty, becomes the only workspace, named `chaos_redux`); or
- regenerate it with `npx -y hoi4-agent-tools@4.12.0 hoi4-agent-tools-setup --init` and check it with `--diagnose`.

Add `"gameRoot": "<installed Hearts of Iron IV folder>"` if the VPS has the game files. Without it `/docs` searches the Paradox wiki snapshot in the mod's `hoi4_wiki/` folder only, and the game's own `documentation` folder is missing from the results.

## 4. The ChaosX plugin

The two private tools live in the vault, not in npm: `hoi4-agent-tools-plugin/chaosx-plugin.mjs` in `klimPaskov/chaos-redux-vault` (merged to its `main`, commit `f6809c2`). The bot lists the file in the `plugins` key of the isolated config it writes for each session.

- On the VPS, pull the vault so that `$OBSIDIAN_VAULT_PATH/hoi4-agent-tools-plugin/chaosx-plugin.mjs` exists. The file must be a regular file, not a symlink.
- Without it the bot still starts and logs a warning (`HOI4 Agent Tools plugin not found at ...`). Focus trees render without the flag and leader pictures beside them, and scripted-GUI previews cache on the source file's stat with a 6-hour expiry instead of the exact revision.
- The plugin is built against a server release. Rebuild it (`node hoi4-agent-tools-plugin/build.mjs` in the vault) when the pin moves past 4.12.0.

## 5. Restart

Restart the bot service. The new `/docs` and `/ui-preview` commands register on start. Graphviz (`dot`) is unchanged.

## Rollback

Set `CHAOSX_FOCUS_MCP_ARGS=-y hoi4-agent-tools@1.2.0` and restore the old config file. The code still runs against 1.x only for the unchanged tools, because it no longer calls `hoi4.mods` or `hoi4.focus_raster`, so a real rollback means redeploying the previous bot release.
