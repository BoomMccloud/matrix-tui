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
        if not self.sandbox.has_container(task_id):
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
        """Gemini CLI session for GitHub issues. Host controls push and PR creation."""
        # Parse repo from message
        is_ci_fix = message.startswith("CI_FIX:")
        repo_match = re.search(r"Repository:\s*(\S+)", message)

        if not repo_match:
            await channel.deliver_error(task_id, "Could not parse repository from message")
            return

        repo_full = repo_match.group(1)  # e.g. "owner/repo"
        repo_name = repo_full.split("/")[-1]  # e.g. "repo"
        repo_path = f"/workspace/{repo_name}"
        mode = "CI fix" if is_ci_fix else "new issue"
        logger.info("[%s] GitHub pipeline: %s for %s", task_id[:20], mode, repo_full)

        # Clone repo (idempotent — skip if dir exists)
        clone_rc, _, clone_err = await self.sandbox.exec(
            task_id,
            f"test -d {repo_path}/.git || git clone https://github.com/{repo_full} {repo_path}",
        )
        if clone_rc != 0:
            await channel.deliver_error(task_id, f"Clone failed: {clone_err}")
            return

        # Create feature branch upfront (new issues only — CI fixes already have a branch)
        if not is_ci_fix:
            issue_num = task_id.replace("gh-", "").split("-")[0]
            branch_name = f"agent/issue-{issue_num}"
            rc, _, err = await self.sandbox.exec(
                task_id,
                f"cd {repo_path} && git checkout -b {branch_name}",
            )
            if rc != 0:
                # Fallback: branch might already exist
                rc, _, err = await self.sandbox.exec(
                    task_id,
                    f"cd {repo_path} && git checkout {branch_name}",
                )
                if rc != 0:
                    await channel.deliver_error(
                        task_id,
                        f"Failed to create or switch to branch {branch_name}: {err}",
                    )
                    return

        # Build prompt
        if is_ci_fix:
            prompt = f"/fix-ci {message}"
        else:
            prompt = f"/fix-issue {message}"

        # Run Gemini with retries
        max_retries = 2
        for attempt in range(max_retries + 1):
            rc, stdout, _ = await self.sandbox.run_gemini_session(
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

            # Validate (tests, scope) locally before push
            passed, failures = await self.sandbox.validate_work(task_id, repo_name, pre_push=True)

            if passed:
                break

            if attempt < max_retries:
                # Re-launch with feedback
                failure_text = "\n".join(f"- {f}" for f in failures)
                prompt = (
                    f"Host validation failed after your previous attempt:\n"
                    f"{failure_text}\n\n"
                    f"Fix these issues, then commit your changes.\n\n"
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

        # Host-controlled push: push branch, create PR
        pr_url, push_error = await self._host_push(
            task_id, repo_path, repo_full, is_ci_fix,
        )
        if pr_url:
            logger.info("[%s] GitHub pipeline succeeded: %s", task_id[:20], pr_url)
            await channel.deliver_result(task_id, f"PR created: {pr_url}")
        else:
            await channel.deliver_error(task_id, f"Push failed: {push_error}")

    async def _host_push(
        self, task_id: str, repo_path: str, repo_full: str, is_ci_fix: bool,
    ) -> tuple[str | None, str | None]:
        """Host-controlled push: push branch, create PR.

        Returns (pr_url, error_reason). error_reason is None on success.
        """
        # 1. Detect branch
        rc, branch_out, _ = await self.sandbox.exec(
            task_id, f"cd {repo_path} && git rev-parse --abbrev-ref HEAD",
        )
        branch = branch_out.strip()
        if not branch or branch in ("main", "master"):
            logger.warning("[%s] No feature branch found (on %s)", task_id[:20], branch)
            return None, (
                "No feature branch created. You must run: "
                "git checkout -b agent/<slug> before committing. "
                "Do NOT commit on main."
            )

        # 2. Push
        if is_ci_fix:
            push_cmd = f"cd {repo_path} && git push --force origin {branch}"
        else:
            push_cmd = f"cd {repo_path} && git push -u origin {branch}"
        rc, _, push_err = await self.sandbox.exec(task_id, push_cmd)
        if rc != 0:
            logger.error("[%s] Push failed: %s", task_id[:20], push_err)
            return None, f"git push failed: {push_err.strip()}"

        # 3. Create PR (or get existing PR URL for CI fixes)
        if is_ci_fix:
            rc, pr_url_out, _ = await self.sandbox.exec(
                task_id,
                f"cd {repo_path} && gh pr view {branch} --json url -q .url",
            )
        else:
            issue_num = task_id.replace("gh-", "").split("-")[0]
            rc, pr_url_out, _ = await self.sandbox.exec(
                task_id,
                f"cd {repo_path} && "
                f"gh pr create --title 'Fix #{issue_num}' "
                f"--body 'Closes #{issue_num}' --head {branch} 2>&1 || "
                f"gh pr view {branch} --json url -q .url",
            )
        # Extract first URL from output (gh pr create may include extra text)
        pr_url = None
        for line in pr_url_out.splitlines():
            line = line.strip()
            if line.startswith("http"):
                pr_url = line
                break
        if not pr_url:
            logger.error("[%s] Failed to get PR URL: %s", task_id[:20], pr_url_out)
            return None, f"Failed to create PR: {pr_url_out.strip()}"

        # 4. Write PR URL to IPC
        self.sandbox.write_ipc_file(task_id, "pr-url.txt", pr_url)
        logger.info("[%s] Host pushed %s and created PR: %s", task_id[:20], branch, pr_url)
        return pr_url, None

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
        if self.sandbox.has_container(task_id):
            await self.sandbox.destroy(task_id)

    async def shutdown(self) -> None:
        """Cancel all workers and destroy all containers."""
        logger.info("TaskRunner: shutting down, cancelling %d workers", len(self._workers))
        for task_id, worker in list(self._workers.items()):
            worker.cancel()
        await asyncio.gather(*self._workers.values(), return_exceptions=True)
        
        # Cleanup all tasks (destroys containers)
        for task_id in list(self._channels):
            await self._cleanup(task_id)
        logger.info("TaskRunner: shutdown complete")

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
        for chat_id in self.sandbox.container_ids():
            if chat_id not in self._processing:
                logger.info("Destroying orphan container: %s", chat_id)
                await self.sandbox.destroy(chat_id)
