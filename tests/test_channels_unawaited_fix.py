
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from aiohttp.test_utils import TestClient, TestServer
from matrix_agent.channels import GitHubChannel
import hmac
import hashlib

def _make_task_runner():
    tr = MagicMock()
    tr._processing = set()
    tr.enqueue = AsyncMock()
    return tr

def _sign(secret: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"

async def _post(client, payload, secret="test-secret", event="issues"):
    body = json.dumps(payload).encode()
    sig = _sign(secret, body)
    return await client.post(
        "/webhook/github",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": event,
        },
    )

@pytest.mark.asyncio
async def test_webhook_issue_comment_unawaited_subprocess():
    """Reproduce the unawaited subprocess in issue_comment handler."""
    task_runner = _make_task_runner()
    settings = SimpleNamespace(
        github_webhook_port=0,
        github_webhook_secret="test-secret",
        github_token="ghp_fake",
        github_repo="owner/repo",
    )
    channel = GitHubChannel(task_runner=task_runner, settings=settings)
    app = channel._make_app()
    
    payload = {
        "action": "created",
        "issue": {
            "number": 7,
            "labels": [{"name": "agent-task"}],
        },
        "comment": {"user": {"login": "user"}, "body": "Fix this please."},
    }

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_proc.wait = AsyncMock(return_value=0)

    async with TestClient(TestServer(app)) as client:
        with patch(
            "matrix_agent.channels.asyncio.create_subprocess_exec", return_value=mock_proc
        ) as mock_exec:
            resp = await _post(client, payload, event="issue_comment")

    assert resp.status == 202
    mock_exec.assert_called_once()
    
    # This is the check: it should be awaited properly (communicated)
    assert mock_proc.communicate.called, "Should have been called in fixed version"

    await channel.stop()
