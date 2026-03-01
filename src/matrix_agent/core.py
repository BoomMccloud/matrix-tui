"""TaskRunner — channel-agnostic autonomous task execution."""

import asyncio
import logging
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
        # Ensure container exists
        if task_id not in self.sandbox._containers:
            await self.sandbox.create(task_id)

        # Define send_update callback for streaming
        async def send_update(chunk: str) -> None:
            await channel.send_update(task_id, chunk)

        # Run decider
        try:
            final_text = None
            async for text, image in self.decider.handle_message(
                task_id, message,
                send_update=send_update,
                system_prompt=channel.system_prompt,
            ):
                if text:
                    final_text = text
            if final_text:
                await channel.deliver_result(task_id, final_text)
        except Exception as e:
            await channel.deliver_error(task_id, str(e))
            raise

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
