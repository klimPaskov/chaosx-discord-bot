"""Check whether the Server Members privileged intent is active, without touching the gateway.

A second gateway connection would invalidate the running bot's session, so this uses REST only:
GET /guilds/<id>/members requires the intent to be enabled on the application. 403 = still off.
The token is read from .env and never printed.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

ENV = Path(__file__).with_name(".env")
if not ENV.exists():
    ENV = Path(".env")

token = None
for line in ENV.read_text(encoding="utf-8").splitlines():
    m = re.match(r"\s*CHAOSX_DISCORD_TOKEN\s*=\s*(.+?)\s*$", line)
    if m:
        token = m.group(1).strip().strip('"').strip("'")
if not token:
    raise SystemExit("token not found in .env")


def get(url: str):
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bot " + token,
            "User-Agent": "DiscordBot (chaosx-check, 1.0)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:160].decode("utf-8", "replace")


status, guilds = get("https://discord.com/api/v10/users/@me/guilds")
print("guilds endpoint:", status, len(guilds) if isinstance(guilds, list) else guilds)
if isinstance(guilds, list):
    for guild in guilds:
        if str(guild.get("id")) != "1395459671598436533":
            continue
        code, members = get(
            "https://discord.com/api/v10/guilds/"
            + str(guild["id"])
            + "/members?limit=1000"
        )
        if isinstance(members, list):
            print("members endpoint:", code, "returned", len(members), "members")
            print("VERDICT: intent is ENABLED")
        else:
            print("members endpoint:", code, members)
            print("VERDICT: intent is NOT enabled")
