from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from .storage import Store


@dataclass(frozen=True)
class WebhookSummary:
    status: int
    text: str


def verify_github_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def summarize_github_event(event: str, payload: dict[str, Any]) -> tuple[str, str, str, str]:
    action = str(payload.get("action") or "")
    repo = payload.get("repository") or {}
    repo_name = repo.get("full_name") or "unknown/repo"
    if event == "pull_request":
        pr = payload.get("pull_request") or {}
        number = pr.get("number") or payload.get("number") or "?"
        title = pr.get("title") or "untitled"
        url = pr.get("html_url") or ""
        draft = pr.get("draft")
        merged = pr.get("merged")
        state = pr.get("state")
        card_key = f"github:pr:{repo_name}:{number}"
        summary = f"PR #{number} {action}: {title}"
        body = f"Repository: `{repo_name}`\nPR: #{number} `{title}`\nAction: `{action}`\nState: `{state}` draft=`{draft}` merged=`{merged}`\n<{url}>"
        return action, card_key, summary, body
    if event in {"workflow_run", "check_run", "check_suite"}:
        run = payload.get("workflow_run") or payload.get("check_run") or payload.get("check_suite") or {}
        name = run.get("name") or run.get("workflow", {}).get("name") or event
        conclusion = run.get("conclusion") or run.get("status") or "unknown"
        url = run.get("html_url") or ""
        branch = run.get("head_branch") or "unknown"
        card_key = f"github:ci:{repo_name}:{name}:{branch}"
        summary = f"CI {name} {conclusion} on {branch}"
        body = f"Repository: `{repo_name}`\nWorkflow/check: `{name}`\nBranch: `{branch}`\nStatus: `{conclusion}`\n<{url}>"
        return action, card_key, summary, body
    if event == "release":
        rel = payload.get("release") or {}
        tag = rel.get("tag_name") or "unknown"
        url = rel.get("html_url") or ""
        card_key = f"github:release:{repo_name}:{tag}"
        summary = f"Release {tag} {action}"
        body = f"Repository: `{repo_name}`\nRelease: `{tag}`\nAction: `{action}`\n<{url}>"
        return action, card_key, summary, body
    if event == "push":
        ref = payload.get("ref") or "unknown"
        after = payload.get("after") or "unknown"
        commits = payload.get("commits") or []
        card_key = f"github:push:{repo_name}:{after}"
        summary = f"Push to {ref}: {len(commits)} commit(s)"
        body = f"Repository: `{repo_name}`\nRef: `{ref}`\nCommit: `{str(after)[:12]}`\nCommits: `{len(commits)}`"
        return action, card_key, summary, body
    card_key = f"github:{event}:{repo_name}:{payload.get('sender', {}).get('id', 'unknown')}:{action}"
    summary = f"GitHub {event} {action} for {repo_name}".strip()
    return action, card_key, summary, f"Repository: `{repo_name}`\nEvent: `{event}`\nAction: `{action or 'none'}`"


class GitHubWebhookServer:
    def __init__(self, *, store: Store, secret: str, host: str, port: int):
        self.store = store
        self.secret = secret
        self.host = host
        self.port = port
        self.runner: web.AppRunner | None = None

    async def start(self) -> None:
        if not self.secret:
            return
        app = web.Application()
        app.router.add_get("/health", self.health)
        app.router.add_post("/github", self.github)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "chaosx-webhooks"})

    async def github(self, request: web.Request) -> web.Response:
        body = await request.read()
        delivery = request.headers.get("X-GitHub-Delivery") or ""
        event = request.headers.get("X-GitHub-Event") or ""
        signature = request.headers.get("X-Hub-Signature-256")
        if not verify_github_signature(self.secret, body, signature):
            return web.json_response({"ok": False, "error": "invalid signature"}, status=401)
        if not delivery or not event:
            return web.json_response({"ok": False, "error": "missing delivery/event"}, status=400)
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        action, card_key, summary, card_body = summarize_github_event(event, payload)
        inserted = await self.store.record_github_delivery(delivery_id=delivery, event=event, action=action, status="received", summary=summary)
        if not inserted:
            return web.json_response({"ok": True, "duplicate": True})
        await self.store.upsert_card(card_key=card_key, destination="configured-discord-channel", title=summary, body=card_body, source_url="")
        return web.json_response({"ok": True, "event": event, "action": action, "summary": summary})
