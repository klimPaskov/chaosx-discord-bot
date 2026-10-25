"""Owner-only server management in plain language.

Hoops, 2026-10-24: "delete messages up to this message", "timeout this user", "ban this user", "create a new
channel named ghlgghal", "create a new role", "change permissions in the channel" — said to ChaosX in normal
words. The model turns the sentence into one JSON action, this module validates it hard and then performs it
with discord.py; anything it cannot validate is refused instead of half-done.

Nothing here is reachable by non-owners: `bot.py` only calls `try_handle*` when the author is the owner.

Deliberate limits, because a wrong guess is destructive:
- one action per message (a sentence full of commands becomes a question back),
- irreversible actions (ban, kick, delete channel/role, purge) ask for a ✅ before running,
- every action is audited through `bot.store.audit`.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import discord

from .ask_api import direct_chat_completion_stream

MAX_PURGE = 1000
MAX_TIMEOUT_MINUTES = 60 * 24 * 28  # Discord's ceiling for a timeout
IRREVERSIBLE = {"ban", "kick", "delete_channel", "delete_role", "purge"}

# A cheap prefilter so a normal question never costs a planning round-trip.
_MANAGEMENT_HINTS = re.compile(
    r"(?i)\b("
    r"delete|purge|clean ?up|remove (?:the )?messages?|timeout|untimeout|mute|unmute|kick|ban|unban|"
    r"create (?:a |the )?(?:new )?(?:channel|category|role|thread)|new (?:channel|category|role)|"
    r"make (?:a |the )?(?:new )?(?:channel|category|role)|rename (?:the )?|delete (?:the )?(?:channel|role)|"
    r"permissions?|lock|unlock|slow ?mode|add role|remove role|give (?:him|her|them|@)|"
    r"role for|move (?:the )?(?:channel|messages?)"
    r")\b"
)

# name → (discord permission attribute, what to call it back to the owner)
PERMISSIONS: dict[str, str] = {
    "view_channel": "see the channel",
    "send_messages": "send messages",
    "read_message_history": "read history",
    "attach_files": "attach files",
    "embed_links": "embed links",
    "add_reactions": "add reactions",
    "use_external_emojis": "use external emojis",
    "mention_everyone": "mention everyone",
    "manage_messages": "manage messages",
    "manage_channels": "manage channels",
    "manage_roles": "manage roles",
    "manage_webhooks": "manage webhooks",
    "create_public_threads": "create public threads",
    "create_private_threads": "create private threads",
    "send_messages_in_threads": "send messages in threads",
    "connect": "connect to voice",
    "speak": "speak",
    "stream": "stream",
    "use_voice_activation": "use voice activation",
}

COLOR_NAMES = {
    "red": 0xE74C3C, "orange": 0xE67E22, "yellow": 0xF1C40F, "green": 0x2ECC71,
    "blue": 0x3498DB, "purple": 0x9B59B6, "pink": 0xE91E63, "black": 0x010101,
    "white": 0xFFFFFF, "grey": 0x95A5A6, "gray": 0x95A5A6, "gold": 0xD4AF37,
    "teal": 0x1ABC9C, "blurple": 0x5865F2, "default": None,
}

ACTION_CATALOG = """\
- purge: delete messages. args: count (int 1-1000, default 100), until_message_id (string, the message to stop
  at, inclusive — use the id given in CONTEXT when the owner says "up to this message"), from_user_id (string,
  only that member's messages), contains (string, only messages containing this text).
- timeout: args user (id or name), minutes (int 1-40320), reason (string, optional).
- untimeout: args user.
- kick: args user, reason (optional).
- ban: args user, delete_message_days (int 0-7, default 0), reason (optional).
- unban: args user (id or name).
- create_channel: args name, kind ("text" | "voice" | "category", default "text"), category (name or id,
  optional), topic (string, optional), private (bool, default false).
- delete_channel: args channel (name or id; omit for the current channel).
- rename_channel: args channel (name or id, optional), name (new name).
- lock_channel: args channel (optional) — removes send permission from @everyone.
- unlock_channel: args channel (optional) — gives send permission back to @everyone.
- slowmode: args channel (optional), seconds (int 0-21600).
- set_channel_permissions: args channel (optional), role (name or id) or user (name or id), allow (list of
  permission names), deny (list of permission names).
- create_role: args name, color (hex like "#ff8800" or a name like "red", optional), hoist (bool),
  mentionable (bool), permissions (list of permission names).
- delete_role: args role (name or id).
- add_role: args user, role.
- remove_role: args user, role.
"""

PLANNER_SYSTEM = f"""You turn a Discord server owner's request into exactly one JSON object, or refuse.

Supported actions and arguments:
{ACTION_CATALOG}

Reply ONLY with JSON:
{{"action": "<one of the actions above>", "args": {{...}}, "say": "<short description of what you will do>"}}

Valid permission names (use exactly these): {", ".join(sorted(PERMISSIONS))}.

Rules:
- If the request is not a server-management request, reply {{"action": "none"}}.
- If it contains more than one action, reply {{"action": "multiple"}} — the owner will be asked to send them
  one at a time.
- Never invent ids. Use exactly the ids and names given in CONTEXT. "this message", "this user" and "here"
  refer to CONTEXT's referenced message, mentioned member and current channel.
- Omit arguments you were not told and that have a default. Keep reasons short and factual.
"""


@dataclass
class Plan:
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    say: str = ""

    @property
    def needs_confirmation(self) -> bool:
        return self.action in IRREVERSIBLE


class PlanError(Exception):
    """The request matched a management action but could not be turned into a safe one."""


def looks_like_management(text: str) -> bool:
    return bool(_MANAGEMENT_HINTS.search(text or ""))


def _json_object(raw: str) -> dict[str, Any] | None:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


CATALOG_ACTIONS = {
    line.split(":")[0].strip("- ").strip()
    for line in ACTION_CATALOG.splitlines()
    if line.startswith("- ")
}


def parse_plan(raw: str) -> "str | list[Plan] | None":
    """The string "none" for a refusal, a list of plans for an action request, None for junk."""
    data = _json_object(raw)
    if data is None:
        return None
    if str(data.get("action") or "").strip().lower() == "none":
        return "none"
    entries = data.get("actions")
    if entries is None:
        entries = [data]  # a single action with no wrapper
    if not isinstance(entries, list) or not entries:
        return None
    plans: list[Plan] = []
    for entry in entries[:5]:
        if not isinstance(entry, dict):
            continue
        action = str(entry.get("action") or "").strip().lower()
        if action not in CATALOG_ACTIONS:
            continue
        args = entry.get("args")
        plans.append(
            Plan(
                action=action,
                args=args if isinstance(args, dict) else {},
                say=str(entry.get("say") or ""),
            )
        )
    return plans or None


def _as_int(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _perm_names(names: Any) -> list[str]:
    if not names:
        return []
    if isinstance(names, str):
        names = [part for part in re.split(r"[,;]", names) if part.strip()]
    out: list[str] = []
    for name in names:
        key = str(name).strip().lower().replace(" ", "_").replace("-", "_")
        if key in {"all", "everything", "full"}:
            out.append("administrator")
            continue
        if key in PERMISSIONS:
            out.append(key)
    return out


def validate(plan: Plan) -> Plan:
    """Raise PlanError with something a human can act on; return a cleaned plan otherwise."""
    args = dict(plan.args)
    action = plan.action

    if action == "purge":
        count = _as_int(args.get("count"), 100)
        if count is None or count < 1:
            raise PlanError("How many messages should I delete? Give a number.")
        args["count"] = min(count, MAX_PURGE)
    elif action == "timeout":
        minutes = _as_int(args.get("minutes"), None)
        if minutes is None:
            raise PlanError("For how many minutes should I time them out?")
        if minutes < 1 or minutes > MAX_TIMEOUT_MINUTES:
            raise PlanError(f"A timeout has to be between 1 and {MAX_TIMEOUT_MINUTES} minutes.")
        args["minutes"] = minutes
        if not args.get("user"):
            raise PlanError("Who should I time out?")
    elif action in {"untimeout", "kick", "ban", "unban", "add_role", "remove_role"}:
        if not args.get("user"):
            raise PlanError("Who is this about? Name them or reply to their message.")
        if action == "ban":
            args["delete_message_days"] = max(0, min(_as_int(args.get("delete_message_days"), 0) or 0, 7))
    elif action == "create_channel":
        name = str(args.get("name") or "").strip()
        if not name:
            raise PlanError("What should the channel be called?")
        args["name"] = name[:100]
        args["kind"] = str(args.get("kind") or "text").strip().lower()
        if args["kind"] not in {"text", "voice", "category"}:
            args["kind"] = "text"
    elif action == "delete_channel":
        pass  # the current channel is a valid target
    elif action == "rename_channel":
        name = str(args.get("name") or "").strip()
        if not name:
            raise PlanError("What should the new channel name be?")
        args["name"] = name[:100]
    elif action in {"lock_channel", "unlock_channel"}:
        pass
    elif action == "slowmode":
        seconds = _as_int(args.get("seconds"), None)
        if seconds is None or seconds < 0 or seconds > 21600:
            raise PlanError("Slowmode takes a number of seconds between 0 and 21600.")
        args["seconds"] = seconds
    elif action == "set_channel_permissions":
        if not args.get("role") and not args.get("user"):
            raise PlanError("Whose permissions should I change — a role or a member?")
        allow = _perm_names(args.get("allow"))
        deny = _perm_names(args.get("deny"))
        if not allow and not deny:
            raise PlanError("Which permission should I change? Name it explicitly.")
        args["allow"], args["deny"] = allow, deny
    elif action == "create_role":
        name = str(args.get("name") or "").strip()
        if not name:
            raise PlanError("What should the role be called?")
        args["name"] = name[:100]
        args["permissions"] = _perm_names(args.get("permissions"))
        colour = args.get("color")
        if isinstance(colour, str) and colour.strip():
            hexish = colour.strip().lstrip("#")
            named = COLOR_NAMES.get(colour.strip().lower())
            if named is not None:
                args["color"] = named
            elif re.fullmatch(r"[0-9a-fA-F]{6}", hexish):
                args["color"] = int(hexish, 16)
            else:
                args["color"] = None
        elif isinstance(colour, int):
            args["color"] = max(0, min(colour, 0xFFFFFF))
        else:
            args["color"] = None
    elif action == "delete_role":
        if not args.get("role"):
            raise PlanError("Which role should I delete?")
    else:  # pragma: no cover - the parser only yields catalogued actions
        raise PlanError(f"I do not know how to `{action}`.")
    return Plan(action=action, args=args, say=plan.say)


async def _complete(bot: Any, *, system: str, prompt: str) -> str:
    settings = getattr(bot, "settings", None)
    chunks: list[str] = []
    async for _reasoning, content in direct_chat_completion_stream(
        system=system,
        user=prompt,
        model=getattr(settings, "operator_model", "") or getattr(settings, "ask_model", "") or "",
        provider=str(getattr(settings, "ask_provider", "") or ""),
    ):
        if content:
            chunks.append(content)
    return "".join(chunks).strip()


def context_block(
    *,
    guild: discord.Guild | None,
    channel: Any,
    referenced_message: Any = None,
    mentioned_members: list[Any] | None = None,
    channels: list[Any] | None = None,
) -> str:
    lines = [f"Guild: {guild.name if guild else 'unknown'}"]
    if channel is not None:
        lines.append(f"Current channel: #{getattr(channel, 'name', 'unknown')} (id {getattr(channel, 'id', '?')})")
    if channels:
        # Ids beat names: Discord renders names with emoji and pipes, and the owner types plain words.
        listing = ", ".join(f"{getattr(item, 'name', item)} (id {getattr(item, 'id', item)})" for item in channels[:80])
        lines.append(f"Channels: {listing}")
    members = mentioned_members or []
    if members:
        lines.append(
            "Mentioned members: "
            + ", ".join(f"{m.name} (id {m.id})" for m in members[:5])
        )
    if referenced_message is not None:
        author = getattr(referenced_message, "author", None)
        author_text = f"{author.name} (id {author.id})" if author is not None else "unknown"
        body = str(getattr(referenced_message, "content", "") or "")[:200]
        lines.append(
            f"Referenced message: id {getattr(referenced_message, 'id', '?')}, author {author_text}, "
            f"at {getattr(referenced_message, 'created_at', '?')}, content: {body!r}"
        )
    else:
        lines.append("Referenced message: none")
    return "\n".join(lines)


async def plan_from_text(bot: Any, text: str, *, context: str) -> Plan | str | None:
    prompt = f"CONTEXT:\n{context}\n\nREQUEST: {text}\n\nJSON:"
    try:
        raw = await asyncio.wait_for(_complete(bot, system=PLANNER_SYSTEM, prompt=prompt), timeout=30)
    except Exception as exc:
        print(f"[server_ops] planning failed: {type(exc).__name__}: {exc}", flush=True)
        return None
    return parse_plan(raw)


async def _member(guild: discord.Guild, target: Any) -> discord.Member:
    raw = str(target or "").strip().strip("<@!>")
    if raw.isdigit():
        member = guild.get_member(int(raw)) or await guild.fetch_member(int(raw))
        return member
    cleaned = raw.lstrip("@").lower()
    for member in guild.members:
        if member.name.lower() == cleaned or (member.display_name or "").lower() == cleaned:
            return member
    raise PlanError(f"I cannot find a member called `{raw}` in this server.")


_CHANNEL_NOISE = {"the", "and", "of", "channel", "chat", "text", "general"}


def _channel_tokens(name: str) -> set[str]:
    """Words that identify a channel, with emoji, pipes, dashes and filler removed."""
    text = str(name or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return {token for token in text.split() if len(token) > 2 and token not in _CHANNEL_NOISE}


def _role(guild: discord.Guild, target: Any) -> discord.Role:
    raw = str(target or "").strip().strip("<@&>")
    if raw.isdigit():
        role = guild.get_role(int(raw))
        if role is not None:
            return role
    cleaned = raw.lstrip("@").lower()
    if cleaned in {"everyone", "here", "all"}:
        return guild.default_role
    for role in guild.roles:
        if role.name.lstrip("@").lower() == cleaned:
            return role
    raise PlanError(f"I cannot find a role called `{raw}` in this server.")


def _channel(guild: discord.Guild, target: Any, current: Any) -> Any:
    raw = str(target or "").strip().strip("<#>")
    if raw.isdigit():
        found = guild.get_channel_or_thread(int(raw))
        if found is not None:
            return found
    if not raw:
        if current is None:
            raise PlanError("Which channel? I do not have one in view here.")
        return current
    cleaned = raw.lstrip("#").strip().lower()
    for channel in guild.channels:
        if channel.name.lower() == cleaned:
            return channel
    # Hoops types "jazz and conversation"; Discord shows "💬┃jazz-and-conversation". Compare the words.
    wanted = _channel_tokens(cleaned)
    if wanted:
        best: tuple[int, Any] | None = None
        ambiguous = False
        for channel in guild.channels:
            have = _channel_tokens(channel.name)
            if not have or not wanted.issubset(have):
                continue
            score = len(have & wanted)
            if best is None or score > best[0]:
                best, ambiguous = (score, channel), False
            elif score == best[0]:
                ambiguous = True
        if best is not None and not ambiguous:
            return best[1]
        if best is not None and ambiguous:
            raise PlanError(f"More than one channel matches `{raw}` — send the channel link.")
    raise PlanError(f"I cannot find a channel called `{raw}` in this server.")


def _permissions(names: list[str]) -> discord.Permissions:
    perms = discord.Permissions.none()
    for name in names:
        if name == "administrator":
            perms.administrator = True
        elif hasattr(perms, name):
            setattr(perms, name, True)
    return perms


def _describe_permissions(names: list[str]) -> str:
    return ", ".join(PERMISSIONS.get(name, name).replace("_", " ") for name in names) or "nothing"


def _colour_name(value: int | None) -> str:
    return f"#{value:06x}" if value else "no colour"


async def execute(bot: Any, plan: Plan, *, guild: discord.Guild, channel: Any, author_id: int) -> str:
    """Run the plan. Raises PlanError for a refusal and discord.HTTPException for a failed API call."""
    args = plan.args
    action = plan.action
    reason = f"ChaosX server management by {author_id}"

    if action == "purge":
        channel = _channel(guild, args.get("channel"), channel)
        if channel is None:
            raise PlanError("I need a channel to delete messages in.")
        count = int(args.get("count") or 100)
        until_id = args.get("until_message_id")
        kwargs: dict[str, Any] = {"limit": count, "reason": reason}
        if until_id:
            try:
                boundary = await channel.fetch_message(int(str(until_id).strip()))
            except (discord.NotFound, discord.HTTPException, ValueError):
                raise PlanError("I cannot see the message you mean — reply to it and try again.")
            kwargs["after"] = boundary
        if args.get("from_user_id"):
            member = await _member(guild, args["from_user_id"])
            kwargs["check"] = lambda m, member=member: m.author.id == member.id
        elif args.get("contains"):
            needle = str(args["contains"]).lower()
            kwargs["check"] = lambda m, needle=needle: needle in (m.content or "").lower()
        if not until_id:
            deleted = await channel.purge(**kwargs)
            return f"Deleted {len(deleted)} message(s) in #{getattr(channel, 'name', 'this channel')}."
        # "From latest up to that message" is unbounded: Discord deletes 100 at a time, so keep going until the
        # boundary is reached, then delete the boundary message itself.
        boundary = kwargs.pop("after")
        kwargs.pop("limit", None)
        total = 0
        while total < MAX_PURGE:
            batch_limit = min(100, MAX_PURGE - total)
            batch = await channel.purge(limit=batch_limit, after=boundary, reason=reason, check=kwargs.get("check"))
            total += len(batch)
            if len(batch) < batch_limit:
                break
        try:
            await boundary.delete()
            total += 1
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        return f"Deleted {total} message(s) in #{getattr(channel, 'name', 'this channel')}, up to and including the one you linked."

    if action == "set_channel_permissions":
        target_channel = _channel(guild, args.get("channel"), channel)
        overwrite = target_channel.overwrites_for(
            _role(guild, args["role"]) if args.get("role") else await _member(guild, args["user"])
        )
        for name in args.get("allow") or []:
            setattr(overwrite, name, True)
        for name in args.get("deny") or []:
            setattr(overwrite, name, False)
        subject = (
            (args["role"] if str(args["role"]).startswith("@") else f"@{args['role']}")
            if args.get("role")
            else f"{args['user']}"
        )
        await target_channel.set_permissions(
            _role(guild, args["role"]) if args.get("role") else await _member(guild, args["user"]),
            overwrite=overwrite,
            reason=reason,
        )
        changes: list[str] = []
        if args.get("allow"):
            changes.append(f"allow {_describe_permissions(args['allow'])}")
        if args.get("deny"):
            changes.append(f"deny {_describe_permissions(args['deny'])}")
        return f"Updated #{target_channel.name} for {subject}: {'; '.join(changes)}."

    if action == "create_channel":
        kind = args.get("kind") or "text"
        parent = None
        if args.get("category"):
            parent = _channel(guild, args["category"], None)
            if not isinstance(parent, discord.CategoryChannel):
                parent = None
        if kind == "category":
            created = await guild.create_category(args["name"], reason=reason)
        elif kind == "voice":
            created = await guild.create_voice_channel(args["name"], category=parent, reason=reason)
        else:
            overwrites = {}
            if args.get("private") and guild.default_role is not None:
                overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
            text_kwargs: dict[str, Any] = {"category": parent, "overwrites": overwrites, "reason": reason}
            if args.get("topic"):
                text_kwargs["topic"] = str(args["topic"])
            created = await guild.create_text_channel(args["name"], **text_kwargs)
        return f"Created {kind} `{created.name}` (<#{created.id}>)." if hasattr(created, "mention") else f"Created `{created.name}`."

    if action == "delete_channel":
        target_channel = _channel(guild, args.get("channel"), channel)
        name = target_channel.name
        await target_channel.delete(reason=reason)
        return f"Deleted channel `{name}`."

    if action == "rename_channel":
        target_channel = _channel(guild, args.get("channel"), channel)
        old = target_channel.name
        await target_channel.edit(name=args["name"], reason=reason)
        return f"Renamed `{old}` to `{args['name']}`."

    if action in {"lock_channel", "unlock_channel"}:
        target_channel = _channel(guild, args.get("channel"), channel)
        overwrite = target_channel.overwrites_for(guild.default_role)
        can_send = action == "unlock_channel"
        overwrite.send_messages = can_send
        await target_channel.set_permissions(guild.default_role, overwrite=overwrite, reason=reason)
        return f"{'Unlocked' if can_send else 'Locked'} #{target_channel.name} for @everyone."

    if action == "slowmode":
        target_channel = _channel(guild, args.get("channel"), channel)
        await target_channel.edit(slowmode_delay=int(args["seconds"]), reason=reason)
        return f"Slowmode in #{target_channel.name} is now {args['seconds']}s."

    if action == "create_role":
        role = await guild.create_role(
            name=args["name"],
            colour=discord.Colour(args["color"]) if args.get("color") else discord.Colour.default(),
            hoist=bool(args.get("hoist")),
            mentionable=bool(args.get("mentionable")),
            permissions=_permissions(args.get("permissions") or []),
            reason=reason,
        )
        extra = ""
        if args.get("permissions"):
            extra = f" with {_describe_permissions(args['permissions'])}"
        return f"Created role `{role.name}`{extra} ({_colour_name(args.get('color'))})."

    if action == "delete_role":
        role = _role(guild, args["role"])
        if role.is_default() or role.managed:
            raise PlanError("I cannot delete @everyone or a role managed by an integration.")
        name = role.name
        await role.delete(reason=reason)
        return f"Deleted role `{name}`."

    if action in {"add_role", "remove_role"}:
        member = await _member(guild, args["user"])
        role = _role(guild, args["role"])
        if role.is_default() or role.managed:
            raise PlanError("That role cannot be assigned by hand.")
        if action == "add_role":
            await member.add_roles(role, reason=reason)
            return f"Gave {member.name} the role `{role.name}`."
        await member.remove_roles(role, reason=reason)
        return f"Removed the role `{role.name}` from {member.name}."

    if action in {"timeout", "untimeout"}:
        member = await _member(guild, args["user"])
        if action == "timeout":
            minutes = int(args["minutes"])
            await member.timeout(timedelta(minutes=minutes), reason=reason)
            return f"Timed out {member.name} for {minutes} minute(s)."
        await member.timeout(None, reason=reason)
        return f"Removed the timeout from {member.name}."

    if action in {"kick", "ban", "unban"}:
        if action == "unban":
            user = discord.Object(id=int(str(args["user"]).strip().strip("<@!>")))
            await guild.unban(user, reason=reason)
            return f"Unbanned `{args['user']}`."
        member = await _member(guild, args["user"])
        name = member.name
        if action == "kick":
            await member.kick(reason=reason)
            return f"Kicked {name}."
        await member.ban(reason=reason, delete_message_days=int(args.get("delete_message_days") or 0))
        return f"Banned {name}."

    raise PlanError(f"I do not know how to `{action}`.")


def describe(plan: Plan) -> str:
    """One line the owner can read before confirming."""
    args = plan.args
    action = plan.action
    if action == "purge":
        where = f"up to message {args['until_message_id']}" if args.get("until_message_id") else f"last {args.get('count', 100)}"
        extra = ""
        if args.get("from_user_id"):
            extra = f" from {args['from_user_id']}"
        if args.get("contains"):
            extra += f" containing {args['contains']!r}"
        return f"Delete messages {where}{extra}"
    if action == "ban":
        return f"Ban {args.get('user')}" + (f" and delete {args['delete_message_days']}d of messages" if args.get("delete_message_days") else "")
    if action == "kick":
        return f"Kick {args.get('user')}"
    if action == "delete_channel":
        return f"Delete channel {args.get('channel') or '(this one)'}"
    if action == "delete_role":
        return f"Delete role {args.get('role')}"
    return plan.say or action.replace("_", " ")


def describe_all(plans: list[Plan]) -> str:
    if len(plans) == 1:
        return describe(plans[0])
    return "\n".join(f"{index}. {describe(plan)}" for index, plan in enumerate(plans, start=1))
