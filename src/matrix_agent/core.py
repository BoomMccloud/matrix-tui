"""AgentCore — channel-agnostic autonomous task execution."""

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from .channels import Task
from .sandbox import SandboxManager, _container_name

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
        on_progress: Callable[[dict], Awaitable[Any]] | None = None,
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

            progress_task = None
            if on_progress:
                progress_task = asyncio.create_task(
                    self._poll_progress(task.task_id, on_progress)
                )

            try:
                rc, stdout, stderr = await self.sandbox.code_stream(
                    task.task_id, prompt, on_chunk=self._noop, auto_accept=True
                )
            finally:
                if progress_task:
                    progress_task.cancel()
                    try:
                        await progress_task
                    except asyncio.CancelledError:
                        pass

            if rc == 0:
                if on_result:
                    await on_result(stdout)
            else:
                if on_error:
                    await on_error(stderr or stdout)
        except Exception as e:
            if on_error:
                await on_error(str(e))

    async def _poll_progress(
        self, task_id: str, on_progress: Callable[[dict], Awaitable[Any]]
    ) -> None:
        """Poll for event-progress.json in the IPC dir and fire on_progress."""
        container_name = _container_name(task_id)
        ipc_file = os.path.join(
            self.settings.ipc_base_dir, container_name, "event-progress.json"
        )
        try:
            while True:
                await asyncio.sleep(1)
                if os.path.exists(ipc_file):
                    try:
                        with open(ipc_file) as f:
                            data = json.load(f)
                        os.unlink(ipc_file)
                        log.info("[%s] IPC progress: %s", container_name, data)
                        await on_progress(data)
                    except Exception:
                        log.exception("[%s] Failed to read progress event", container_name)
        except asyncio.CancelledError:
            pass

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
