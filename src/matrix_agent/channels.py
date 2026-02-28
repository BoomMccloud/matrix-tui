"""Channel adapters — ingest tasks from external sources (GitHub, etc.)."""

import hashlib
import hmac
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiohttp import web

log = logging.getLogger(__name__)


@dataclass
class Task:
    task_id: str
    description: str
    repo: str | None = None
    issue_number: int | None = None
    source: str = ""


class ChannelAdapter(ABC):
    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def deliver_result(self, task_id: str, result: str) -> None: ...

    @abstractmethod
    async def deliver_error(self, task_id: str, error: str) -> None: ...


class GitHubChannel(ChannelAdapter):
    def __init__(self, submit_task: Callable[[Task], Awaitable[Any]], settings: Any):
        self.submit_task = submit_task
        self.settings = settings
        self._runner: web.AppRunner | None = None

    def _make_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/webhook/github", self._handle_webhook)
        return app

    async def start(self) -> None:
        app = self._make_app()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.settings.github_webhook_port)
        await site.start()
        log.info("GitHub webhook listening on port %s", self.settings.github_webhook_port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    async def deliver_result(self, task_id: str, result: str) -> None:
        pass  # TODO: post comment on GitHub issue

    async def deliver_error(self, task_id: str, error: str) -> None:
        pass  # TODO: post error comment on GitHub issue

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        body = await request.read()
        secret = self.settings.github_webhook_secret

        if secret:
            sig_header = request.headers.get("X-Hub-Signature-256", "")
            if not sig_header:
                return web.Response(status=401, text="Missing signature")
            expected = "sha256=" + hmac.new(
                secret.encode(), body, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(sig_header, expected):
                return web.Response(status=401, text="Invalid signature")

        payload = json.loads(body)

        if payload.get("action") != "labeled":
            return web.Response(status=200, text="Ignored")

        label_name = payload.get("label", {}).get("name", "")
        if label_name != "agent-task":
            return web.Response(status=200, text="Ignored")

        issue = payload["issue"]
        repo = payload["repository"]["full_name"]
        task = Task(
            task_id=f"gh-{issue['number']}",
            description=f"{issue['title']}\n\n{issue.get('body', '')}",
            repo=repo,
            issue_number=issue["number"],
            source="github",
        )

        await self.submit_task(task)
        return web.Response(status=202, text="Accepted")
