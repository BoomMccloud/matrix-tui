"""Tests for TaskRunner — channel-agnostic autonomous task execution."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from matrix_agent.core import TaskRunner
from matrix_agent.channels import ChannelAdapter


class MockChannel(ChannelAdapter):
    system_prompt = "Test prompt"

    def __init__(self):
        self.results = []
        self.errors = []
        self.updates = []

    async def start(self) -> None: pass
    async def stop(self) -> None: pass

    async def send_update(self, task_id: str, text: str) -> None:
        self.updates.append((task_id, text))

    async def deliver_result(self, task_id: str, text: str) -> None:
        self.results.append((task_id, text))

    async def deliver_error(self, task_id: str, error: str) -> None:
        self.errors.append((task_id, error))

    async def is_valid(self, task_id: str) -> bool:
        return True


def _make_sandbox():
    sandbox = AsyncMock()
    sandbox._containers = {}
    
    async def fake_create(chat_id):
        sandbox._containers[chat_id] = f"sandbox-{chat_id}"
        return f"sandbox-{chat_id}"

    sandbox.create = AsyncMock(side_effect=fake_create)
    sandbox.destroy = AsyncMock()
    return sandbox


def _make_decider(mock_responses: list[tuple[str, bytes | None]]):
    decider = MagicMock()

    async def mock_handle_message(chat_id, user_text, send_update=None, system_prompt=None):
        for text, image in mock_responses:
            if isinstance(text, Exception):
                raise text
            yield text, image

    decider.handle_message = mock_handle_message
    return decider


# ------------------------------------------------------------------ #
# TaskRunner tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_enqueue_creates_queue_and_worker():
    """enqueue() creates a queue, tracking state, and starts a worker."""
    sandbox = _make_sandbox()
    decider = _make_decider([])
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    await runner.enqueue("task-1", "hello", channel)

    assert "task-1" in runner._queues
    assert "task-1" in runner._channels
    assert "task-1" in runner._processing
    assert "task-1" in runner._workers

    # Clean up worker
    await runner._cleanup("task-1")


@pytest.mark.asyncio
async def test_pre_register():
    """pre_register() adds task to _processing with empty queue."""
    sandbox = _make_sandbox()
    decider = _make_decider([])
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    await runner.pre_register("task-pr", channel)

    assert "task-pr" in runner._processing
    assert "task-pr" in runner._queues
    assert "task-pr" in runner._workers
    assert "task-pr" in runner._channels
    assert runner._queues["task-pr"].empty()

    await runner._cleanup("task-pr")


@pytest.mark.asyncio
async def test_pre_register_idempotent():
    """pre_register() is a no-op if task already registered."""
    sandbox = _make_sandbox()
    decider = _make_decider([])
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    await runner.pre_register("task-pr2", channel)
    original_queue = runner._queues["task-pr2"]

    await runner.pre_register("task-pr2", channel)
    assert runner._queues["task-pr2"] is original_queue  # same object

    await runner._cleanup("task-pr2")


@pytest.mark.asyncio
async def test_worker_processes_messages_sequentially():
    """Worker processes messages via _process() in order."""
    sandbox = _make_sandbox()
    # Decider yields a success message
    decider = _make_decider([("Processed", None)])
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    # Enqueue two messages
    await runner.enqueue("task-2", "msg 1", channel)
    await runner.enqueue("task-2", "msg 2", channel)

    # Yield to let the worker run
    await asyncio.sleep(0.01)

    # Check that decider was run
    assert len(channel.results) == 2
    assert channel.results[0] == ("task-2", "Processed")
    assert channel.results[1] == ("task-2", "Processed")

    await runner._cleanup("task-2")


@pytest.mark.asyncio
async def test_process_creates_container():
    """_process() creates a sandbox container if it doesn't exist."""
    sandbox = _make_sandbox()
    decider = _make_decider([("Done", None)])
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    await runner.enqueue("task-3", "hello", channel)
    await asyncio.sleep(0.01)

    sandbox.create.assert_called_once_with("task-3")
    assert "task-3" in sandbox._containers

    await runner._cleanup("task-3")


@pytest.mark.asyncio
async def test_process_delivers_error_on_exception():
    """_process() catches exceptions and calls deliver_error on the channel."""
    sandbox = _make_sandbox()
    # Decider raises an exception
    decider = _make_decider([(Exception("decider failed"), None)])
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    await runner.enqueue("task-4", "hello", channel)
    await asyncio.sleep(0.01)

    assert len(channel.errors) == 1
    assert channel.errors[0] == ("task-4", "decider failed")
    assert len(channel.results) == 0

    await runner._cleanup("task-4")


@pytest.mark.asyncio
async def test_reconcile_cleans_invalid_tasks():
    """reconcile() cleans up tasks when is_valid returns False."""
    sandbox = _make_sandbox()
    decider = _make_decider([])
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    await runner.enqueue("task-5", "hello", channel)
    
    # Force container presence to check destruction
    sandbox._containers["task-5"] = "sandbox-task-5"

    # Make the channel report task is no longer valid
    channel.is_valid = AsyncMock(return_value=False)

    await runner.reconcile()

    assert "task-5" not in runner._queues
    assert "task-5" not in runner._channels
    assert "task-5" not in runner._processing
    assert "task-5" not in runner._workers
    sandbox.destroy.assert_called_once_with("task-5")


@pytest.mark.asyncio
async def test_destroy_orphans():
    """destroy_orphans() cleans up containers not in _processing."""
    sandbox = _make_sandbox()
    sandbox._containers = {
        "active-1": "sandbox-active-1",
        "orphan-1": "sandbox-orphan-1",
    }
    decider = _make_decider([])
    runner = TaskRunner(decider, sandbox)
    runner._processing.add("active-1")

    await runner.destroy_orphans()

    sandbox.destroy.assert_called_once_with("orphan-1")


@pytest.mark.asyncio
async def test_destroy_orphans_preserves_pre_registered():
    """destroy_orphans() does not destroy containers for pre-registered tasks."""
    sandbox = _make_sandbox()
    sandbox._containers = {
        "recovered-1": "sandbox-recovered-1",
        "orphan-1": "sandbox-orphan-1",
    }
    decider = _make_decider([])
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    # Pre-register one task (simulates recovery)
    await runner.pre_register("recovered-1", channel)

    await runner.destroy_orphans()

    # Orphan destroyed, recovered preserved
    sandbox.destroy.assert_called_once_with("orphan-1")
    assert "recovered-1" in runner._processing

    await runner._cleanup("recovered-1")
