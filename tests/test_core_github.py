"""Tests for GitHub pipeline routing in TaskRunner._process_github()."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from matrix_agent.core import TaskRunner
from matrix_agent.channels import ChannelAdapter


class MockChannel(ChannelAdapter):
    system_prompt = "Test prompt"

    def __init__(self):
        self.results = []
        self.errors = []
        self.updates = []

    async def start(self) -> None: pass
    async def stop(self) -> None: pass

    async def send_update(self, task_id: str, text: str) -> None:
        self.updates.append((task_id, text))

    async def deliver_result(self, task_id: str, text: str, *, status: str = "completed") -> None:
        self.results.append((task_id, text))

    async def deliver_error(self, task_id: str, error: str) -> None:
        self.errors.append((task_id, error))

    async def is_valid(self, task_id: str) -> bool:
        return True


def _make_sandbox():
    sandbox = AsyncMock()
    sandbox._containers = {}

    async def fake_create(chat_id):
        sandbox._containers[chat_id] = f"sandbox-{chat_id}"
        return f"sandbox-{chat_id}"

    sandbox.create = AsyncMock(side_effect=fake_create)
    sandbox.destroy = AsyncMock()
    sandbox.exec = AsyncMock(return_value=(0, "", ""))
    sandbox.run_gemini_session = AsyncMock(
        return_value=(0, "output", "https://github.com/owner/repo/pull/1"),
    )
    sandbox.validate_work = AsyncMock(return_value=(True, []))
    return sandbox


def _make_decider(mock_responses=None):
    if mock_responses is None:
        mock_responses = [("Processed", None)]
    decider = MagicMock()

    async def mock_handle_message(chat_id, user_text, send_update=None, system_prompt=None):
        for text, image in mock_responses:
            if isinstance(text, Exception):
                raise text
            yield text, image, "completed"

    decider.handle_message = mock_handle_message
    return decider


GITHUB_MESSAGE = "Repository: owner/repo\n\n# Fix the bug\n\nDetails here"


# ------------------------------------------------------------------ #
# Routing tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_gh_task_id_routes_to_process_github():
    """gh-* task IDs call _process_github, not the decider."""
    sandbox = _make_sandbox()
    decider = _make_decider()
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    await runner.enqueue("gh-42", GITHUB_MESSAGE, channel)
    await asyncio.sleep(0.05)

    # run_gemini_session should have been called (GitHub path)
    sandbox.run_gemini_session.assert_called()
    # deliver_result should have the PR URL
    assert len(channel.results) >= 1

    await runner._cleanup("gh-42")


@pytest.mark.asyncio
async def test_non_gh_task_id_routes_to_process_matrix():
    """Non-gh-* task IDs use the decider path (Matrix)."""
    sandbox = _make_sandbox()
    decider = _make_decider([("Done", None)])
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    await runner.enqueue("room-abc", "hello", channel)
    await asyncio.sleep(0.05)

    # run_gemini_session should NOT be called
    sandbox.run_gemini_session.assert_not_called()
    # decider path should deliver a result
    assert len(channel.results) >= 1

    await runner._cleanup("room-abc")


# ------------------------------------------------------------------ #
# _process_github behavior tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_process_github_parses_repo_from_message():
    """_process_github extracts repo name from 'Repository: owner/repo' line."""
    sandbox = _make_sandbox()
    decider = _make_decider()
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    await runner.enqueue("gh-10", GITHUB_MESSAGE, channel)
    await asyncio.sleep(0.05)

    # Check run_gemini_session was called with repo_name="repo"
    call_args = sandbox.run_gemini_session.call_args
    assert call_args is not None
    # repo_name is the 4th positional arg
    repo_name = call_args[0][3] if len(call_args[0]) > 3 else call_args[1].get("repo_name")
    assert repo_name == "repo"

    await runner._cleanup("gh-10")


@pytest.mark.asyncio
async def test_process_github_clones_repo():
    """_process_github clones the repo via sandbox.exec."""
    sandbox = _make_sandbox()
    decider = _make_decider()
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    await runner.enqueue("gh-11", GITHUB_MESSAGE, channel)
    await asyncio.sleep(0.05)

    # sandbox.exec should have been called with a git clone command
    exec_calls = sandbox.exec.call_args_list
    clone_calls = [c for c in exec_calls if "git clone" in str(c) or "clone" in str(c)]
    assert len(clone_calls) >= 1, f"Expected a clone call, got: {exec_calls}"

    await runner._cleanup("gh-11")


@pytest.mark.asyncio
async def test_process_github_delivers_result_with_pr_url():
    """On success, deliver_result is called with the PR URL."""
    sandbox = _make_sandbox()
    sandbox.run_gemini_session = AsyncMock(
        return_value=(0, "output", "https://github.com/owner/repo/pull/99"),
    )
    sandbox.validate_work = AsyncMock(return_value=(True, []))
    decider = _make_decider()
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    await runner.enqueue("gh-12", GITHUB_MESSAGE, channel)
    await asyncio.sleep(0.05)

    assert len(channel.results) == 1
    assert "https://github.com/owner/repo/pull/99" in channel.results[0][1]
    assert len(channel.errors) == 0

    await runner._cleanup("gh-12")


@pytest.mark.asyncio
async def test_process_github_retries_on_validation_failure():
    """run_gemini_session called 3 times (1 initial + 2 retries) when validation always fails."""
    sandbox = _make_sandbox()
    sandbox.run_gemini_session = AsyncMock(return_value=(0, "output", None))
    sandbox.validate_work = AsyncMock(return_value=(False, ["tests failed"]))
    decider = _make_decider()
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    await runner.enqueue("gh-13", GITHUB_MESSAGE, channel)
    await asyncio.sleep(0.1)

    assert sandbox.run_gemini_session.call_count == 3

    await runner._cleanup("gh-13")


@pytest.mark.asyncio
async def test_process_github_delivers_error_after_max_retries():
    """After exhausting retries, deliver_error is called."""
    sandbox = _make_sandbox()
    sandbox.run_gemini_session = AsyncMock(return_value=(0, "output", None))
    sandbox.validate_work = AsyncMock(return_value=(False, ["tests failed"]))
    decider = _make_decider()
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    await runner.enqueue("gh-14", GITHUB_MESSAGE, channel)
    await asyncio.sleep(0.1)

    assert len(channel.errors) == 1
    assert "tests failed" in channel.errors[0][1]
    assert len(channel.results) == 0

    await runner._cleanup("gh-14")


@pytest.mark.asyncio
async def test_process_github_retry_prompt_includes_failure_reasons():
    """Retry prompt includes failure text from validate_work."""
    sandbox = _make_sandbox()

    call_prompts = []

    async def capture_session(chat_id, prompt, on_chunk, repo_name):
        call_prompts.append(prompt)
        return (0, "output", "https://github.com/owner/repo/pull/1")

    sandbox.run_gemini_session = AsyncMock(side_effect=capture_session)
    # First validation fails, second succeeds
    sandbox.validate_work = AsyncMock(
        side_effect=[(False, ["lint errors: E501"]), (True, [])],
    )
    decider = _make_decider()
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    await runner.enqueue("gh-15", GITHUB_MESSAGE, channel)
    await asyncio.sleep(0.1)

    assert len(call_prompts) == 2
    assert "lint errors: E501" in call_prompts[1]

    await runner._cleanup("gh-15")


@pytest.mark.asyncio
async def test_process_github_succeeds_on_second_attempt():
    """First validation fails, second succeeds -> deliver_result called."""
    sandbox = _make_sandbox()
    sandbox.run_gemini_session = AsyncMock(
        return_value=(0, "output", "https://github.com/owner/repo/pull/1"),
    )
    sandbox.validate_work = AsyncMock(
        side_effect=[(False, ["tests failed"]), (True, [])],
    )
    decider = _make_decider()
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    await runner.enqueue("gh-16", GITHUB_MESSAGE, channel)
    await asyncio.sleep(0.1)

    assert len(channel.results) == 1
    assert len(channel.errors) == 0

    await runner._cleanup("gh-16")


@pytest.mark.asyncio
async def test_process_github_creates_container_if_not_exists():
    """sandbox.create called when container doesn't exist for gh-* task."""
    sandbox = _make_sandbox()
    decider = _make_decider()
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    await runner.enqueue("gh-17", GITHUB_MESSAGE, channel)
    await asyncio.sleep(0.05)

    sandbox.create.assert_called_with("gh-17")

    await runner._cleanup("gh-17")


@pytest.mark.asyncio
async def test_process_github_delivers_error_on_bad_message():
    """deliver_error called when message doesn't contain Repository: line."""
    sandbox = _make_sandbox()
    decider = _make_decider()
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    await runner.enqueue("gh-18", "no repo info here", channel)
    await asyncio.sleep(0.05)

    assert len(channel.errors) == 1
    assert "repository" in channel.errors[0][1].lower() or "parse" in channel.errors[0][1].lower()

    await runner._cleanup("gh-18")


@pytest.mark.asyncio
async def test_process_github_ci_fix_uses_fix_ci_prompt():
    """CI_FIX: prefix in message triggers /fix-ci prompt."""
    sandbox = _make_sandbox()

    call_prompts = []

    async def capture_session(chat_id, prompt, on_chunk, repo_name):
        call_prompts.append(prompt)
        return (0, "output", "https://github.com/owner/repo/pull/1")

    sandbox.run_gemini_session = AsyncMock(side_effect=capture_session)
    sandbox.validate_work = AsyncMock(return_value=(True, []))
    decider = _make_decider()
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    ci_message = f"CI_FIX: Tests failed\n\n{GITHUB_MESSAGE}"
    await runner.enqueue("gh-19", ci_message, channel)
    await asyncio.sleep(0.05)

    assert len(call_prompts) >= 1
    assert "/fix-ci" in call_prompts[0]

    await runner._cleanup("gh-19")
