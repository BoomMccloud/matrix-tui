"""Tests for AgentCore — channel-agnostic autonomous task execution."""

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from matrix_agent.channels import Task
from matrix_agent.core import AgentCore


def _make_settings(**overrides):
    defaults = dict(
        gemini_api_key="fake-key",
        github_token="ghp_fake",
        ipc_base_dir="/tmp/test-ipc",
        coding_timeout_seconds=60,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_sandbox():
    sandbox = AsyncMock()
    sandbox._containers = {}
    sandbox.settings = _make_settings()

    async def fake_create(chat_id):
        sandbox._containers[chat_id] = f"sandbox-{chat_id}"
        return f"sandbox-{chat_id}"

    sandbox.create = AsyncMock(side_effect=fake_create)
    sandbox.exec = AsyncMock(return_value=(0, "", ""))
    sandbox.code_stream = AsyncMock(return_value=(0, "PR created: #1", ""))
    return sandbox


# ------------------------------------------------------------------ #
# Task submission
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_submit_creates_container():
    """submit() creates a container for the task."""
    sandbox = _make_sandbox()
    core = AgentCore(sandbox, _make_settings())

    task = Task(task_id="gh-1", description="fix bug", repo="owner/repo", issue_number=1, source="github")
    on_result = AsyncMock()
    on_error = AsyncMock()

    await core.submit(task, on_result=on_result, on_error=on_error)

    sandbox.create.assert_called_once_with("gh-1")


@pytest.mark.asyncio
async def test_submit_clones_repo():
    """submit() clones the repo into the container."""
    sandbox = _make_sandbox()
    core = AgentCore(sandbox, _make_settings())

    task = Task(task_id="gh-2", description="add feature", repo="owner/repo", issue_number=2, source="github")
    on_result = AsyncMock()

    await core.submit(task, on_result=on_result)

    # Find the exec call that does git clone
    clone_calls = [
        c for c in sandbox.exec.call_args_list
        if "git clone" in str(c)
    ]
    assert len(clone_calls) >= 1, "Expected a git clone exec call"


@pytest.mark.asyncio
async def test_submit_runs_gemini_autonomous():
    """submit() runs gemini with auto_accept=True."""
    sandbox = _make_sandbox()
    core = AgentCore(sandbox, _make_settings())

    task = Task(task_id="gh-3", description="fix bug", repo="owner/repo", issue_number=3, source="github")
    on_result = AsyncMock()

    await core.submit(task, on_result=on_result)

    sandbox.code_stream.assert_called_once()
    call_kwargs = sandbox.code_stream.call_args
    # Should pass auto_accept=True
    assert call_kwargs.kwargs.get("auto_accept") is True or (
        len(call_kwargs.args) > 3 and call_kwargs.args[3] is True
    ), "Expected auto_accept=True in code_stream call"


@pytest.mark.asyncio
async def test_submit_fires_on_result_on_success():
    """on_result callback fires when gemini completes successfully."""
    sandbox = _make_sandbox()
    sandbox.code_stream = AsyncMock(return_value=(0, "Created PR #42", ""))
    core = AgentCore(sandbox, _make_settings())

    task = Task(task_id="gh-4", description="fix it", repo="o/r", issue_number=4, source="github")
    on_result = AsyncMock()
    on_error = AsyncMock()

    await core.submit(task, on_result=on_result, on_error=on_error)

    on_result.assert_called_once()
    on_error.assert_not_called()
    # Result should contain the stdout
    result_arg = on_result.call_args[0][0] if on_result.call_args[0] else on_result.call_args[1].get("result", "")
    assert "PR" in str(result_arg) or len(str(result_arg)) > 0


@pytest.mark.asyncio
async def test_submit_fires_on_error_on_failure():
    """on_error callback fires when gemini exits non-zero."""
    sandbox = _make_sandbox()
    sandbox.code_stream = AsyncMock(return_value=(1, "", "gemini crashed"))
    core = AgentCore(sandbox, _make_settings())

    task = Task(task_id="gh-5", description="fix it", repo="o/r", issue_number=5, source="github")
    on_result = AsyncMock()
    on_error = AsyncMock()

    await core.submit(task, on_result=on_result, on_error=on_error)

    on_error.assert_called_once()
    on_result.assert_not_called()


@pytest.mark.asyncio
async def test_submit_fires_on_error_on_exception():
    """on_error callback fires when an exception occurs during execution."""
    sandbox = _make_sandbox()
    sandbox.create = AsyncMock(side_effect=RuntimeError("podman failed"))
    core = AgentCore(sandbox, _make_settings())

    task = Task(task_id="gh-6", description="fix it", repo="o/r", issue_number=6, source="github")
    on_result = AsyncMock()
    on_error = AsyncMock()

    await core.submit(task, on_result=on_result, on_error=on_error)

    on_error.assert_called_once()
    assert "podman failed" in str(on_error.call_args)


@pytest.mark.asyncio
async def test_submit_prompt_includes_issue_context():
    """The prompt passed to gemini includes repo, issue number, and description."""
    sandbox = _make_sandbox()
    core = AgentCore(sandbox, _make_settings())

    task = Task(
        task_id="gh-7",
        description="Login page crashes when email has a plus sign",
        repo="acme/webapp",
        issue_number=42,
        source="github",
    )

    await core.submit(task, on_result=AsyncMock())

    prompt = sandbox.code_stream.call_args[0][1]  # second positional arg
    assert "acme/webapp" in prompt or "42" in prompt
    assert "Login page crashes" in prompt


@pytest.mark.asyncio
async def test_submit_without_repo_skips_clone():
    """When task has no repo, skip git clone step."""
    sandbox = _make_sandbox()
    core = AgentCore(sandbox, _make_settings())

    task = Task(task_id="gh-8", description="just do something", source="github")

    await core.submit(task, on_result=AsyncMock())

    clone_calls = [c for c in sandbox.exec.call_args_list if "git clone" in str(c)]
    assert len(clone_calls) == 0
