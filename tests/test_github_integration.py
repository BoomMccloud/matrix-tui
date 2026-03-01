"""Integration tests for the GitHub Issues pipeline as per docs/github-issues-spec.md.

These tests use a mix of real container execution and mocked external boundaries
(GitHub webhooks, GitHub API calls, and the `gh` CLI) to verify the end-to-end
TaskRunner and GitHubChannel integration without network instability.
"""

import json
import logging
import os
import shutil
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from matrix_agent.core import TaskRunner
from matrix_agent.channels import GitHubChannel
from matrix_agent.sandbox import SandboxManager

log = logging.getLogger(__name__)

_has_podman = shutil.which("podman") is not None
_has_gemini_key = bool(os.environ.get("GEMINI_API_KEY", ""))

pytestmark = [
    pytest.mark.skipif(not _has_podman, reason="podman not on PATH"),
    pytest.mark.skipif(not _has_gemini_key, reason="GEMINI_API_KEY not set"),
    pytest.mark.integration,
]

@pytest.fixture
def settings():
    return SimpleNamespace(
        podman_path="podman",
        sandbox_image="matrix-agent-sandbox:latest",
        command_timeout_seconds=120,
        coding_timeout_seconds=300,
        gemini_api_key=os.environ["GEMINI_API_KEY"],
        github_webhook_port=0,
        github_webhook_secret="test-secret",
        github_token="ghp_fake",
        dashscope_api_key=os.environ.get("DASHSCOPE_API_KEY", ""),
        ipc_base_dir=os.path.realpath("/tmp/test-github-ipc"),
        llm_model=os.environ.get("LLM_MODEL", "openrouter/anthropic/claude-haiku-4-5"),
        llm_api_key=os.environ.get("LLM_API_KEY", "fake"),
        llm_api_base=os.environ.get("LLM_API_BASE", ""),
        max_agent_turns=25,
    )

@pytest.fixture
async def sandbox(settings):
    mgr = SandboxManager(settings)
    yield mgr
    for chat_id in list(mgr._containers):
        try:
            await mgr.destroy(chat_id)
        except Exception:
            pass

@pytest.fixture
def task_runner(sandbox, settings):
    from matrix_agent.decider import Decider
    decider = Decider(settings, sandbox)
    return TaskRunner(decider, sandbox)

@pytest.fixture
async def github_channel(task_runner, settings):
    channel = GitHubChannel(task_runner=task_runner, settings=settings)
    
    # Spy on the delivery methods
    channel.deliver_result = AsyncMock()
    channel.deliver_error = AsyncMock()
    channel.send_update = AsyncMock()
    channel.is_valid = AsyncMock(return_value=True)

    await channel.start()
    yield channel
    await channel.stop()

@pytest.fixture
async def webhook_client(github_channel):
    app = github_channel._make_app()
    async with TestClient(TestServer(app)) as c:
        yield c

def _sign(secret: str, body: bytes) -> str:
    import hashlib
    import hmac
    mac = hmac.new(secret.encode(), body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"

async def _post_webhook(client, payload, secret="test-secret"):
    body = json.dumps(payload).encode()
    sig = _sign(secret, body)
    return await client.post(
        "/webhook/github",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "issues",
        },
    )

@pytest.mark.asyncio
async def test_end_to_end_successful_task(webhook_client, github_channel, sandbox):
    """
    Test 1 & 2: Label an issue -> Agent picks it up, writes code, creates PR, delivers result.
    """
    # 1. Setup mock `gh` in a wrapper script so it intercepts the container call
    original_create = sandbox.create
    
    async def _mocked_create(chat_id, **kwargs):
        name = await original_create(chat_id, **kwargs)
        # Inject dummy `gh` binary
        script = "#!/bin/sh\\necho https://github.com/mock/repo/pull/1"
        await sandbox.exec(chat_id, f"echo '{script}' > /usr/local/bin/gh && chmod +x /usr/local/bin/gh")
        
        # Initialize a fake git repo with a LOCAL remote to bypass auth issues
        setup_cmds = [
            "git config --global user.email 'agent@test.com'",
            "git config --global user.name 'Agent'",
            "mkdir -p /tmp/remote.git",
            "cd /tmp/remote.git && git init --bare",
            "mkdir -p /workspace/repo",
            "cd /workspace/repo && git init",
            "cd /workspace/repo && git remote add origin /tmp/remote.git",
            "cd /workspace/repo && touch README.md && git add README.md && git commit -m 'initial'",
            "cd /workspace/repo && git push origin master"
        ]
        for cmd in setup_cmds:
            await sandbox.exec(chat_id, cmd)
        return name
    
    # Mock host-level `gh` calls in GitHubChannel (post comment, backfill comments)
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"[]", b""))
    mock_proc.wait = AsyncMock(return_value=0)

    # Use a side_effect to only mock `gh` calls and let `podman` pass through
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def mocked_create_subprocess_exec(program, *args, **kwargs):
        if program == "gh":
            return mock_proc
        return await real_create_subprocess_exec(program, *args, **kwargs)

    with patch.object(sandbox, "create", new=_mocked_create), \
         patch("matrix_agent.channels.asyncio.create_subprocess_exec", side_effect=mocked_create_subprocess_exec):
        
        payload = {
            "action": "labeled",
            "label": {"name": "agent-task"},
            "issue": {
                "number": 101,
                "title": "Create a dummy script",
                "body": "Write a script named script.py in the repo that prints 'hello' and create a PR.",
            },
            "repository": {"full_name": "owner/repo"},
        }
        
        # 2. Fire the webhook
        resp = await _post_webhook(webhook_client, payload)
        assert resp.status == 202
        
        # 3. Wait until the container finishes executing
        for _ in range(120):
            if github_channel.deliver_result.called or github_channel.deliver_error.called:
                break
            await asyncio.sleep(1)
            
    # 4. Assert success and PR creation
    assert not github_channel.deliver_error.called
    github_channel.deliver_result.assert_called_once()
    
    # Check what result was delivered
    args = github_channel.deliver_result.call_args[0]
    task_id, result_text = args[0], args[1]
    
    assert task_id == "gh-101"
    assert "https://github.com/mock/repo/pull/1" in result_text

@pytest.mark.asyncio
async def test_reconcile_cleans_closed_issue(sandbox, github_channel, task_runner):
    """
    Test 6 & 8: Close issue -> container cleaned up by reconcile.
    """
    # Create an active container
    await sandbox.create("gh-42")
    task_runner._channels["gh-42"] = github_channel
    
    # Simulate the GitHub issue losing the label or being closed
    github_channel.is_valid.return_value = False
    
    # Trigger reconcile loop
    await task_runner.reconcile()
    
    # Verify container was destroyed
    assert "gh-42" not in sandbox._containers


@pytest.mark.asyncio
async def test_agent_failure_delivers_error(sandbox, github_channel, task_runner):
    """
    Test 3: Agent fails -> "Failed" comment posted via deliver_error.
    """
    # Patch the decider to raise an exception
    original_handle = task_runner.decider.handle_message

    async def failing_handle(chat_id, user_text, send_update=None, system_prompt=None):
        raise RuntimeError("LLM quota exceeded")
        yield  # noqa: F541 — makes this an async generator

    task_runner.decider.handle_message = failing_handle

    try:
        await task_runner.enqueue("gh-999", "do something", github_channel)

        for _ in range(10):
            if github_channel.deliver_error.called:
                break
            await asyncio.sleep(0.5)

        github_channel.deliver_error.assert_called_once()
        args = github_channel.deliver_error.call_args[0]
        assert args[0] == "gh-999"
        assert "LLM quota exceeded" in args[1]
        github_channel.deliver_result.assert_not_called()
    finally:
        task_runner.decider.handle_message = original_handle
        await task_runner._cleanup("gh-999")
