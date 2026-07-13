#!/usr/bin/env python3
from __future__ import annotations

import argparse
from urllib.parse import urlencode

# Permission helper. Current Hoops-approved maximum-control setup uses
# Administrator while keeping execution owner-gated in `/admin ask`.
PERMISSIONS = {
    "administrator": 1 << 3,
    "view_channels": 1 << 10,
    "send_messages": 1 << 11,
    "embed_links": 1 << 14,
    "attach_files": 1 << 15,
    "read_message_history": 1 << 16,
    "send_messages_in_threads": 1 << 38,
    "create_events": 1 << 44,
}

BASELINE = ["view_channels", "send_messages", "embed_links", "read_message_history"]
PLAYTEST = BASELINE + ["create_events"]
EXPORTS = BASELINE + ["attach_files"]
ADMIN = ["administrator"]


def permission_value(names: list[str]) -> int:
    return sum(PERMISSIONS[name] for name in names)


def build_url(client_id: str, guild_id: str | None, permissions: int) -> str:
    query = {
        "client_id": client_id,
        "scope": "bot applications.commands",
        "permissions": str(permissions),
    }
    if guild_id:
        query["guild_id"] = guild_id
        query["disable_guild_select"] = "true"
    return "https://discord.com/oauth2/authorize?" + urlencode(query)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a ChaosX Discord bot invite URL.")
    parser.add_argument("client_id", help="Application/client ID from Discord Developer Portal")
    parser.add_argument("--guild-id", help="Optional Chaos Redux guild/server ID")
    parser.add_argument(
        "--profile",
        choices=["baseline", "exports", "playtest", "admin"],
        default="admin",
        help="Permission bundle to request",
    )
    args = parser.parse_args()

    names = {"baseline": BASELINE, "exports": EXPORTS, "playtest": PLAYTEST, "admin": ADMIN}[args.profile]
    permissions = permission_value(names)
    print("permissions=" + str(permissions))
    print("permissions_named=" + ",".join(names))
    print(build_url(args.client_id, args.guild_id, permissions))


if __name__ == "__main__":
    main()
