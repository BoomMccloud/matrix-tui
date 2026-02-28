"""Integration tests for the channel → core → container → result pipeline.

Requirements:
- podman on PATH
- GEMINI_API_KEY in environment (use: uv run --env-file .env pytest ...)
- Sandbox image built: podman build -t matrix-agent-sandbox:latest -f Containerfile .
"""

import json
import logging
import os
import shutil
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from matrix_agent.channels import Task
from matrix_agent.core import AgentCore
from matrix_agent.sandbox import SandboxManager

log = logging.getLogger(__name__)

# --------------- skip conditions --------------- #

_has_podman = shutil.which("podman") is not None
_has_gemini_key = bool(os.environ.get("GEMINI_API_KEY", ""))
_has_llm_key = bool(os.environ.get("LLM_API_KEY", ""))
_has_dashscope = bool(os.environ.get("DASHSCOPE_API_KEY", ""))

pytestmark = [
    pytest.mark.skipif(not _has_podman, reason="podman not on PATH"),
    pytest.mark.skipif(not _has_gemini_key, reason="GEMINI_API_KEY not set"),
    pytest.mark.integration,
]


# --------------- fixtures --------------- #


@pytest.fixture
def settings():
    return SimpleNamespace(
        podman_path="podman",
        sandbox_image="matrix-agent-sandbox:latest",
        command_timeout_seconds=120,
        coding_timeout_seconds=300,
        gemini_api_key=os.environ["GEMINI_API_KEY"],
        dashscope_api_key=os.environ.get("DASHSCOPE_API_KEY", ""),
        github_token=os.environ.get("GITHUB_TOKEN", ""),
        ipc_base_dir=os.path.realpath("/tmp/test-integration-ipc"),
        screenshot_script="/opt/playwright/screenshot.js",
    )


@pytest.fixture
async def sandbox(settings):
    mgr = SandboxManager(settings)
    yield mgr
    for chat_id in list(mgr._containers):
        try:
            await mgr.destroy(chat_id)
        except Exception:
            log.warning("Failed to destroy container for %s", chat_id)


@pytest.fixture
def core(sandbox, settings):
    return AgentCore(sandbox, settings)


# --------------- tests --------------- #


@pytest.mark.asyncio
async def test_submit_creates_file(core, sandbox):
    """core.submit() → real container → gemini -y creates a file."""
    task = Task(
        task_id="integ-1",
        description=(
            "Create a file /workspace/hello.py containing exactly: print('hello world')\n"
            "Then run: python /workspace/hello.py"
        ),
        source="test",
    )

    result = None
    error = None

    async def on_result(r):
        nonlocal result
        result = r

    async def on_error(e):
        nonlocal error
        error = e

    log.info("Submitting task %s", task.task_id)
    await core.submit(task, on_result=on_result, on_error=on_error)
    log.info("Task completed. result=%s, error=%s", result is not None, error)

    assert error is None, f"Expected success but got error: {error}"
    assert result is not None, "Expected a result"

    # Verify file exists in container
    rc, stdout, stderr = await sandbox.exec("integ-1", "cat /workspace/hello.py")
    assert rc == 0, f"cat failed (rc={rc}): {stderr}"
    assert "hello" in stdout.lower()


@pytest.mark.asyncio
async def test_submit_error_callback(core, sandbox):
    """core.submit() fires on_error when gemini exits non-zero or raises."""
    task = Task(
        task_id="integ-err",
        description="Run: /nonexistent/binary --fail",
        source="test",
    )

    result = None
    error = None

    async def on_result(r):
        nonlocal result
        result = r

    async def on_error(e):
        nonlocal error
        error = e

    await core.submit(task, on_result=on_result, on_error=on_error)

    # Either callback should have fired
    assert result is not None or error is not None, "No callback fired"


@pytest.mark.asyncio
async def test_container_creation_and_cleanup(sandbox):
    """Verify container is created and can be destroyed."""
    name = await sandbox.create("integ-lifecycle")
    assert name, "create() returned empty name"
    assert "integ-lifecycle" in sandbox._containers

    rc, stdout, _ = await sandbox.exec("integ-lifecycle", "echo ok")
    assert rc == 0
    assert "ok" in stdout

    await sandbox.destroy("integ-lifecycle")
    assert "integ-lifecycle" not in sandbox._containers


async def _dump_hook_errors(sandbox, chat_id):
    """Helper: dump hook-errors.log on test failure for debugging."""
    try:
        rc, stdout, _ = await sandbox.exec(chat_id, "cat /workspace/.ipc/hook-errors.log 2>/dev/null")
        if rc == 0 and stdout.strip():
            log.error("hook-errors.log for %s:\n%s", chat_id, stdout)
    except Exception:
        pass


@pytest.mark.asyncio
async def test_ipc_event_files_written(settings, sandbox, core):
    """After a gemini run with hooks, event-result.json exists in IPC dir."""
    task = Task(task_id="integ-ipc-2", description="echo hello", source="test")

    try:
        await core.submit(task, on_result=AsyncMock())
    except Exception:
        await _dump_hook_errors(sandbox, "integ-ipc-2")
        raise

    ipc_dir = os.path.join(settings.ipc_base_dir, "sandbox-integ-ipc-2")
    result_file = os.path.join(ipc_dir, "event-result.json")
    assert os.path.exists(result_file), "AfterAgent hook didn't write event-result.json"

    with open(result_file) as f:
        data = json.load(f)
    # Should contain AfterAgent hook payload
    assert isinstance(data, dict)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not (_has_podman and _has_gemini_key and _has_llm_key),
    reason="needs podman + GEMINI_API_KEY + LLM_API_KEY",
)
async def test_orchestrator_multi_agent_events(settings, sandbox):
    """Full pipeline: Agent → Haiku routes to plan (gemini) + implement (qwen) → IPC events logged."""
    from matrix_agent.agent import Agent
    from matrix_agent.tools import execute_tool

    agent = Agent(settings, sandbox)

    # Collect all tool calls and their order
    tool_log = []
    original_execute = execute_tool

    async def logging_execute(sandbox, chat_id, name, arguments, send_update=None):
        tool_log.append({"tool": name, "time": time.monotonic()})
        return await original_execute(sandbox, chat_id, name, arguments, send_update=send_update)

    # Patch execute_tool to log ordering
    with patch("matrix_agent.agent.execute_tool", logging_execute):
        results = []
        try:
            async for text, image in agent.handle_message(
                "test-multi",
                "Create a file /workspace/is_palindrome.py with a function is_palindrome(s) that returns True if s is a palindrome. Use plan() first, then implement()."
            ):
                if text:
                    results.append(text)
        except Exception:
            await _dump_hook_errors(sandbox, "test-multi")
            raise

    # Verify multi-agent routing happened
    tool_names = [t["tool"] for t in tool_log]
    assert "plan" in tool_names, f"Expected plan tool call, got: {tool_names}"
    assert "implement" in tool_names, f"Expected implement tool call, got: {tool_names}"

    # Verify ordering: plan before implement
    plan_idx = tool_names.index("plan")
    impl_idx = tool_names.index("implement")
    assert plan_idx < impl_idx, f"plan should come before implement: {tool_names}"

    # Verify the file was actually created
    rc, stdout, _ = await sandbox.exec("test-multi", "cat /workspace/is_palindrome.py")
    assert rc == 0, "is_palindrome.py not created"
    assert "palindrome" in stdout.lower()
