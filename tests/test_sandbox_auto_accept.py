"""Tests for sandbox auto_accept flag (gemini -y autonomous mode)."""

from unittest.mock import AsyncMock, patch

import pytest

from matrix_agent.sandbox import SandboxManager


def _make_settings():
    from types import SimpleNamespace
    return SimpleNamespace(
        podman_path="podman",
        sandbox_image="test:latest",
        command_timeout_seconds=10,
        coding_timeout_seconds=30,
        gemini_api_key="fake",
        dashscope_api_key="",
        github_token="",
        ipc_base_dir="/tmp/test-ipc",
        screenshot_script="/opt/playwright/screenshot.js",
    )


@pytest.mark.asyncio
async def test_code_stream_default_no_y_flag():
    """code_stream without auto_accept should NOT pass -y to gemini."""
    sandbox = SandboxManager(_make_settings())
    sandbox._containers = {"room-1": "sandbox-room-1"}

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        # Set up mock process
        proc = AsyncMock()
        proc.stdout = AsyncMock()
        proc.stdout.__aiter__ = lambda self: aiter([])
        proc.stderr = AsyncMock()
        proc.stderr.__aiter__ = lambda self: aiter([])
        proc.wait = AsyncMock(return_value=0)
        proc.returncode = 0
        mock_exec.return_value = proc

        on_chunk = AsyncMock()
        await sandbox.code_stream("room-1", "do something", on_chunk)

        # Check the args passed to create_subprocess_exec
        call_args = mock_exec.call_args[0]
        args_list = list(call_args)
        assert "-y" not in args_list, "Should not pass -y without auto_accept"


@pytest.mark.asyncio
async def test_code_stream_auto_accept_passes_y_flag():
    """code_stream with auto_accept=True should pass -y to gemini."""
    sandbox = SandboxManager(_make_settings())
    sandbox._containers = {"room-1": "sandbox-room-1"}

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        proc = AsyncMock()
        proc.stdout = AsyncMock()
        proc.stdout.__aiter__ = lambda self: aiter([])
        proc.stderr = AsyncMock()
        proc.stderr.__aiter__ = lambda self: aiter([])
        proc.wait = AsyncMock(return_value=0)
        proc.returncode = 0
        mock_exec.return_value = proc

        on_chunk = AsyncMock()
        await sandbox.code_stream("room-1", "do something", on_chunk, auto_accept=True)

        call_args = mock_exec.call_args[0]
        args_list = list(call_args)
        assert "-y" in args_list, "Should pass -y with auto_accept=True"
        # -y should come before -p
        y_idx = args_list.index("-y")
        p_idx = args_list.index("-p")
        assert y_idx < p_idx, "-y should appear before -p"


@pytest.mark.asyncio
async def test_code_default_no_y_flag():
    """code() without auto_accept should NOT pass -y."""
    sandbox = SandboxManager(_make_settings())
    sandbox._containers = {"room-1": "sandbox-room-1"}

    with patch.object(sandbox, "_run", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = (0, "output", "")
        await sandbox.code("room-1", "do something")

        call_args = mock_run.call_args[0]
        assert "-y" not in call_args


@pytest.mark.asyncio
async def test_code_auto_accept_passes_y_flag():
    """code() with auto_accept=True should pass -y."""
    sandbox = SandboxManager(_make_settings())
    sandbox._containers = {"room-1": "sandbox-room-1"}

    with patch.object(sandbox, "_run", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = (0, "output", "")
        await sandbox.code("room-1", "do something", auto_accept=True)

        call_args = mock_run.call_args[0]
        assert "-y" in call_args


# Helper for async iteration
async def aiter(items):
    for item in items:
        yield item
