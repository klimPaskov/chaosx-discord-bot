"""Owner-requested server actions: plan in plain English, confirm, then execute.

The owner types a request in normal language; the model maps it onto one of a small whitelist of
Discord actions and returns strict JSON. Nothing executes from that plan — the plan is stored with
an id, shown back for review, and only runs when the owner confirms it by id. Two consequences worth
keeping:

* the confirmed action is the one that was reviewed (the plan is data, not a re-prompt), and
* an unparsable or unsupported request executes nothing at all.

The whitelist is deliberately small and non-destructive: create a scheduled event, set a channel
topic, open a thread, post a message, pin a message, grant/revoke a role. No bans, kicks, deletes
or permission edits.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .routine_posts import sanitize_post

MAX_PARAM_TEXT = 900
MAX_REASON_CHARS = 200


@dataclass(frozen=True)
class ParamSpec:
    name: str
    kind: str  # text | int | channel | member | role | iso | message_id
    required: bool = True
    label: str = ""


@dataclass(frozen=True)
class ActionSpec:
    name: str
    label: str
    params: tuple[ParamSpec, ...]
    danger: str = "low"  # informational only; nothing here is destructive


ACTION_SPECS: tuple[ActionSpec, ...] = (
    ActionSpec(
        name="create_scheduled_event",
        label="Create a Discord scheduled event",
        params=(
            ParamSpec("name", "text"),
            ParamSpec("start", "iso"),
            ParamSpec("duration_minutes", "int", required=False),
            ParamSpec("description", "text", required=False),
            ParamSpec("voice_channel", "channel", required=False),
            ParamSpec("location", "text", required=False),
        ),
    ),
    ActionSpec(
        name="update_channel_topic",
        label="Update a channel topic",
        params=(ParamSpec("channel", "channel"), ParamSpec("topic", "text")),
    ),
    ActionSpec(
        name="create_thread",
        label="Open a thread",
        params=(
            ParamSpec("channel", "channel"),
            ParamSpec("name", "text"),
            ParamSpec("message", "text", required=False),
        ),
    ),
    ActionSpec(
        name="post_message",
        label="Post a message",
        params=(ParamSpec("channel", "channel"), ParamSpec("text", "text")),
    ),
    ActionSpec(
        name="pin_message",
        label="Pin a message",
        params=(ParamSpec("channel", "channel"), ParamSpec("message_id", "message_id")),
    ),
    ActionSpec(
        name="grant_role",
        label="Grant a role to a member",
        params=(ParamSpec("member", "member"), ParamSpec("role", "role")),
    ),
    ActionSpec(
        name="revoke_role",
        label="Remove a role from a member",
        params=(ParamSpec("member", "member"), ParamSpec("role", "role")),
    ),
)

ACTIONS = {spec.name: spec for spec in ACTION_SPECS}


@dataclass
class ActionPlan:
    plan_id: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    request: str = ""
    created_at: str = ""

    @property
    def label(self) -> str:
        spec = ACTIONS.get(self.action)
        return spec.label if spec else self.action


def plan_id_for(*, request: str, action: str, params: dict[str, Any]) -> str:
    raw = json.dumps({"request": request.strip().lower(), "action": action, "params": params}, sort_keys=True)
    return "plan-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _extract_json(text: str) -> str:
    """Pull the first JSON object out of a model reply (tolerates fences and prose)."""
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return ""
    return cleaned[start : end + 1]


def _clean_text(value: Any, *, max_chars: int = MAX_PARAM_TEXT) -> str:
    return sanitize_post(str(value or "").strip(), max_chars=max_chars)


def parse_action_plan(raw: str, *, request: str = "") -> tuple[ActionPlan | None, str]:
    """Parse strict JSON into a validated plan. Returns (plan, error_message)."""
    payload = _extract_json(raw)
    if not payload:
        return None, "the model did not return a JSON plan"
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None, "the model returned invalid JSON"
    if not isinstance(data, dict):
        return None, "the plan was not a JSON object"

    action = str(data.get("action") or "").strip()
    if not action:
        return None, "no action was proposed"
    spec = ACTIONS.get(action)
    if spec is None:
        return None, f"unsupported action `{action}` (allowed: {', '.join(sorted(ACTIONS))})"

    raw_params_value = data.get("params")
    raw_params: dict[str, Any] = raw_params_value if isinstance(raw_params_value, dict) else {}
    params: dict[str, Any] = {}
    for param in spec.params:
        value = raw_params.get(param.name)
        if value is None or (isinstance(value, str) and not value.strip()):
            if param.required:
                return None, f"`{action}` is missing the required `{param.name}` parameter"
            continue
        if param.kind == "int":
            try:
                params[param.name] = int(value)
            except (TypeError, ValueError):
                return None, f"`{param.name}` must be a whole number"
        elif param.kind == "text":
            cleaned = _clean_text(value)
            if not cleaned:
                return None, f"`{param.name}` was empty after sanitizing"
            params[param.name] = cleaned
        else:
            params[param.name] = str(value).strip()

    # Names must be resolvable before execution; the caller resolves them against the guild.
    reason = _clean_text(data.get("reason") or "", max_chars=MAX_REASON_CHARS)
    plan = ActionPlan(
        plan_id=plan_id_for(request=request, action=action, params=params),
        action=action,
        params=params,
        reason=reason,
        request=request.strip(),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    return plan, ""


def build_plan_prompt(
    *,
    request: str,
    channel_names: list[str],
    role_names: list[str],
    member_names: list[str],
    max_members: int = 40,
) -> str:
    channels = ", ".join(sorted(channel_names)) or "(none known)"
    roles = ", ".join(sorted(role_names)) or "(none known)"
    members = ", ".join(sorted(member_names)[:max_members]) or "(none known)"
    actions = "\n".join(
        f"- `{spec.name}` ({spec.label}): "
        + ", ".join(
            f"{param.name}:{param.kind}{'' if param.required else '?'}" for param in spec.params
        )
        for spec in ACTION_SPECS
    )
    return f"""Map Hoops' request onto exactly one ChaosX server action and return strict JSON.

Request: {request}

Available actions:
{actions}

Known channels: {channels}
Known roles: {roles}
Known members: {members}

Rules:
- Return ONLY a JSON object, no prose, no code fences:
  {{"action": "<name>", "params": {{...}}, "reason": "<one short line>"}}
- Use exact names as listed above for channel/member/role values.
- `start` must be an ISO-8601 timestamp with timezone (Hoops' local time is UTC+3 unless stated).
  `duration_minutes` and `message_id` are numbers-as-strings are fine; keep them plain.
- Never include @everyone/@here/role pings in any text parameter.
- If the request does not map to one of these actions, return {{"action": "", "params": {{}}, "reason": "..."}}
  and explain in the reason what is unsupported.
- Choose the single most likely action; do not invent new actions or parameters."""


def describe_plan(plan: ActionPlan, *, resolvers: dict[str, dict[str, str]] | None = None) -> str:
    """Human-readable plan for the confirmation card."""
    spec = ACTIONS.get(plan.action)
    lines = [f"**{plan.label}** (`{plan.action}`)"]
    if spec:
        for param in spec.params:
            if param.name in plan.params:
                value = plan.params[param.name]
                if param.kind == "channel":
                    resolved = (resolvers or {}).get("channel", {}).get(str(value))
                    value = f"#{value}" if resolved else f"unresolved channel `{value}`"
                elif param.kind == "member":
                    resolved = (resolvers or {}).get("member", {}).get(str(value))
                    value = f"{value} (id {resolved})" if resolved else f"unresolved member `{value}`"
                elif param.kind == "role":
                    resolved = (resolvers or {}).get("role", {}).get(str(value))
                    value = f"{value} (id {resolved})" if resolved else f"unresolved role `{value}`"
                lines.append(f"- {param.label or param.name}: {value}")
    if plan.reason:
        lines.append(f"- why: {plan.reason}")
    return "\n".join(lines)


def unresolvable_params(plan: ActionPlan, *, resolvers: dict[str, dict[str, str]]) -> list[str]:
    """Which referenced channels/members/roles do not exist in the guild (blocking errors)."""
    spec = ACTIONS.get(plan.action)
    if spec is None:
        return [f"unsupported action `{plan.action}`"]
    problems: list[str] = []
    for param in spec.params:
        if param.name not in plan.params:
            continue
        value = str(plan.params[param.name])
        if param.kind in {"channel", "member", "role"}:
            if value not in resolvers.get(param.kind, {}):
                problems.append(f"{param.label or param.name} `{value}` was not found in this server")
    return problems


def plan_detail(plan: ActionPlan) -> str:
    return json.dumps(
        {"action": plan.action, "params": plan.params, "reason": plan.reason, "request": plan.request},
        ensure_ascii=False,
    )[:4000]
