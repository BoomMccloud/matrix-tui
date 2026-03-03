"""TaskRunner — channel-agnostic autonomous task execution."""

import asyncio
import logging
import re
import time
from .sandbox import SandboxManager
from .decider import Decider
from .channels import ChannelAdapter

logger = logging.getLogger(__name__)


class TaskRunner:
    def __init__(self, decider: Decider, sandbox: SandboxManager):
        self.decider = decider
        self.sandbox = sandbox
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._channels: dict[str, ChannelAdapter] = {}  # task_id -> channel
        self._processing: set[str] = set()

    async def pre_register(self, task_id: str, channel: ChannelAdapter) -> None:
        """Register a task so destroy_orphans() preserves its container.

        Creates queue + worker + adds to _processing without enqueuing a message.
        The worker idles on queue.get() until a message arrives or reconcile()
        cleans it up via is_valid().
        """
        if task_id in self._queues:
            return
        self._queues[task_id] = asyncio.Queue()
        self._channels[task_id] = channel
        self._processing.add(task_id)
        self._workers[task_id] = asyncio.create_task(
            self._worker(task_id)
        )

    async def enqueue(self, task_id: str, message: str, channel: ChannelAdapter) -> None:
        """Add a message for a task. Creates the worker on first call."""
        if task_id not in self._queues:
            self._queues[task_id] = asyncio.Queue()
            self._channels[task_id] = channel
            self._processing.add(task_id)
            self._workers[task_id] = asyncio.create_task(
                self._worker(task_id)
            )
        await self._queues[task_id].put(message)

    async def _worker(self, task_id: str) -> None:
        """Process messages sequentially for a single task."""
        channel = self._channels[task_id]
        queue = self._queues[task_id]
        try:
            while True:
                message = await queue.get()
                try:
                    await self._process(task_id, message, channel)
                except Exception:
                    logger.exception("Error processing %s", task_id)
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            pass

    async def _process(self, task_id: str, message: str, channel: ChannelAdapter) -> None:
        """Run the decider loop for one message."""
        logger.info("[%s] Task started (message: %d chars)", task_id[:20], len(message))
        t0 = time.monotonic()

        # Ensure container exists
        if task_id not in self.sandbox._containers:
            logger.info("[%s] Creating container...", task_id[:20])
            await self.sandbox.create(task_id)
            logger.info("[%s] Container ready", task_id[:20])

        # Define send_update callback for streaming
        async def send_update(chunk: str) -> None:
            await channel.send_update(task_id, chunk)

        # Overall timeout: coding_timeout + buffer for setup
        overall_timeout = self.sandbox.settings.coding_timeout_seconds + 300

        try:
            if task_id.startswith("gh-"):
                await asyncio.wait_for(
                    self._process_github(task_id, message, channel, send_update),
                    timeout=overall_timeout,
                )
            else:
                await asyncio.wait_for(
                    self._process_matrix(task_id, message, channel, send_update),
                    timeout=overall_timeout,
                )
            elapsed = time.monotonic() - t0
            logger.info("[%s] Task completed in %.1fs", task_id[:20], elapsed)
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - t0
            logger.error("[%s] Task timed out after %.1fs", task_id[:20], elapsed)
            await channel.deliver_error(task_id, f"Task timed out after {int(elapsed)}s")
        except Exception as e:
            elapsed = time.monotonic() - t0
            logger.error("[%s] Task failed after %.1fs: %s", task_id[:20], elapsed, e)
            await channel.deliver_error(task_id, str(e))
            raise

    async def _process_matrix(self, task_id: str, message: str, channel: ChannelAdapter, send_update) -> None:
        """Existing LiteLLM decider path for Matrix chat."""
        final_text = None
        final_status = "completed"
        async for text, image, status in self.decider.handle_message(
            task_id, message,
            send_update=send_update,
            system_prompt=channel.system_prompt,
        ):
            if text:
                final_text = text
                final_status = status
        if final_text:
            await channel.deliver_result(task_id, final_text, status=final_status)

    async def _process_github(self, task_id: str, message: str, channel: ChannelAdapter, send_update) -> None:
        """Single Gemini CLI session for GitHub issues."""
        # Parse repo from message
        is_ci_fix = message.startswith("CI_FIX:")
        repo_match = re.search(r"Repository:\s*(\S+)", message)

        if not repo_match:
            await channel.deliver_error(task_id, "Could not parse repository from message")
            return

        repo_full = repo_match.group(1)  # e.g. "owner/repo"
        repo_name = repo_full.split("/")[-1]  # e.g. "repo"
        mode = "CI fix" if is_ci_fix else "new issue"
        logger.info("[%s] GitHub pipeline: %s for %s", task_id[:20], mode, repo_full)

        # Clone repo (idempotent — skip if dir exists)
        clone_rc, _, clone_err = await self.sandbox.exec(
            task_id,
            f"test -d /workspace/{repo_name}/.git || git clone https://github.com/{repo_full} /workspace/{repo_name}",
        )
        if clone_rc != 0:
            await channel.deliver_error(task_id, f"Clone failed: {clone_err}")
            return

        # Build prompt
        if is_ci_fix:
            prompt = f"/fix-ci {message}"
        else:
            prompt = f"/fix-issue {message}"

        # Run Gemini with retries
        max_retries = 2
        for attempt in range(max_retries + 1):
            rc, stdout, pr_url = await self.sandbox.run_gemini_session(
                task_id, prompt, send_update, repo_name,
            )

            # Check if Gemini is asking for clarification
            clarification = await self.sandbox.read_ipc_file(task_id, "clarification.txt")
            if clarification:
                logger.info("[%s] Gemini requesting clarification", task_id[:20])
                await channel.deliver_result(
                    task_id,
                    f"I need clarification before I can proceed:\n\n{clarification}",
                    status="max_turns",
                )
                return

            # Validate
            passed, failures = await self.sandbox.validate_work(task_id, repo_name)

            if passed and pr_url:
                logger.info("[%s] GitHub pipeline succeeded: %s", task_id[:20], pr_url)
                await channel.deliver_result(task_id, f"PR created: {pr_url}")
                return

            if attempt < max_retries:
                # Re-launch with feedback
                failure_text = "\n".join(f"- {f}" for f in failures)
                if not pr_url:
                    failure_text += "\n- No PR URL found"
                prompt = (
                    f"Host validation failed after your previous attempt:\n"
                    f"{failure_text}\n\n"
                    f"Fix these issues, then create the PR.\n\n"
                    f"Original issue:\n{message}"
                )
                logger.warning("[%s] Validation failed (attempt %d/%d): %s",
                               task_id[:20], attempt + 1, max_retries + 1,
                               "; ".join(failures))
            else:
                # Final failure
                failure_text = "\n".join(f"- {f}" for f in failures)
                await channel.deliver_error(
                    task_id,
                    f"Failed after {max_retries + 1} attempts. Issues:\n{failure_text}",
                )
                return

    async def reconcile(self) -> None:
        """Destroy containers for tasks that are no longer valid."""
        for task_id in list(self._channels):
            channel = self._channels[task_id]
            if not await channel.is_valid(task_id):
                logger.info("Reconcile: cleaning up %s", task_id)
                await self._cleanup(task_id)

    async def _cleanup(self, task_id: str) -> None:
        """Cancel worker, destroy container, remove all tracking state."""
        if task_id in self._workers:
            self._workers[task_id].cancel()
            del self._workers[task_id]
        self._queues.pop(task_id, None)
        self._channels.pop(task_id, None)
        self._processing.discard(task_id)
        if task_id in self.sandbox._containers:
            await self.sandbox.destroy(task_id)

    async def reconcile_loop(self) -> None:
        """Run reconcile every 60s."""
        while True:
            await asyncio.sleep(60)
            try:
                await self.reconcile()
            except Exception:
                logger.exception("Reconcile error")

    async def destroy_orphans(self) -> None:
        """On startup, destroy containers with no active worker (crash recovery)."""
        for chat_id in list(self.sandbox._containers):
            if chat_id not in self._processing:
                logger.info("Destroying orphan container: %s", chat_id)
                await self.sandbox.destroy(chat_id)
