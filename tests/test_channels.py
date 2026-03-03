"""Tests for ChannelAdapter ABC and GitHubChannel webhook handling."""

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from matrix_agent.channels import GitHubChannel, ChannelAdapter
from tests.conftest import SubprocessMocker


# ------------------------------------------------------------------ #
# ChannelAdapter ABC
# ------------------------------------------------------------------ #


def test_channel_adapter_has_required_abstract_methods():
    """ChannelAdapter defines all methods from the spec."""
    required = {
        "start",
        "stop",
        "send_update",
        "deliver_result",
        "deliver_error",
        "is_valid",
    }
    abstract = set(ChannelAdapter.__abstractmethods__)
    assert required == abstract


# ------------------------------------------------------------------ #
# GitHubChannel Helpers & Fixtures
# ------------------------------------------------------------------ #


def _make_task_runner():
    tr = MagicMock()
    tr._processing = set()
    tr.enqueue = AsyncMock()
    return tr


@pytest.fixture
async def github_channel():
    """Create a GitHubChannel with mocked task_runner and settings."""
    task_runner = _make_task_runner()
    settings = SimpleNamespace(
        github_webhook_port=0,
        github_webhook_secret="test-secret",
        github_token="ghp_fake",
        github_repo="owner/repo",
    )
    channel = GitHubChannel(task_runner=task_runner, settings=settings)
    yield channel
    await channel.stop()


@pytest.fixture
def gh_mocker():
    """SubprocessMocker pre-configured for gh CLI calls."""
    mocker = SubprocessMocker()
    mocker.on("gh", "issue", "comment")
    mocker.on("gh", "issue", "close")
    mocker.on("gh", "issue", "view")
    mocker.on("gh", "issue", "list")
    mocker.on("gh", "api")
    return mocker


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


def _labeled_payload(issue_number=7, label="agent-task"):
    return {
        "action": "labeled",
        "label": {"name": label},
        "issue": {
            "number": issue_number,
            "title": "Fix login bug",
            "body": "The login page crashes on submit.",
            "labels": [{"name": label}],
        },
        "repository": {"full_name": "owner/repo"},
    }


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


# ------------------------------------------------------------------ #
# GitHubChannel Webhook Tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_webhook_valid_issue_labeled(client, github_channel):
    """POST with valid signature and issue payload calls task_runner.enqueue."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_proc.wait = AsyncMock(return_value=0)

    with patch(
        "matrix_agent.channels.asyncio.create_subprocess_exec", return_value=mock_proc
    ):
        resp = await _post(client, _labeled_payload())

    assert resp.status == 202
    github_channel.task_runner.enqueue.assert_called()
    call_args = github_channel.task_runner.enqueue.call_args
    assert call_args[0][0] == "gh-7"  # task_id
    assert "Fix login bug" in call_args[0][1]  # message


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
            "X-GitHub-Event": "issues",
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
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "issues",
        },
    )
    assert resp.status == 401


@pytest.mark.asyncio
async def test_webhook_ignores_non_agent_label(client, github_channel):
    """POST with a label other than 'agent-task' is ignored."""
    resp = await _post(client, _labeled_payload(label="bug"))
    assert resp.status == 200
    github_channel.task_runner.enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_no_secret_skips_validation():
    """When no webhook secret is configured, signature check is skipped."""
    task_runner = _make_task_runner()
    settings = SimpleNamespace(
        github_webhook_port=0,
        github_webhook_secret="",
        github_token="ghp_fake",
        github_repo="owner/repo",
    )
    channel = GitHubChannel(task_runner=task_runner, settings=settings)
    app = channel._make_app()

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_proc.wait = AsyncMock(return_value=0)

    async with TestClient(TestServer(app)) as c:
        payload = _labeled_payload(issue_number=1)
        with patch(
            "matrix_agent.channels.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            resp = await c.post(
                "/webhook/github",
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "issues",
                },
            )
        assert resp.status == 202
        task_runner.enqueue.assert_called()

    await channel.stop()


@pytest.mark.asyncio
async def test_webhook_idempotency_skips_duplicate(client, github_channel):
    """Re-labeling an in-progress issue is skipped (spec test 5)."""
    github_channel.task_runner._processing.add("gh-7")
    resp = await _post(client, _labeled_payload())
    assert resp.status == 200
    github_channel.task_runner.enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_issue_comment(client, github_channel):
    """issue_comment webhook enqueues the comment body (spec test 4)."""
    # Simulate task already processing (follow-up comment, no "working" comment posted)
    github_channel.task_runner._processing.add("gh-7")
    payload = {
        "action": "created",
        "issue": {
            "number": 7,
            "labels": [{"name": "agent-task"}],
        },
        "comment": {"body": "Please also fix the logout page."},
    }
    resp = await _post(client, payload, event="issue_comment")
    assert resp.status == 202
    github_channel.task_runner.enqueue.assert_called_once()
    call_args = github_channel.task_runner.enqueue.call_args
    assert call_args[0][0] == "gh-7"
    assert "logout page" in call_args[0][1]


# ------------------------------------------------------------------ #
# Scenario 3: Backfill comment filtering in GitHubChannel
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_webhook_backfill_filtering(client, github_channel):
    """Webhook filters out bot comments and empty comments from backfill."""
    # Mock responses for gh subprocesses:
    # 1. gh issue comment (Working...)
    # 2. gh api (list comments)
    mock_proc_working = MagicMock()
    mock_proc_working.returncode = 0
    mock_proc_working.communicate = AsyncMock(return_value=(b"", b""))

    comments_bodies_json = json.dumps(
        [
            "🤖 Working on this issue...",  # Bot comment
            "Real user comment",  # Keep
            "✅ Completed — fixed",  # Bot comment
            "❌ Failed: error",  # Bot comment
            "Another real comment",  # Keep
            "   ",  # Empty
        ]
    ).encode()
    mock_proc_api = MagicMock()
    mock_proc_api.returncode = 0
    mock_proc_api.communicate = AsyncMock(return_value=(comments_bodies_json, b""))

    with patch(
        "matrix_agent.channels.asyncio.create_subprocess_exec",
        side_effect=[mock_proc_working, mock_proc_api],
    ):
        resp = await _post(client, _labeled_payload())

    assert resp.status == 202
    # Enqueue should be called twice: 1. Title/Body, 2. Filtered comments
    assert github_channel.task_runner.enqueue.call_count == 2

    # Verify second call contains only non-bot comments
    call_args = github_channel.task_runner.enqueue.call_args_list[1][0]
    context = call_args[1]
    assert "Real user comment" in context
    assert "Another real comment" in context
    assert "🤖 Working" not in context
    assert "✅ Completed" not in context
    assert "❌ Failed" not in context


# ------------------------------------------------------------------ #
# Scenario 4: issue_comment bot-comment ignoring in GitHubChannel
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_webhook_ignores_bot_comment_by_login(client, github_channel):
    """issue_comment webhook ignores comments from bot users."""
    payload = {
        "action": "created",
        "issue": {"number": 7, "labels": [{"name": "agent-task"}]},
        "comment": {"user": {"login": "github-actions[bot]"}, "body": "Build passed"},
    }
    resp = await _post(client, payload, event="issue_comment")
    assert resp.status == 200
    assert await resp.text() == "ignoring bot comment"
    github_channel.task_runner.enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_ignores_bot_comment_by_prefix(client, github_channel):
    """issue_comment webhook ignores comments starting with bot emojis."""
    payload = {
        "action": "created",
        "issue": {"number": 7, "labels": [{"name": "agent-task"}]},
        "comment": {"user": {"login": "realuser"}, "body": "✅ Success!"},
    }
    resp = await _post(client, payload, event="issue_comment")
    assert resp.status == 200
    assert await resp.text() == "ignoring bot comment"
    github_channel.task_runner.enqueue.assert_not_called()


# ------------------------------------------------------------------ #
# GitHubChannel Method Tests (deliver_error, deliver_result, etc)
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_deliver_error(github_channel):
    """deliver_error posts a comment with the error message."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    with patch(
        "matrix_agent.channels.asyncio.create_subprocess_exec", return_value=mock_proc
    ) as mock_exec:
        await github_channel.deliver_error("gh-123", "Internal error")

    mock_exec.assert_called_once_with(
        "gh",
        "issue",
        "comment",
        "123",
        "--body",
        "❌ Failed: Internal error",
        stdout=-1,
        stderr=-1,
    )


@pytest.mark.asyncio
async def test_deliver_result_max_turns(github_channel):
    """deliver_result with status='max_turns' posts a bot-prefixed comment and does NOT close."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    with patch(
        "matrix_agent.channels.asyncio.create_subprocess_exec", return_value=mock_proc
    ) as mock_exec:
        await github_channel.deliver_result(
            "gh-123", "Hit turn limit", status="max_turns"
        )

    # Should only call comment, not close
    mock_exec.assert_called_once_with(
        "gh",
        "issue",
        "comment",
        "123",
        "--body",
        "🤖 Hit turn limit",
        stdout=-1,
        stderr=-1,
    )


@pytest.mark.asyncio
async def test_deliver_result_completed(github_channel):
    """deliver_result with default status posts completion comment AND closes the issue."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    with patch(
        "matrix_agent.channels.asyncio.create_subprocess_exec", return_value=mock_proc
    ) as mock_exec:
        await github_channel.deliver_result("gh-123", "Fixed everything")

    assert mock_exec.call_count == 2
    # First call: comment
    mock_exec.assert_any_call(
        "gh",
        "issue",
        "comment",
        "123",
        "--body",
        "✅ Completed — Fixed everything",
        stdout=-1,
        stderr=-1,
    )
    # Second call: close
    mock_exec.assert_any_call("gh", "issue", "close", "123", stdout=-1, stderr=-1)


@pytest.mark.asyncio
async def test_recover_tasks_returns_open_issues(github_channel):
    """recover_tasks() returns (task_id, message) pairs for open agent-task issues."""
    gh_output = json.dumps(
        [
            {"number": 10, "title": "Fix bug", "body": "Details here"},
            {"number": 11, "title": "Add feature", "body": "More details"},
        ]
    ).encode()

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(gh_output, b""))

    with patch(
        "matrix_agent.channels.asyncio.create_subprocess_exec", return_value=mock_proc
    ):
        results = await github_channel.recover_tasks()

    assert len(results) == 2
    assert results[0] == ("gh-10", "Repository: owner/repo\n\n# Fix bug\n\nDetails here")
    assert results[1] == ("gh-11", "Repository: owner/repo\n\n# Add feature\n\nMore details")


@pytest.mark.asyncio
async def test_recover_tasks_skips_when_no_repo():
    """recover_tasks() returns empty list when github_repo is not set."""
    task_runner = _make_task_runner()
    settings = SimpleNamespace(
        github_webhook_port=0,
        github_webhook_secret="",
        github_token="ghp_fake",
        github_repo="",
    )
    channel = GitHubChannel(task_runner=task_runner, settings=settings)

    results = await channel.recover_tasks()
    assert results == []
