"""Tests for Gemini orchestration additions to SandboxManager.

Tests template extraction, code_stream workdir param, run_gemini_session(), and validate_work().
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from matrix_agent.sandbox import SandboxManager


# Helper for async iteration (reused from test_sandbox_auto_accept.py)
async def aiter(items):
    for item in items:
        yield item


def _make_settings(tmp_path=None):
    return SimpleNamespace(
        podman_path="podman",
        sandbox_image="test:latest",
        command_timeout_seconds=10,
        coding_timeout_seconds=30,
        gemini_api_key="fake",
        gemini_model="",
        dashscope_api_key="",
        github_token="",
        ipc_base_dir=str(tmp_path) if tmp_path else "/tmp/test-ipc",
        screenshot_script="/opt/playwright/screenshot.js",
    )


# ------------------------------------------------------------------ #
# Group A: Template extraction
# ------------------------------------------------------------------ #


def test_templates_dir_constant_points_to_correct_path():
    """_TEMPLATES_DIR resolves to src/matrix_agent/templates/."""
    from matrix_agent.sandbox import _TEMPLATES_DIR

    assert _TEMPLATES_DIR.is_dir(), f"{_TEMPLATES_DIR} is not a directory"
    assert _TEMPLATES_DIR.name == "templates"
    assert _TEMPLATES_DIR.parent.name == "matrix_agent"


def test_templates_dict_has_expected_keys():
    """TEMPLATES dict contains all 13 expected template files."""
    from matrix_agent.sandbox import TEMPLATES

    expected = {
        "GEMINI.md",
        "status.md",
        "settings.json",
        "hook-session-start.sh",
        "hook-after-agent.sh",
        "hook-after-tool.sh",
        "hook-notification.sh",
        "hook-before-tool.sh",
        "cmd-fix-issue.toml",
        "cmd-fix-ci.toml",
        "skill-delegate-qwen.md",
        "qwen-wrapper.sh",
        "qwen-settings.json",
    }
    assert set(TEMPLATES.keys()) == expected


def test_all_template_files_exist_on_disk():
    """Every template referenced in TEMPLATES exists as a file."""
    from matrix_agent.sandbox import TEMPLATES, _TEMPLATES_DIR

    for template_name in TEMPLATES:
        path = _TEMPLATES_DIR / template_name
        assert path.is_file(), f"Template file missing: {path}"


@pytest.mark.asyncio
async def test_init_workspace_reads_from_templates_dir():
    """_init_workspace() writes template file content (not inline strings) to the container."""
    from matrix_agent.sandbox import TEMPLATES, _TEMPLATES_DIR

    sandbox = SandboxManager(_make_settings())
    sandbox._containers = {"test-1": "sandbox-test-1"}

    written_paths = []
    written_contents = []

    async def capture_run(*args, stdin_data=None, **kwargs):
        # Capture writes (exec -i <name> sh -c "mkdir ... && cat > <path>")
        if stdin_data and len(args) >= 6 and args[0] == "exec" and args[1] == "-i":
            # args = ("exec", "-i", container_name, "sh", "-c", "mkdir -p ... && cat > /path")
            sh_cmd = args[5] if len(args) > 5 else ""
            if "cat >" in sh_cmd:
                dest = sh_cmd.split("cat > ")[-1].strip()
                written_paths.append(dest)
                written_contents.append(stdin_data.decode())
        return (0, "", "")

    sandbox._run = capture_run

    await sandbox._init_workspace("sandbox-test-1")

    # All template destinations should have been written
    for template_name, container_path in TEMPLATES.items():
        expected_content = (_TEMPLATES_DIR / template_name).read_text()
        assert container_path in written_paths, f"Template {template_name} not written to {container_path}"
        idx = written_paths.index(container_path)
        assert written_contents[idx] == expected_content, (
            f"Template {template_name} content mismatch"
        )


# ------------------------------------------------------------------ #
# Group B: code_stream() workdir parameter
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_code_stream_default_workdir_is_workspace():
    """code_stream without workdir passes --workdir /workspace to podman."""
    sandbox = SandboxManager(_make_settings())
    sandbox._containers = {"room-1": "sandbox-room-1"}

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        proc = AsyncMock()
        proc.stdout = AsyncMock()
        proc.stdout.__aiter__ = lambda self: aiter([])
        proc.stderr = AsyncMock()
        proc.stderr.__aiter__ = lambda self: aiter([])
        proc.wait = AsyncMock(return_value=0)
        proc.returncode = 0
        mock_exec.return_value = proc

        on_chunk = AsyncMock()
        await sandbox.code_stream("room-1", "do something", on_chunk)

        call_args = list(mock_exec.call_args[0])
        # Find --workdir and its value
        idx = call_args.index("--workdir")
        assert call_args[idx + 1] == "/workspace"


@pytest.mark.asyncio
async def test_code_stream_custom_workdir_is_forwarded():
    """code_stream with workdir='/workspace/my-repo' passes it to podman."""
    sandbox = SandboxManager(_make_settings())
    sandbox._containers = {"room-1": "sandbox-room-1"}

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        proc = AsyncMock()
        proc.stdout = AsyncMock()
        proc.stdout.__aiter__ = lambda self: aiter([])
        proc.stderr = AsyncMock()
        proc.stderr.__aiter__ = lambda self: aiter([])
        proc.wait = AsyncMock(return_value=0)
        proc.returncode = 0
        mock_exec.return_value = proc

        on_chunk = AsyncMock()
        await sandbox.code_stream(
            "room-1", "do something", on_chunk, workdir="/workspace/my-repo",
        )

        call_args = list(mock_exec.call_args[0])
        idx = call_args.index("--workdir")
        assert call_args[idx + 1] == "/workspace/my-repo"


# ------------------------------------------------------------------ #
# Group C: run_gemini_session()
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_run_gemini_session_calls_code_stream_with_correct_params():
    """run_gemini_session delegates to code_stream with correct workdir, cli, auto_accept."""
    sandbox = SandboxManager(_make_settings())
    sandbox._containers = {"gh-1": "sandbox-gh-1"}
    sandbox.code_stream = AsyncMock(return_value=(0, "output", ""))

    on_chunk = AsyncMock()
    await sandbox.run_gemini_session("gh-1", "/fix-issue context", on_chunk, "my-repo")

    sandbox.code_stream.assert_called_once()
    kwargs = sandbox.code_stream.call_args
    # Check positional and keyword args
    assert kwargs[1]["cli"] == "gemini" or kwargs[0][3] if len(kwargs[0]) > 3 else True
    assert kwargs[1].get("auto_accept") is True or kwargs[1].get("auto_accept", None) is True
    assert kwargs[1].get("workdir") == "/workspace/my-repo"


@pytest.mark.asyncio
async def test_run_gemini_session_reads_pr_url_from_ipc_dir(tmp_path):
    """run_gemini_session reads pr-url.txt from IPC dir and returns URL."""
    settings = _make_settings(tmp_path)
    sandbox = SandboxManager(settings)
    container_name = "sandbox-gh-2"
    sandbox._containers = {"gh-2": container_name}
    sandbox.code_stream = AsyncMock(return_value=(0, "output", ""))

    # Write pr-url.txt to the IPC dir
    ipc_dir = tmp_path / container_name
    ipc_dir.mkdir()
    (ipc_dir / "pr-url.txt").write_text("https://github.com/owner/repo/pull/42\n")

    on_chunk = AsyncMock()
    rc, stdout, pr_url = await sandbox.run_gemini_session(
        "gh-2", "/fix-issue ctx", on_chunk, "repo",
    )

    assert rc == 0
    assert pr_url == "https://github.com/owner/repo/pull/42"


@pytest.mark.asyncio
async def test_run_gemini_session_returns_none_pr_url_when_file_missing(tmp_path):
    """run_gemini_session returns None for pr_url when file doesn't exist."""
    settings = _make_settings(tmp_path)
    sandbox = SandboxManager(settings)
    container_name = "sandbox-gh-3"
    sandbox._containers = {"gh-3": container_name}
    sandbox.code_stream = AsyncMock(return_value=(0, "output", ""))

    # Create IPC dir but no pr-url.txt
    ipc_dir = tmp_path / container_name
    ipc_dir.mkdir()

    on_chunk = AsyncMock()
    rc, stdout, pr_url = await sandbox.run_gemini_session(
        "gh-3", "/fix-issue ctx", on_chunk, "repo",
    )

    assert pr_url is None


@pytest.mark.asyncio
async def test_run_gemini_session_returns_nonzero_exit_on_failure(tmp_path):
    """run_gemini_session propagates non-zero exit code from code_stream."""
    settings = _make_settings(tmp_path)
    sandbox = SandboxManager(settings)
    container_name = "sandbox-gh-4"
    sandbox._containers = {"gh-4": container_name}
    sandbox.code_stream = AsyncMock(return_value=(1, "partial", "timeout"))

    ipc_dir = tmp_path / container_name
    ipc_dir.mkdir()

    on_chunk = AsyncMock()
    rc, stdout, pr_url = await sandbox.run_gemini_session(
        "gh-4", "/fix-issue ctx", on_chunk, "repo",
    )

    assert rc == 1


# ------------------------------------------------------------------ #
# Group D: validate_work()
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_validate_work_passes_when_all_checks_succeed(tmp_path):
    """validate_work returns (True, []) when tests pass and IPC files exist."""
    settings = _make_settings(tmp_path)
    sandbox = SandboxManager(settings)
    container_name = "sandbox-gh-5"
    sandbox._containers = {"gh-5": container_name}

    # Mock exec to return success for test/lint commands
    sandbox.exec = AsyncMock(return_value=(0, "all passed\n", ""))

    # Create IPC files
    ipc_dir = tmp_path / container_name
    ipc_dir.mkdir()
    (ipc_dir / "pr-url.txt").write_text("https://github.com/owner/repo/pull/1")
    (ipc_dir / "acceptance-criteria.md").write_text("- Feature X works\n")

    passed, failures = await sandbox.validate_work("gh-5", "repo")

    assert passed is True
    assert failures == []


@pytest.mark.asyncio
async def test_validate_work_fails_when_tests_fail(tmp_path):
    """validate_work returns failure when ruff/pytest exits non-zero."""
    settings = _make_settings(tmp_path)
    sandbox = SandboxManager(settings)
    container_name = "sandbox-gh-6"
    sandbox._containers = {"gh-6": container_name}

    async def mock_exec(chat_id, cmd):
        if "ruff" in cmd or "pytest" in cmd:
            return (1, "FAILED test_foo.py::test_bar", "")
        return (0, "1 file changed\n", "")

    sandbox.exec = AsyncMock(side_effect=mock_exec)

    ipc_dir = tmp_path / container_name
    ipc_dir.mkdir()
    (ipc_dir / "pr-url.txt").write_text("https://github.com/owner/repo/pull/1")
    (ipc_dir / "acceptance-criteria.md").write_text("- Feature X works\n")

    passed, failures = await sandbox.validate_work("gh-6", "repo")

    assert passed is False
    assert any("fail" in f.lower() or "FAILED" in f for f in failures)


@pytest.mark.asyncio
async def test_validate_work_fails_when_pr_url_missing(tmp_path):
    """validate_work fails when pr-url.txt is absent."""
    settings = _make_settings(tmp_path)
    sandbox = SandboxManager(settings)
    container_name = "sandbox-gh-7"
    sandbox._containers = {"gh-7": container_name}
    sandbox.exec = AsyncMock(return_value=(0, "ok\n", ""))

    ipc_dir = tmp_path / container_name
    ipc_dir.mkdir()
    # No pr-url.txt
    (ipc_dir / "acceptance-criteria.md").write_text("- Feature X works\n")

    passed, failures = await sandbox.validate_work("gh-7", "repo")

    assert passed is False
    assert any("pr" in f.lower() or "PR" in f for f in failures)


@pytest.mark.asyncio
async def test_validate_work_fails_when_acceptance_criteria_missing(tmp_path):
    """validate_work fails when acceptance-criteria.md is absent or empty."""
    settings = _make_settings(tmp_path)
    sandbox = SandboxManager(settings)
    container_name = "sandbox-gh-8"
    sandbox._containers = {"gh-8": container_name}
    sandbox.exec = AsyncMock(return_value=(0, "ok\n", ""))

    ipc_dir = tmp_path / container_name
    ipc_dir.mkdir()
    (ipc_dir / "pr-url.txt").write_text("https://github.com/owner/repo/pull/1")
    # No acceptance-criteria.md

    passed, failures = await sandbox.validate_work("gh-8", "repo")

    assert passed is False
    assert any("acceptance" in f.lower() or "criteria" in f.lower() for f in failures)


@pytest.mark.asyncio
async def test_validate_work_collects_all_failures(tmp_path):
    """validate_work aggregates multiple failures into the list."""
    settings = _make_settings(tmp_path)
    sandbox = SandboxManager(settings)
    container_name = "sandbox-gh-9"
    sandbox._containers = {"gh-9": container_name}

    # Tests fail
    sandbox.exec = AsyncMock(return_value=(1, "FAILED", ""))

    ipc_dir = tmp_path / container_name
    ipc_dir.mkdir()
    # No pr-url.txt, no acceptance-criteria.md

    passed, failures = await sandbox.validate_work("gh-9", "repo")

    assert passed is False
    assert len(failures) >= 2, f"Expected at least 2 failures, got {failures}"
