"""Channel adapters — ingest tasks from external sources (GitHub, etc.)."""

import hashlib
import hmac
import json
import logging
import asyncio
from abc import ABC, abstractmethod

from aiohttp import web
from .decider import GITHUB_SYSTEM_PROMPT

log = logging.getLogger(__name__)


class ChannelAdapter(ABC):
    system_prompt: str = ""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send_update(self, task_id: str, text: str) -> None: ...

    @abstractmethod
    async def deliver_result(self, task_id: str, text: str) -> None: ...

    @abstractmethod
    async def deliver_error(self, task_id: str, error: str) -> None: ...

    @abstractmethod
    async def is_valid(self, task_id: str) -> bool: ...

    async def recover_tasks(self) -> list[tuple[str, str]]:
        """Return (task_id, message) pairs to re-enqueue after restart."""
        return []


class GitHubChannel(ChannelAdapter):
    system_prompt = GITHUB_SYSTEM_PROMPT

    def __init__(self, task_runner, settings):
        self.task_runner = task_runner
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

    async def send_update(self, task_id: str, text: str) -> None:
        # No-op for GitHub — avoid spamming issues with intermediate output
        pass

    async def deliver_result(self, task_id: str, text: str) -> None:
        issue_number = task_id.split("-", 1)[1]
        body = f"✅ Completed — {text}"
        proc = await asyncio.create_subprocess_exec(
            "gh", "issue", "comment", issue_number, "--body", body,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.error("gh issue comment failed for #%s: %s", issue_number, stderr.decode())
            return

        proc = await asyncio.create_subprocess_exec(
            "gh", "issue", "close", issue_number,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.error("gh issue close failed for #%s: %s", issue_number, stderr.decode())

    async def deliver_error(self, task_id: str, error: str) -> None:
        issue_number = task_id.split("-", 1)[1]
        body = f"❌ Failed: {error}"
        proc = await asyncio.create_subprocess_exec(
            "gh", "issue", "comment", issue_number, "--body", body,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.error("gh issue comment (error) failed for #%s: %s", issue_number, stderr.decode())

    async def is_valid(self, task_id: str) -> bool:
        """Check if the issue is still open with the agent-task label."""
        issue_number = task_id.split("-", 1)[1]
        proc = await asyncio.create_subprocess_exec(
            "gh", "issue", "view", issue_number, "--json", "state,labels",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return False
        data = json.loads(stdout)
        if data.get("state") != "OPEN":
            return False
        labels = [lb["name"] for lb in data.get("labels", [])]
        return "agent-task" in labels

    async def recover_tasks(self) -> list[tuple[str, str]]:
        """Scan for open agent-task issues to resume after restart."""
        repo = self.settings.github_repo
        if not repo:
            log.warning("github_repo not set — skipping crash recovery for GitHub tasks")
            return []

        proc = await asyncio.create_subprocess_exec(
            "gh", "issue", "list",
            "--repo", repo,
            "--label", "agent-task",
            "--state", "open",
            "--json", "number,title,body",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.error("gh issue list failed: %s", stderr.decode())
            return []

        issues = json.loads(stdout)
        results = []
        for issue in issues:
            number = issue["number"]
            task_id = f"gh-{number}"
            message = f"# {issue['title']}\n\n{issue.get('body', '')}"
            results.append((task_id, message))

            # Post recovery comment
            proc = await asyncio.create_subprocess_exec(
                "gh", "issue", "comment", str(number),
                "--repo", repo,
                "--body", "🤖 Bot restarted — resuming work on this issue.",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                log.error("gh issue comment (recovery) failed for #%s: %s", number, stderr.decode())

        log.info("GitHub recovery: found %d open agent-task issues", len(results))
        return results

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
        event_type = request.headers.get("X-GitHub-Event", "")
        action = payload.get("action", "")

        if event_type == "issues" and action == "labeled":
            label = payload.get("label", {}).get("name", "")
            if label != "agent-task":
                return web.Response(text="ignored label")

            issue = payload["issue"]
            task_id = f"gh-{issue['number']}"

            # Idempotency: skip if already processing
            if task_id in self.task_runner._processing:
                return web.Response(text="already processing")

            # Post "Working" comment
            proc = await asyncio.create_subprocess_exec(
                "gh", "issue", "comment", str(issue["number"]),
                "--body", "🤖 Working on this issue...",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                log.error("gh issue comment (working) failed for #%s: %s", issue["number"], stderr.decode())

            # Enqueue title+body as first message
            repo_full_name = payload.get("repository", {}).get("full_name", "")
            message = f"Repository: {repo_full_name}\n\n# {issue['title']}\n\n{issue.get('body', '')}"
            await self.task_runner.enqueue(task_id, message, self)

            # Backfill existing comments
            repo_full_name = payload.get("repository", {}).get("full_name", "")
            if repo_full_name:
                proc = await asyncio.create_subprocess_exec(
                    "gh", "api", f"repos/{repo_full_name}/issues/{issue['number']}/comments",
                    "--jq", ".[].body",
                    stdout=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                if proc.returncode == 0 and stdout:
                    for comment in stdout.decode().strip().split("\n"):
                        if comment.strip():
                            await self.task_runner.enqueue(task_id, comment.strip(), self)

        elif event_type == "issue_comment" and action == "created":
            issue = payload["issue"]
            labels = [lb["name"] for lb in issue.get("labels", [])]
            if "agent-task" not in labels:
                return web.Response(text="not an agent-task issue")

            # Ignore bot's own comments to prevent feedback loops
            sender = payload.get("comment", {}).get("user", {}).get("login", "")
            if sender.endswith("[bot]") or payload["comment"]["body"].startswith(("✅", "❌", "🤖")):
                return web.Response(text="ignoring bot comment")

            task_id = f"gh-{issue['number']}"
            comment_body = payload["comment"]["body"]
            await self.task_runner.enqueue(task_id, comment_body, self)

        return web.Response(status=202, text="Accepted")
