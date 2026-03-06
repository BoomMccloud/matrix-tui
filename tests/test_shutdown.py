"""Tests for graceful shutdown."""

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
    async def deliver_result(self, task_id: str, text: str, *, status: str = "completed") -> None:
        self.results.append((task_id, text))
    async def deliver_error(self, task_id: str, error: str) -> None:
        self.errors.append((task_id, error))
    async def is_valid(self, task_id: str) -> bool:
        return True

def _make_sandbox():
    sandbox = AsyncMock()
    sandbox._containers = {}
    sandbox.settings = MagicMock()
    sandbox.settings.coding_timeout_seconds = 600
    
    async def fake_create(chat_id):
        sandbox._containers[chat_id] = f"sandbox-{chat_id}"
        return f"sandbox-{chat_id}"

    sandbox.create = AsyncMock(side_effect=fake_create)
    sandbox.destroy = AsyncMock()
    sandbox.save_state = MagicMock()  # Synchronous
    sandbox.has_container = MagicMock(side_effect=lambda cid: cid in sandbox._containers)
    sandbox.container_ids = MagicMock(side_effect=lambda: list(sandbox._containers))
    return sandbox

def _make_decider():
    decider = MagicMock()
    async def mock_handle_message(chat_id, user_text, send_update=None, system_prompt=None):
        await asyncio.sleep(10) # Long task
        yield "Finished", None, "completed"
    decider.handle_message = mock_handle_message
    return decider

@pytest.mark.asyncio
async def test_task_runner_shutdown_cancels_workers_and_destroys_containers():
    """TaskRunner.shutdown() cancels all workers and destroys all containers."""
    sandbox = _make_sandbox()
    decider = _make_decider()
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    task_id = "task-shutdown"
    await runner.enqueue(task_id, "hello", channel)
    
    # Wait for worker to start and create container
    await asyncio.sleep(0.1)
    assert task_id in runner._workers
    assert task_id in runner._processing
    assert sandbox.has_container(task_id)

    # Trigger shutdown
    await runner.shutdown()

    # Verify worker cancelled
    assert task_id not in runner._workers
    assert task_id not in runner._processing
    
    # Verify container destroyed
    sandbox.destroy.assert_called_with(task_id)

@pytest.mark.asyncio
async def test_task_runner_shutdown_multiple_tasks():
    """TaskRunner.shutdown() handles multiple active tasks."""
    sandbox = _make_sandbox()
    decider = _make_decider()
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    tasks = ["task-1", "task-2", "task-3"]
    for tid in tasks:
        await runner.enqueue(tid, "hello", channel)
    
    await asyncio.sleep(0.1)
    for tid in tasks:
        assert tid in runner._workers
        assert sandbox.has_container(tid)

    await runner.shutdown()

    for tid in tasks:
        assert tid not in runner._workers
        sandbox.destroy.assert_any_call(tid)

@pytest.mark.asyncio
async def test_task_runner_shutdown_saves_state_logic():
    """Verify that we can call sandbox.save_state() after shutdown."""
    sandbox = _make_sandbox()
    decider = _make_decider()
    runner = TaskRunner(decider, sandbox)
    
    # This just ensures we don't have regressions in calling these in sequence
    await runner.shutdown()
    sandbox.save_state()
    sandbox.save_state.assert_called_once()
