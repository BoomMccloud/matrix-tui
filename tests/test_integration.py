"""Integration tests for the channel → core → container → result pipeline.

Requirements:
- podman on PATH
- GEMINI_API_KEY in environment (use: uv run --env-file .env pytest ...)
- Sandbox image built: podman build -t matrix-agent-sandbox:latest -f Containerfile .
"""

import logging
import os
import shutil
from types import SimpleNamespace

import pytest

from matrix_agent.channels import Task
from matrix_agent.core import AgentCore
from matrix_agent.sandbox import SandboxManager

log = logging.getLogger(__name__)

# --------------- skip conditions --------------- #

_has_podman = shutil.which("podman") is not None
_has_gemini_key = bool(os.environ.get("GEMINI_API_KEY", ""))

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


@pytest.mark.asyncio
@pytest.mark.xfail(reason="IPC watching not implemented yet (phase 2)")
async def test_ipc_needs_help(core, sandbox, settings):
    """Container writes needs_help.json to .ipc/ → core detects it."""
    task = Task(
        task_id="integ-ipc",
        description='Write a file /workspace/.ipc/needs_help.json with content: {"question": "What should I do?"}',
        source="test",
    )

    await core.submit(task, on_result=lambda r: None, on_error=lambda e: None)

    ipc_path = os.path.join(settings.ipc_base_dir, "sandbox-integ-ipc", "needs_help.json")
    assert os.path.exists(ipc_path), f"needs_help.json not found at {ipc_path}"
