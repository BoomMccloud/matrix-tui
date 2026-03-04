"""Shared test infrastructure — mock only external boundaries."""

import sys
import os
from pathlib import Path

# Add 'src' to sys.path to ensure matrix_agent is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from unittest.mock import AsyncMock, MagicMock

import pytest

from matrix_agent.channels import ChannelAdapter
from matrix_agent.config import Settings


class SubprocessMocker:
    """Route subprocess calls to scripted responses based on command prefix."""

    def __init__(self):
        self.handlers: list[tuple[tuple[str, ...], dict]] = []
        self.calls: list[tuple] = []

    def on(self, *prefix: str, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
        """Register a handler for commands matching prefix."""
        self.handlers.append((prefix, {"returncode": returncode, "stdout": stdout, "stderr": stderr}))
        return self  # allow chaining

    async def __call__(self, *args, **kwargs):
        """Patch target for asyncio.create_subprocess_exec."""
        self.calls.append(args)

        # Match handlers by prefix (longest match first)
        best_match = None
        best_len = 0
        for prefix, response in self.handlers:
            if args[:len(prefix)] == prefix and len(prefix) > best_len:
                best_match = response
                best_len = len(prefix)

        if best_match is None:
            best_match = {"returncode": 0, "stdout": b"", "stderr": b""}

        proc = MagicMock()
        proc.returncode = best_match["returncode"]
        proc.communicate = AsyncMock(return_value=(best_match["stdout"], best_match["stderr"]))
        proc.wait = AsyncMock(return_value=best_match["returncode"])
        proc.kill = MagicMock()

        # For code_stream which reads stdout/stderr line by line
        async def _aiter_lines(data):
            for line in data.split(b"\n"):
                if line:
                    yield line + b"\n"

        stdout_mock = MagicMock()
        stdout_mock.__aiter__ = lambda self: _aiter_lines(best_match["stdout"])
        proc.stdout = stdout_mock

        stderr_mock = MagicMock()
        stderr_mock.__aiter__ = lambda self: _aiter_lines(best_match["stderr"])
        proc.stderr = stderr_mock

        return proc


class StubChannel(ChannelAdapter):
    """Real ChannelAdapter implementation that captures calls."""
    system_prompt = "Test prompt"

    def __init__(self):
        self.results: list[tuple] = []
        self.errors: list[tuple] = []
        self.updates: list[tuple] = []

    async def start(self): pass
    async def stop(self): pass

    async def send_update(self, task_id: str, text: str):
        self.updates.append((task_id, text))

    async def deliver_result(self, task_id: str, text: str, *, status: str = "completed"):
        self.results.append((task_id, text, status))

    async def deliver_error(self, task_id: str, error: str):
        self.errors.append((task_id, error))

    async def is_valid(self, task_id: str):
        return True


@pytest.fixture
def settings(tmp_path):
    return Settings(
        matrix_homeserver="https://example.com",
        matrix_user="@bot:example.com",
        matrix_password="pass",
        llm_api_key="fake-key",
        ipc_base_dir=str(tmp_path / "ipc"),
        github_token="ghp_fake",
        github_repo="owner/repo",
        github_webhook_port=0,
        github_webhook_secret="test-secret",
    )


@pytest.fixture
def subprocess_mocker():
    return SubprocessMocker()
