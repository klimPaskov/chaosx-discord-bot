#!/usr/bin/env python3
"""Wire the live-data block into every ask path.

Every path that passed `server_facts=` now passes a merged block (identity facts when relevant + LIVE state when
the question asks about current state), so the bot can answer "how many members", "what's open on GitHub",
"what's in testing", "who's at a warning count" from live sources instead of refusing.
"""
from pathlib import Path

target = Path("src/chaosx_bot/bot.py")
text = target.read_text()
applied: list[str] = []
skipped: list[str] = []


def sub(old: str, new: str, label: str, count: int = 1) -> None:
    global text
    hits = text.count(old)
    if hits < count:
        skipped.append(f"{label}: anchor found {hits}x (needed {count})")
        return
    text = text.replace(old, new, count)
    applied.append(f"{label} ({count})")


# 1. the two new methods, right after server_facts_for_request
sub(
    '''        if request_needs_server_facts(request, self._SERVER_FACTS_LOOKUP_TERMS):
            return self.server_facts_block()
        return ""

    def members_block(self) -> str:''',
    '''        if request_needs_server_facts(request, self._SERVER_FACTS_LOOKUP_TERMS):
            return self.server_facts_block()
        return ""

    async def live_facts_for_request(self, request: str, *, requester_id: int = 0) -> str:
        """LIVE state (activity, GitHub, playtests, moderation) only when the ask is about current state."""
        from . import live_facts

        if not live_facts.wants_live_facts(request):
            return ""
        try:
            return await live_facts.collect(self, requester_id=requester_id)
        except Exception:
            return ""

    async def facts_block_for_request(
        self, request: str, *, requester_id: int = 0, always_identity: bool = False
    ) -> str:
        """Identity facts plus live state for one ask. Never let either half break the answer."""
        identity = self.server_facts_block() if always_identity else self.server_facts_for_request(request)
        live = await self.live_facts_for_request(request, requester_id=requester_id)
        return "\\n\\n".join(part for part in (identity, live) if part)

    def members_block(self) -> str:''',
    "bot.py: live_facts_for_request/facts_block_for_request",
)

# 2. auto-scan answer + banter paths
sub(
    "            server_facts=bot.server_facts_for_request(user_message),",
    "            server_facts=await bot.facts_block_for_request(user_message, requester_id=message.author.id),",
    "bot.py: auto-scan answer/banter",
    count=2,
)

# 3. the two voice/lookup paths that already have user_id
sub(
    """        user_context=await bot.user_context_for(user_id),
""",
    """        user_context=await bot.user_context_for(user_id),
""",
    "bot.py: voice paths (context unchanged)",
)

sub(
    "        server_facts=bot.server_facts_for_request(request),\n",
    "        server_facts=await bot.facts_block_for_request(request, requester_id=user_id),\n",
    "bot.py: lookup paths using user_id",
    count=2,
)

# 4. the path that has the message object
sub(
    """        user_context=await bot.user_context_for(message.author.id, exclude_message_id=message.id),""",
    """        user_context=await bot.user_context_for(message.author.id, exclude_message_id=message.id),""",
    "bot.py: message path (context unchanged)",
)
sub(
    """        server_facts=bot.server_facts_for_request(request),
        known_users=""",
    """        server_facts=await bot.facts_block_for_request(request, requester_id=message.author.id),
        known_users=""",
    "bot.py: message path using message.author.id",
)

# 5. public ask command
sub(
    "            server_facts=bot.server_facts_for_request(request),\n            known_users=known_users,",
    "            server_facts=await bot.facts_block_for_request(\n"
    "                request, requester_id=interaction.user.id\n"
    "            ),\n            known_users=known_users,",
    "bot.py: /ask public path",
)

# 6. owner path: identity always plus live state
sub(
    "            server_facts=bot.server_facts_block(),\n"
    "            model_name=bot.settings.operator_model",
    "            server_facts=await bot.facts_block_for_request(\n"
    "                owner_request, requester_id=interaction.user.id, always_identity=True\n"
    "            ),\n            model_name=bot.settings.operator_model",
    "bot.py: owner path",
)

target.write_text(text)
print("applied:")
for item in applied:
    print("  -", item)
if skipped:
    print("SKIPPED (fix before trusting this patch):")
    for item in skipped:
        print("  -", item)
remaining = text.count("server_facts=bot.server_facts_for_request(")
print(f"remaining old-form call sites: {remaining}")
