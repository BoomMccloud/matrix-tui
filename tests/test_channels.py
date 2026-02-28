"""Tests for channel adapters and GitHubChannel webhook handling."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from matrix_agent.channels import GitHubChannel, Task


# ------------------------------------------------------------------ #
# Task dataclass
# ------------------------------------------------------------------ #


def test_task_defaults():
    """Task has sensible defaults for optional fields."""
    t = Task(task_id="t-1", description="fix the bug")
    assert t.task_id == "t-1"
    assert t.description == "fix the bug"
    assert t.repo is None
    assert t.issue_number is None
    assert t.source == ""


def test_task_full():
    """Task stores all fields when provided."""
    t = Task(
        task_id="gh-42",
        description="Add auth",
        repo="owner/repo",
        issue_number=42,
        source="github",
    )
    assert t.repo == "owner/repo"
    assert t.issue_number == 42
    assert t.source == "github"


# ------------------------------------------------------------------ #
# GitHubChannel webhook
# ------------------------------------------------------------------ #


@pytest.fixture
async def github_channel():
    """Create a GitHubChannel with mocked submit and settings."""
    from types import SimpleNamespace

    submit = AsyncMock()
    settings = SimpleNamespace(
        github_webhook_port=0,  # OS picks a free port
        github_webhook_secret="test-secret",
        github_token="ghp_fake",
    )
    channel = GitHubChannel(submit_task=submit, settings=settings)
    yield channel
    await channel.stop()


@pytest.fixture
async def client(github_channel):
    """aiohttp test client for the webhook server."""
    app = github_channel._make_app()
    async with TestClient(TestServer(app)) as c:
        yield c


def _sign(secret: str, body: bytes) -> str:
    """Compute X-Hub-Signature-256 for a payload."""
    mac = hmac.new(secret.encode(), body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


@pytest.mark.asyncio
async def test_webhook_valid_issue_labeled(client, github_channel):
    """POST with valid signature and issue payload calls submit_task."""
    payload = {
        "action": "labeled",
        "label": {"name": "agent-task"},
        "issue": {
            "number": 7,
            "title": "Fix login bug",
            "body": "The login page crashes on submit.",
        },
        "repository": {"full_name": "owner/repo"},
    }
    body = json.dumps(payload).encode()
    sig = _sign("test-secret", body)

    resp = await client.post(
        "/webhook/github",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
    )
    assert resp.status == 202

    # submit_task should have been called with a Task
    github_channel.submit_task.assert_called_once()
    call_args = github_channel.submit_task.call_args
    task = call_args[0][0] if call_args[0] else call_args[1]["task"]
    assert isinstance(task, Task)
    assert task.repo == "owner/repo"
    assert task.issue_number == 7
    assert "Fix login bug" in task.description
    assert task.source == "github"


@pytest.mark.asyncio
async def test_webhook_bad_signature(client):
    """POST with wrong signature is rejected with 401."""
    body = b'{"action": "labeled"}'
    resp = await client.post(
        "/webhook/github",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=bad",
        },
    )
    assert resp.status == 401


@pytest.mark.asyncio
async def test_webhook_no_signature_when_secret_configured(client):
    """POST without signature header is rejected when secret is set."""
    body = b'{"action": "labeled"}'
    resp = await client.post(
        "/webhook/github",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 401


@pytest.mark.asyncio
async def test_webhook_ignores_non_agent_label(client, github_channel):
    """POST with a label other than 'agent-task' is ignored."""
    payload = {
        "action": "labeled",
        "label": {"name": "bug"},
        "issue": {"number": 1, "title": "x", "body": ""},
        "repository": {"full_name": "o/r"},
    }
    body = json.dumps(payload).encode()
    sig = _sign("test-secret", body)

    resp = await client.post(
        "/webhook/github",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
    )
    assert resp.status == 200  # OK but no task submitted
    github_channel.submit_task.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_no_secret_skips_validation():
    """When no webhook secret is configured, signature check is skipped."""
    from types import SimpleNamespace

    submit = AsyncMock()
    settings = SimpleNamespace(
        github_webhook_port=0,
        github_webhook_secret="",
        github_token="ghp_fake",
    )
    channel = GitHubChannel(submit_task=submit, settings=settings)
    app = channel._make_app()

    async with TestClient(TestServer(app)) as c:
        payload = {
            "action": "labeled",
            "label": {"name": "agent-task"},
            "issue": {"number": 1, "title": "Do thing", "body": "details"},
            "repository": {"full_name": "o/r"},
        }
        resp = await c.post(
            "/webhook/github",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 202
        submit.assert_called_once()

    await channel.stop()
