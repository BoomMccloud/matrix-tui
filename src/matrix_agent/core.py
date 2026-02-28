"""AgentCore — channel-agnostic autonomous task execution."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .channels import Task
from .sandbox import SandboxManager

log = logging.getLogger(__name__)


class AgentCore:
    def __init__(self, sandbox: SandboxManager, settings: Any):
        self.sandbox = sandbox
        self.settings = settings

    async def submit(
        self,
        task: Task,
        on_result: Callable[[str], Awaitable[Any]] | None = None,
        on_error: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        try:
            await self.sandbox.create(task.task_id)

            if task.repo:
                token = getattr(self.settings, "github_token", "")
                if token:
                    clone_url = f"https://{token}@github.com/{task.repo}.git"
                else:
                    clone_url = f"https://github.com/{task.repo}.git"
                await self.sandbox.exec(
                    task.task_id, f"git clone {clone_url} /workspace/repo"
                )

            prompt = self._build_prompt(task)
            rc, stdout, stderr = await self.sandbox.code_stream(
                task.task_id, prompt, on_chunk=self._noop, auto_accept=True
            )

            if rc == 0:
                if on_result:
                    await on_result(stdout)
            else:
                if on_error:
                    await on_error(stderr or stdout)
        except Exception as e:
            if on_error:
                await on_error(str(e))

    def _build_prompt(self, task: Task) -> str:
        parts = []
        if task.repo:
            parts.append(f"Repository: {task.repo}")
        if task.issue_number is not None:
            parts.append(f"Issue #{task.issue_number}")
        parts.append(f"\n{task.description}")
        return "\n".join(parts)

    @staticmethod
    async def _noop(chunk: str) -> None:
        pass
