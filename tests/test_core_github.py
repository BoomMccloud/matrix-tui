"""Tests for GitHub pipeline routing in TaskRunner._process_github().

Uses real TaskRunner + real SandboxManager + TestChannel.
Only external boundary mocked: asyncio.create_subprocess_exec.
"""

import asyncio
import os
from unittest.mock import patch, MagicMock

import pytest

from matrix_agent.core import TaskRunner
from matrix_agent.sandbox import SandboxManager
from conftest import SubprocessMocker, StubChannel


GITHUB_MESSAGE = "Repository: owner/repo\n\n# Fix the bug\n\nDetails here"


def _make_runner(settings, subprocess_mocker):
    """Create real TaskRunner with real SandboxManager, patching only subprocess."""
    sandbox = SandboxManager(settings)
    decider = MagicMock()

    # Default decider for Matrix path
    async def mock_handle_message(chat_id, user_text, send_update=None, system_prompt=None):
        yield "Done", None, "completed"

    decider.handle_message = mock_handle_message
    runner = TaskRunner(decider, sandbox)
    return runner, sandbox


def _setup_default_subprocess(mocker, ipc_dir):
    """Configure subprocess mocker for a successful GitHub pipeline run."""
    # Container create
    mocker.on("podman", "run", stdout=b"container-id")
    # All exec calls succeed by default
    mocker.on("podman", "exec")
    # Container lifecycle
    mocker.on("podman", "stop")
    mocker.on("podman", "rm")


async def _run_pipeline(runner, task_id, message, channel):
    """Enqueue and wait for processing to complete."""
    await runner.enqueue(task_id, message, channel)
    # Wait for worker to process
    for _ in range(50):
        if channel.results or channel.errors:
            break
        await asyncio.sleep(0.02)
    await runner._cleanup(task_id)


# ------------------------------------------------------------------ #
# Routing tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_gh_task_id_routes_to_process_github(settings):
    """gh-* task IDs call _process_github path."""
    mocker = SubprocessMocker()
    _setup_default_subprocess(mocker, None)
    runner, sandbox = _make_runner(settings, mocker)
    channel = StubChannel()

    # Write pr-url.txt to IPC dir
    ipc_dir = os.path.join(settings.ipc_base_dir, "sandbox-gh-42")
    os.makedirs(ipc_dir, exist_ok=True)
    with open(os.path.join(ipc_dir, "pr-url.txt"), "w") as f:
        f.write("https://github.com/owner/repo/pull/1")
    with open(os.path.join(ipc_dir, "acceptance-criteria.md"), "w") as f:
        f.write("- Feature works")
    with open(os.path.join(ipc_dir, "changed-files.txt"), "w") as f:
        f.write("fix.py\n")

    # Mock git diff to return clean (no scope creep)
    async def mock_exec(chat_id, cmd):
        if "git diff --name-only" in cmd:
            return (0, "fix.py\n", "")
        if "ruff" in cmd or "pytest" in cmd:
            return (0, "all passed", "")
        if "git rev-parse --abbrev-ref HEAD" in cmd:
            return (0, "agent/fix-42\n", "")
        if "git push" in cmd:
            return (0, "", "")
        if "gh pr create" in cmd or "gh pr view" in cmd:
            return (0, "https://github.com/owner/repo/pull/1\n", "")
        return (0, "", "")

    with patch("asyncio.create_subprocess_exec", mocker):
        sandbox.exec = mock_exec
        await _run_pipeline(runner, "gh-42", GITHUB_MESSAGE, channel)

    assert len(channel.results) >= 1
    assert "pull/1" in channel.results[0][1]
    assert len(channel.errors) == 0


@pytest.mark.asyncio
async def test_non_gh_task_id_routes_to_process_matrix(settings):
    """Non-gh-* task IDs use the decider path (Matrix)."""
    mocker = SubprocessMocker()
    _setup_default_subprocess(mocker, None)
    runner, sandbox = _make_runner(settings, mocker)
    channel = StubChannel()

    with patch("asyncio.create_subprocess_exec", mocker):
        await _run_pipeline(runner, "room-abc", "hello", channel)

    assert len(channel.results) >= 1


# ------------------------------------------------------------------ #
# _process_github behavior tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_process_github_clones_repo(settings):
    """_process_github clones the repo via sandbox.exec."""
    mocker = SubprocessMocker()
    _setup_default_subprocess(mocker, None)
    runner, sandbox = _make_runner(settings, mocker)
    channel = StubChannel()

    ipc_dir = os.path.join(settings.ipc_base_dir, "sandbox-gh-11")
    os.makedirs(ipc_dir, exist_ok=True)
    with open(os.path.join(ipc_dir, "pr-url.txt"), "w") as f:
        f.write("https://github.com/owner/repo/pull/1")
    with open(os.path.join(ipc_dir, "acceptance-criteria.md"), "w") as f:
        f.write("- Done")
    with open(os.path.join(ipc_dir, "changed-files.txt"), "w") as f:
        f.write("fix.py\n")

    exec_cmds = []

    async def tracking_exec(chat_id, cmd):
        exec_cmds.append(cmd)
        if "git diff --name-only" in cmd:
            return (0, "fix.py\n", "")
        return (0, "", "")

    sandbox.exec = tracking_exec

    with patch("asyncio.create_subprocess_exec", mocker):
        await _run_pipeline(runner, "gh-11", GITHUB_MESSAGE, channel)

    clone_calls = [c for c in exec_cmds if "git clone" in c]
    assert len(clone_calls) >= 1


@pytest.mark.asyncio
async def test_process_github_delivers_result_with_pr_url(settings):
    """On success, deliver_result is called with the PR URL."""
    mocker = SubprocessMocker()
    _setup_default_subprocess(mocker, None)
    runner, sandbox = _make_runner(settings, mocker)
    channel = StubChannel()

    ipc_dir = os.path.join(settings.ipc_base_dir, "sandbox-gh-12")
    os.makedirs(ipc_dir, exist_ok=True)
    with open(os.path.join(ipc_dir, "pr-url.txt"), "w") as f:
        f.write("https://github.com/owner/repo/pull/99")
    with open(os.path.join(ipc_dir, "acceptance-criteria.md"), "w") as f:
        f.write("- Done")
    with open(os.path.join(ipc_dir, "changed-files.txt"), "w") as f:
        f.write("fix.py\n")

    async def mock_exec(chat_id, cmd):
        if "git diff --name-only" in cmd:
            return (0, "fix.py\n", "")
        if "git rev-parse --abbrev-ref HEAD" in cmd:
            return (0, "agent/fix-12\n", "")
        if "git push" in cmd:
            return (0, "", "")
        if "gh pr create" in cmd or "gh pr view" in cmd:
            return (0, "https://github.com/owner/repo/pull/99\n", "")
        return (0, "", "")

    sandbox.exec = mock_exec

    with patch("asyncio.create_subprocess_exec", mocker):
        await _run_pipeline(runner, "gh-12", GITHUB_MESSAGE, channel)

    assert len(channel.results) == 1
    assert "pull/99" in channel.results[0][1]
    assert len(channel.errors) == 0


@pytest.mark.asyncio
async def test_process_github_retries_on_validation_failure(settings):
    """run_gemini_session called 3 times when validation always fails."""
    mocker = SubprocessMocker()
    _setup_default_subprocess(mocker, None)
    runner, sandbox = _make_runner(settings, mocker)
    channel = StubChannel()

    ipc_dir = os.path.join(settings.ipc_base_dir, "sandbox-gh-13")
    os.makedirs(ipc_dir, exist_ok=True)
    # No pr-url.txt → validation fails

    gemini_call_count = 0

    async def counting_code_stream(*args, **kwargs):
        nonlocal gemini_call_count
        gemini_call_count += 1
        return (0, "output", "")

    sandbox.code_stream = counting_code_stream

    async def mock_exec(chat_id, cmd):
        if "git diff --name-only" in cmd:
            return (0, "fix.py\n", "")
        if "git clone" in cmd or "test -d" in cmd:
            return (0, "", "")
        # Tests/lint fail
        return (1, "FAILED", "")

    sandbox.exec = mock_exec

    with patch("asyncio.create_subprocess_exec", mocker):
        await _run_pipeline(runner, "gh-13", GITHUB_MESSAGE, channel)

    assert gemini_call_count == 3  # 1 initial + 2 retries
    assert len(channel.errors) == 1


@pytest.mark.asyncio
async def test_process_github_delivers_error_after_max_retries(settings):
    """After exhausting retries, deliver_error is called."""
    mocker = SubprocessMocker()
    _setup_default_subprocess(mocker, None)
    runner, sandbox = _make_runner(settings, mocker)
    channel = StubChannel()

    ipc_dir = os.path.join(settings.ipc_base_dir, "sandbox-gh-14")
    os.makedirs(ipc_dir, exist_ok=True)

    async def mock_code_stream(*args, **kwargs):
        return (0, "output", "")

    sandbox.code_stream = mock_code_stream

    async def mock_exec(chat_id, cmd):
        if "git diff --name-only" in cmd:
            return (0, "fix.py\n", "")
        if "git clone" in cmd or "test -d" in cmd:
            return (0, "", "")
        return (1, "FAILED tests", "")

    sandbox.exec = mock_exec

    with patch("asyncio.create_subprocess_exec", mocker):
        await _run_pipeline(runner, "gh-14", GITHUB_MESSAGE, channel)

    assert len(channel.errors) == 1
    assert len(channel.results) == 0


@pytest.mark.asyncio
async def test_process_github_retry_prompt_includes_failure_reasons(settings):
    """Retry prompt includes failure text from validate_work."""
    mocker = SubprocessMocker()
    _setup_default_subprocess(mocker, None)
    runner, sandbox = _make_runner(settings, mocker)
    channel = StubChannel()

    ipc_dir = os.path.join(settings.ipc_base_dir, "sandbox-gh-15")
    os.makedirs(ipc_dir, exist_ok=True)
    with open(os.path.join(ipc_dir, "pr-url.txt"), "w") as f:
        f.write("https://github.com/owner/repo/pull/1")
    with open(os.path.join(ipc_dir, "acceptance-criteria.md"), "w") as f:
        f.write("- Done")
    with open(os.path.join(ipc_dir, "changed-files.txt"), "w") as f:
        f.write("fix.py\n")

    call_prompts = []
    call_count = [0]

    async def tracking_code_stream(chat_id, prompt, on_chunk, **kwargs):
        call_prompts.append(prompt)
        return (0, "output", "")

    sandbox.code_stream = tracking_code_stream

    async def mock_exec(chat_id, cmd):
        if "git diff --name-only" in cmd:
            return (0, "fix.py\n", "")
        if "git clone" in cmd or "test -d" in cmd:
            return (0, "", "")
        if "git rev-parse --abbrev-ref HEAD" in cmd:
            return (0, "agent/fix-15\n", "")
        if "git push" in cmd:
            return (0, "", "")
        if "gh pr create" in cmd or "gh pr view" in cmd:
            return (0, "https://github.com/owner/repo/pull/1\n", "")
        if call_count[0] == 0:
            call_count[0] += 1
            return (1, "lint errors: E501", "")
        return (0, "all passed", "")

    sandbox.exec = mock_exec

    with patch("asyncio.create_subprocess_exec", mocker):
        await _run_pipeline(runner, "gh-15", GITHUB_MESSAGE, channel)

    assert len(call_prompts) == 2
    assert "lint errors: E501" in call_prompts[1]


@pytest.mark.asyncio
async def test_process_github_creates_container(settings):
    """sandbox.create called when container doesn't exist for gh-* task."""
    mocker = SubprocessMocker()
    _setup_default_subprocess(mocker, None)
    runner, sandbox = _make_runner(settings, mocker)
    channel = StubChannel()

    ipc_dir = os.path.join(settings.ipc_base_dir, "sandbox-gh-17")
    os.makedirs(ipc_dir, exist_ok=True)
    with open(os.path.join(ipc_dir, "pr-url.txt"), "w") as f:
        f.write("https://github.com/owner/repo/pull/1")
    with open(os.path.join(ipc_dir, "acceptance-criteria.md"), "w") as f:
        f.write("- Done")
    with open(os.path.join(ipc_dir, "changed-files.txt"), "w") as f:
        f.write("fix.py\n")

    async def mock_exec(chat_id, cmd):
        if "git diff --name-only" in cmd:
            return (0, "fix.py\n", "")
        return (0, "", "")

    sandbox.exec = mock_exec

    with patch("asyncio.create_subprocess_exec", mocker):
        await _run_pipeline(runner, "gh-17", GITHUB_MESSAGE, channel)

    # Container should have been created — check via subprocess calls
    create_calls = [c for c in mocker.calls if len(c) > 1 and c[1] == "run"]
    assert len(create_calls) >= 1


@pytest.mark.asyncio
async def test_process_github_delivers_error_on_bad_message(settings):
    """deliver_error called when message doesn't contain Repository: line."""
    mocker = SubprocessMocker()
    _setup_default_subprocess(mocker, None)
    runner, sandbox = _make_runner(settings, mocker)
    channel = StubChannel()

    with patch("asyncio.create_subprocess_exec", mocker):
        await _run_pipeline(runner, "gh-18", "no repo info here", channel)

    assert len(channel.errors) == 1
    assert "repository" in channel.errors[0][1].lower() or "parse" in channel.errors[0][1].lower()


@pytest.mark.asyncio
async def test_process_github_ci_fix_uses_fix_ci_prompt(settings):
    """CI_FIX: prefix in message triggers /fix-ci prompt."""
    mocker = SubprocessMocker()
    _setup_default_subprocess(mocker, None)
    runner, sandbox = _make_runner(settings, mocker)
    channel = StubChannel()

    ipc_dir = os.path.join(settings.ipc_base_dir, "sandbox-gh-19")
    os.makedirs(ipc_dir, exist_ok=True)
    with open(os.path.join(ipc_dir, "pr-url.txt"), "w") as f:
        f.write("https://github.com/owner/repo/pull/1")
    with open(os.path.join(ipc_dir, "acceptance-criteria.md"), "w") as f:
        f.write("- Done")
    with open(os.path.join(ipc_dir, "changed-files.txt"), "w") as f:
        f.write("fix.py\n")

    call_prompts = []

    async def tracking_code_stream(chat_id, prompt, on_chunk, **kwargs):
        call_prompts.append(prompt)
        return (0, "output", "")

    sandbox.code_stream = tracking_code_stream

    async def mock_exec(chat_id, cmd):
        if "git diff --name-only" in cmd:
            return (0, "fix.py\n", "")
        return (0, "", "")

    sandbox.exec = mock_exec

    ci_message = f"CI_FIX: Tests failed\n\n{GITHUB_MESSAGE}"

    with patch("asyncio.create_subprocess_exec", mocker):
        await _run_pipeline(runner, "gh-19", ci_message, channel)

    assert len(call_prompts) >= 1
    assert "/fix-ci" in call_prompts[0]


@pytest.mark.asyncio
async def test_process_github_clarification_stops_retries(settings):
    """When clarification.txt exists, post it as a comment and don't retry."""
    mocker = SubprocessMocker()
    _setup_default_subprocess(mocker, None)
    runner, sandbox = _make_runner(settings, mocker)
    channel = StubChannel()

    ipc_dir = os.path.join(settings.ipc_base_dir, "sandbox-gh-20")
    os.makedirs(ipc_dir, exist_ok=True)
    # Write clarification.txt
    with open(os.path.join(ipc_dir, "clarification.txt"), "w") as f:
        f.write("What Python version should this target?")

    gemini_call_count = 0

    async def counting_code_stream(chat_id, prompt, on_chunk, **kwargs):
        nonlocal gemini_call_count
        gemini_call_count += 1
        return (0, "output", "")

    sandbox.code_stream = counting_code_stream

    with patch("asyncio.create_subprocess_exec", mocker):
        await _run_pipeline(runner, "gh-20", GITHUB_MESSAGE, channel)

    # Should NOT retry — only 1 Gemini session call
    assert gemini_call_count == 1
    # Should deliver as max_turns
    assert len(channel.results) == 1
    assert "clarification" in channel.results[0][1].lower()
    assert "Python version" in channel.results[0][1]
    assert len(channel.errors) == 0


@pytest.mark.asyncio
async def test_process_github_scope_creep_detection(settings):
    """git diff returning forbidden files → validation fails with revert guidance."""
    mocker = SubprocessMocker()
    _setup_default_subprocess(mocker, None)
    runner, sandbox = _make_runner(settings, mocker)
    channel = StubChannel()

    ipc_dir = os.path.join(settings.ipc_base_dir, "sandbox-gh-21")
    os.makedirs(ipc_dir, exist_ok=True)
    with open(os.path.join(ipc_dir, "pr-url.txt"), "w") as f:
        f.write("https://github.com/owner/repo/pull/1")
    with open(os.path.join(ipc_dir, "acceptance-criteria.md"), "w") as f:
        f.write("- Done")
    with open(os.path.join(ipc_dir, "changed-files.txt"), "w") as f:
        f.write("fix.py\npyproject.toml\n")

    call_prompts = []

    async def tracking_code_stream(chat_id, prompt, on_chunk, **kwargs):
        call_prompts.append(prompt)
        return (0, "output", "")

    sandbox.code_stream = tracking_code_stream

    call_count = [0]

    async def mock_exec(chat_id, cmd):
        if "git diff --name-only" in cmd:
            call_count[0] += 1
            if call_count[0] <= 1:
                # First validation: forbidden files
                return (0, "fix.py\npyproject.toml\n", "")
            else:
                # Subsequent: clean
                return (0, "fix.py\n", "")
        return (0, "", "")

    sandbox.exec = mock_exec

    with patch("asyncio.create_subprocess_exec", mocker):
        await _run_pipeline(runner, "gh-21", GITHUB_MESSAGE, channel)

    # Retry prompt should mention scope creep and revert
    assert len(call_prompts) >= 2
    assert "pyproject.toml" in call_prompts[1]
    assert "Revert" in call_prompts[1] or "revert" in call_prompts[1].lower()


@pytest.mark.asyncio
async def test_process_github_clone_failure(settings):
    """Clone failure (subprocess returns rc=1) → error delivered."""
    mocker = SubprocessMocker()
    _setup_default_subprocess(mocker, None)
    runner, sandbox = _make_runner(settings, mocker)
    channel = StubChannel()

    async def failing_exec(chat_id, cmd):
        if "git clone" in cmd:
            return (1, "", "fatal: repository not found")
        return (0, "", "")

    sandbox.exec = failing_exec

    with patch("asyncio.create_subprocess_exec", mocker):
        await _run_pipeline(runner, "gh-22", GITHUB_MESSAGE, channel)

    assert len(channel.errors) == 1
    assert "clone" in channel.errors[0][1].lower() or "Clone" in channel.errors[0][1]


# ------------------------------------------------------------------ #
# _host_push tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_host_push_strips_forbidden_files(settings):
    """_host_push strips forbidden files from commit before pushing."""
    mocker = SubprocessMocker()
    _setup_default_subprocess(mocker, None)
    runner, sandbox = _make_runner(settings, mocker)

    ipc_dir = os.path.join(settings.ipc_base_dir, "sandbox-gh-30")
    os.makedirs(ipc_dir, exist_ok=True)

    exec_cmds = []

    async def mock_exec(chat_id, cmd):
        exec_cmds.append(cmd)
        if "git rev-parse --abbrev-ref HEAD" in cmd:
            return (0, "agent/fix-30\n", "")
        if "git diff --name-only" in cmd:
            return (0, "fix.py\npyproject.toml\nuv.lock\n", "")
        if "git push" in cmd:
            return (0, "", "")
        if "gh pr create" in cmd or "gh pr view" in cmd:
            return (0, "https://github.com/owner/repo/pull/30\n", "")
        return (0, "", "")

    sandbox.exec = mock_exec
    sandbox._containers = {"gh-30": "sandbox-gh-30"}

    with patch("asyncio.create_subprocess_exec", mocker):
        pr_url = await runner._host_push("gh-30", "/workspace/repo", "owner/repo", False)

    assert pr_url == "https://github.com/owner/repo/pull/30"
    # Should have called git checkout to strip forbidden files
    strip_cmds = [c for c in exec_cmds if "git checkout" in c and "pyproject.toml" in c]
    assert len(strip_cmds) >= 1


@pytest.mark.asyncio
async def test_host_push_ci_fix_uses_force_push(settings):
    """_host_push with is_ci_fix=True uses --force."""
    mocker = SubprocessMocker()
    _setup_default_subprocess(mocker, None)
    runner, sandbox = _make_runner(settings, mocker)

    ipc_dir = os.path.join(settings.ipc_base_dir, "sandbox-gh-31")
    os.makedirs(ipc_dir, exist_ok=True)

    exec_cmds = []

    async def mock_exec(chat_id, cmd):
        exec_cmds.append(cmd)
        if "git rev-parse --abbrev-ref HEAD" in cmd:
            return (0, "agent/fix-31\n", "")
        if "git diff --name-only" in cmd:
            return (0, "fix.py\n", "")
        if "git push" in cmd:
            return (0, "", "")
        if "gh pr view" in cmd:
            return (0, "https://github.com/owner/repo/pull/31\n", "")
        return (0, "", "")

    sandbox.exec = mock_exec
    sandbox._containers = {"gh-31": "sandbox-gh-31"}

    with patch("asyncio.create_subprocess_exec", mocker):
        pr_url = await runner._host_push("gh-31", "/workspace/repo", "owner/repo", True)

    assert pr_url == "https://github.com/owner/repo/pull/31"
    push_cmds = [c for c in exec_cmds if "git push" in c]
    assert len(push_cmds) == 1
    assert "--force" in push_cmds[0]


@pytest.mark.asyncio
async def test_host_push_no_branch_returns_none(settings):
    """_host_push returns None when on main branch."""
    mocker = SubprocessMocker()
    _setup_default_subprocess(mocker, None)
    runner, sandbox = _make_runner(settings, mocker)

    async def mock_exec(chat_id, cmd):
        if "git rev-parse --abbrev-ref HEAD" in cmd:
            return (0, "main\n", "")
        return (0, "", "")

    sandbox.exec = mock_exec
    sandbox._containers = {"gh-32": "sandbox-gh-32"}

    with patch("asyncio.create_subprocess_exec", mocker):
        pr_url = await runner._host_push("gh-32", "/workspace/repo", "owner/repo", False)

    assert pr_url is None


# ------------------------------------------------------------------ #
# check_forbidden tests
# ------------------------------------------------------------------ #


def test_check_forbidden_names():
    """check_forbidden catches forbidden file names."""
    from matrix_agent.sandbox import check_forbidden
    result = check_forbidden(["fix.py", "pyproject.toml", "uv.lock"])
    assert "pyproject.toml" in result
    assert "uv.lock" in result
    assert "fix.py" not in result


def test_check_forbidden_prefixes():
    """check_forbidden catches forbidden directory prefixes."""
    from matrix_agent.sandbox import check_forbidden
    result = check_forbidden(["src/main.py", ".github/workflows/ci.yml", ".gemini/settings.json"])
    assert ".github/workflows/ci.yml" in result
    assert ".gemini/settings.json" in result
    assert "src/main.py" not in result


def test_check_forbidden_clean():
    """check_forbidden returns empty list for clean files."""
    from matrix_agent.sandbox import check_forbidden
    result = check_forbidden(["src/main.py", "tests/test_main.py"])
    assert result == []
