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
    sandbox.save_state = MagicMock()
    sandbox.has_container = MagicMock(side_effect=lambda cid: cid in sandbox._containers)
    sandbox.container_ids = MagicMock(side_effect=lambda: list(sandbox._containers))
    return sandbox

def _make_decider():
    decider = MagicMock()
    async def mock_handle_message(chat_id, user_text, send_update=None, system_prompt=None):
        # Hang forever to simulate active work
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise
        yield "Never", None, "completed"
    decider.handle_message = mock_handle_message
    return decider

@pytest.mark.asyncio
async def test_task_runner_shutdown():
    """shutdown() cancels all workers and destroys containers."""
    sandbox = _make_sandbox()
    decider = _make_decider()
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    await runner.enqueue("task-1", "hello", channel)
    await runner.enqueue("task-2", "world", channel)
    
    # Wait for workers to start and containers to be requested
    await asyncio.sleep(0.1)
    
    assert len(runner._workers) == 2
    assert "task-1" in runner._processing
    assert "task-2" in runner._processing
    
    # Trigger shutdown
    await runner.shutdown()
    
    # Verify workers are gone
    assert len(runner._workers) == 0
    assert len(runner._processing) == 0
    assert len(runner._channels) == 0
    
    # Verify containers are destroyed
    assert sandbox.destroy.call_count == 2
    sandbox.destroy.assert_any_call("task-1")
    sandbox.destroy.assert_any_call("task-2")

    # Verify state is saved
    assert sandbox.save_state.called
