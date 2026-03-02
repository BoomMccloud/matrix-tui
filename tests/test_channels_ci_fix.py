"""Tests for CI fix detection in GitHubChannel._handle_webhook() on reopened issues."""

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from matrix_agent.channels import GitHubChannel


def _make_task_runner():
    tr = MagicMock()
    tr._processing = set()
    tr.enqueue = AsyncMock()
    return tr


@pytest.fixture
async def github_channel():
    task_runner = _make_task_runner()
    settings = SimpleNamespace(
        github_webhook_port=0,
        github_webhook_secret="test-secret",
        github_token="ghp_fake",
    )
    channel = GitHubChannel(task_runner=task_runner, settings=settings)
    yield channel
    await channel.stop()


@pytest.fixture
async def client(github_channel):
    app = github_channel._make_app()
    async with TestClient(TestServer(app)) as c:
        yield c


def _sign(secret: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def _reopened_payload(issue_number=7):
    return {
        "action": "reopened",
        "issue": {
            "number": issue_number,
            "title": "Fix login bug",
            "body": "The login page crashes.",
            "labels": [{"name": "agent-task"}],
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


def _mock_gh_subprocess(comment_bodies=None):
    """Create a mock that returns comment_bodies for `gh api` calls and success for other gh calls."""
    if comment_bodies is None:
        comment_bodies = []

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(
        return_value=(json.dumps(comment_bodies).encode(), b""),
    )
    mock_proc.wait = AsyncMock(return_value=0)
    return mock_proc


# ------------------------------------------------------------------ #
# CI fix detection tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_webhook_reopened_with_ci_prefix_prepends_ci_fix(client, github_channel):
    """Reopened issue with ⚠️ comment prepends CI_FIX: to enqueued message."""
    ci_comment = "⚠️ CI failed on PR #5: tests red"
    mock_proc = _mock_gh_subprocess([ci_comment])

    with patch("matrix_agent.channels.asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await _post(client, _reopened_payload())

    assert resp.status == 202
    github_channel.task_runner.enqueue.assert_called()
    first_msg = github_channel.task_runner.enqueue.call_args_list[0][0][1]
    assert first_msg.startswith("CI_FIX:"), f"Expected CI_FIX: prefix, got: {first_msg[:50]}"


@pytest.mark.asyncio
async def test_webhook_reopened_without_ci_comment_normal_flow(client, github_channel):
    """Reopened issue without ⚠️ comment does NOT prepend CI_FIX:."""
    # Comments with no CI failure prefix
    mock_proc = _mock_gh_subprocess(["Just a human comment", "Another one"])

    with patch("matrix_agent.channels.asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await _post(client, _reopened_payload())

    assert resp.status == 202
    github_channel.task_runner.enqueue.assert_called()
    first_msg = github_channel.task_runner.enqueue.call_args_list[0][0][1]
    assert not first_msg.startswith("CI_FIX:"), f"Should not have CI_FIX: prefix: {first_msg[:50]}"


@pytest.mark.asyncio
async def test_webhook_reopened_with_ci_comment_skips_backfill(client, github_channel):
    """When ⚠️ comment found, backfill enqueue is skipped (only 1 enqueue call)."""
    ci_comment = "⚠️ CI failed"
    mock_proc = _mock_gh_subprocess([ci_comment, "human comment"])

    with patch("matrix_agent.channels.asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await _post(client, _reopened_payload())

    assert resp.status == 202
    # Should be exactly 1 enqueue call (the CI_FIX message, no separate backfill)
    assert github_channel.task_runner.enqueue.call_count == 1


@pytest.mark.asyncio
async def test_webhook_reopened_without_ci_comment_does_backfill(client, github_channel):
    """Without ⚠️ comment, normal backfill enqueue happens."""
    human_comments = ["Please fix the button", "Also check the CSS"]
    mock_proc = _mock_gh_subprocess(human_comments)

    with patch("matrix_agent.channels.asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await _post(client, _reopened_payload())

    assert resp.status == 202
    # Should be 2 enqueue calls: first message + backfill context
    assert github_channel.task_runner.enqueue.call_count == 2
    second_msg = github_channel.task_runner.enqueue.call_args_list[1][0][1]
    assert "Previous comments" in second_msg


@pytest.mark.asyncio
async def test_webhook_reopened_fetches_comments_before_enqueue(client, github_channel):
    """Comment fetch (gh api) happens BEFORE task_runner.enqueue."""
    ci_comment = "⚠️ CI failed"
    order = []

    async def tracking_enqueue(task_id, message, channel):
        order.append("enqueue")

    github_channel.task_runner.enqueue = AsyncMock(side_effect=tracking_enqueue)

    original_mock = _mock_gh_subprocess([ci_comment])

    async def tracking_subprocess(*args, **kwargs):
        if "gh" in args[0] if args else "":
            # Track gh api calls (comment fetch)
            if len(args) > 2 and "api" in args:
                order.append("gh_api")
        return original_mock

    with patch("matrix_agent.channels.asyncio.create_subprocess_exec", side_effect=tracking_subprocess):
        resp = await _post(client, _reopened_payload())

    assert resp.status == 202
    # enqueue should happen after at least the gh api call
    # (the "working" comment is a gh issue comment call, not gh api)
    github_channel.task_runner.enqueue.assert_called()
    first_msg = github_channel.task_runner.enqueue.call_args_list[0][0][1]
    assert first_msg.startswith("CI_FIX:")


@pytest.mark.asyncio
async def test_webhook_reopened_ignores_non_agent_task_label(client, github_channel):
    """Reopened issue without agent-task label is ignored."""
    payload = _reopened_payload()
    payload["issue"]["labels"] = [{"name": "bug"}]

    mock_proc = _mock_gh_subprocess()

    with patch("matrix_agent.channels.asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await _post(client, payload)

    assert resp.status == 200
    github_channel.task_runner.enqueue.assert_not_called()
