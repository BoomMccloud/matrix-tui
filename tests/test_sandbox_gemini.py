"""Tests for Gemini orchestration in SandboxManager.

Real SandboxManager, mock only asyncio.create_subprocess_exec.
Real tmp_path for IPC and state files.
"""

import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from matrix_agent.sandbox import SandboxManager, TEMPLATES, _TEMPLATES_DIR
from conftest import SubprocessMocker


def _make_sandbox(settings):
    return SandboxManager(settings)


# ------------------------------------------------------------------ #
# Group A: Template extraction
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_init_workspace_writes_all_templates(settings):
    """_init_workspace() sends a single script containing all templates via stdin."""
    sandbox = _make_sandbox(settings)
    sandbox._containers = {"test-1": "sandbox-test-1"}

    captured_stdin = []

    async def capture_run(*args, stdin_data=None, **kwargs):
        if stdin_data:
            captured_stdin.append(stdin_data.decode())
        return (0, "", "")

    sandbox._run = capture_run
    await sandbox._init_workspace("sandbox-test-1")

    # Should be a single _run call with all templates in the stdin script
    assert len(captured_stdin) == 1
    script = captured_stdin[0]

    # Verify every template's content is in the script
    for template_name, container_path in TEMPLATES.items():
        expected_content = (_TEMPLATES_DIR / template_name).read_text()
        assert container_path in script, f"Template {template_name} path not in script"
        assert expected_content in script, f"Template {template_name} content not in script"

    # Verify chmod, git config, and gh auth are in the script
    assert "chmod +x" in script
    assert "git config --global" in script


# ------------------------------------------------------------------ #
# Group B: code_stream() workdir parameter
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_code_stream_default_workdir_is_workspace(settings):
    """code_stream without workdir passes --workdir /workspace."""
    mocker = SubprocessMocker()
    mocker.on("podman", "exec")

    sandbox = _make_sandbox(settings)
    sandbox._containers = {"room-1": "sandbox-room-1"}

    with patch("asyncio.create_subprocess_exec", mocker):
        on_chunk = AsyncMock()
        await sandbox.code_stream("room-1", "do something", on_chunk)

    # Find the exec call with --workdir
    exec_calls = [c for c in mocker.calls if len(c) > 2 and c[2] == "--workdir"]
    assert len(exec_calls) >= 1
    call_args = list(exec_calls[0])
    idx = call_args.index("--workdir")
    assert call_args[idx + 1] == "/workspace"


@pytest.mark.asyncio
async def test_code_stream_custom_workdir_is_forwarded(settings):
    """code_stream with workdir='/workspace/my-repo' passes it to podman."""
    mocker = SubprocessMocker()
    mocker.on("podman", "exec")

    sandbox = _make_sandbox(settings)
    sandbox._containers = {"room-1": "sandbox-room-1"}

    with patch("asyncio.create_subprocess_exec", mocker):
        on_chunk = AsyncMock()
        await sandbox.code_stream("room-1", "do something", on_chunk, workdir="/workspace/my-repo")

    exec_calls = [c for c in mocker.calls if len(c) > 2 and c[2] == "--workdir"]
    assert len(exec_calls) >= 1
    call_args = list(exec_calls[0])
    idx = call_args.index("--workdir")
    assert call_args[idx + 1] == "/workspace/my-repo"


# ------------------------------------------------------------------ #
# Group C: run_gemini_session()
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_run_gemini_session_reads_pr_url_from_ipc(settings):
    """run_gemini_session reads pr-url.txt from IPC dir and returns URL."""
    mocker = SubprocessMocker()
    mocker.on("podman", "exec")

    sandbox = _make_sandbox(settings)
    container_name = "sandbox-gh-2"
    sandbox._containers = {"gh-2": container_name}

    # Write pr-url.txt to the IPC dir
    ipc_dir = os.path.join(settings.ipc_base_dir, container_name)
    os.makedirs(ipc_dir, exist_ok=True)
    with open(os.path.join(ipc_dir, "pr-url.txt"), "w") as f:
        f.write("https://github.com/owner/repo/pull/42\n")

    with patch("asyncio.create_subprocess_exec", mocker):
        on_chunk = AsyncMock()
        rc, stdout, pr_url = await sandbox.run_gemini_session("gh-2", "/fix-issue ctx", on_chunk, "repo")

    assert rc == 0
    assert pr_url == "https://github.com/owner/repo/pull/42"


@pytest.mark.asyncio
async def test_run_gemini_session_returns_none_when_no_pr_url(settings):
    """run_gemini_session returns None for pr_url when file doesn't exist."""
    mocker = SubprocessMocker()
    mocker.on("podman", "exec")

    sandbox = _make_sandbox(settings)
    container_name = "sandbox-gh-3"
    sandbox._containers = {"gh-3": container_name}

    ipc_dir = os.path.join(settings.ipc_base_dir, container_name)
    os.makedirs(ipc_dir, exist_ok=True)

    with patch("asyncio.create_subprocess_exec", mocker):
        on_chunk = AsyncMock()
        rc, stdout, pr_url = await sandbox.run_gemini_session("gh-3", "/fix-issue ctx", on_chunk, "repo")

    assert pr_url is None


# ------------------------------------------------------------------ #
# Group D: validate_work()
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_validate_work_passes_when_all_checks_succeed(settings):
    """validate_work returns (True, []) when tests pass and IPC files exist."""
    mocker = SubprocessMocker()
    mocker.on("podman", "exec")

    sandbox = _make_sandbox(settings)
    container_name = "sandbox-gh-5"
    sandbox._containers = {"gh-5": container_name}

    # Create IPC files
    ipc_dir = os.path.join(settings.ipc_base_dir, container_name)
    os.makedirs(ipc_dir, exist_ok=True)
    with open(os.path.join(ipc_dir, "pr-url.txt"), "w") as f:
        f.write("https://github.com/owner/repo/pull/1")
    with open(os.path.join(ipc_dir, "acceptance-criteria.md"), "w") as f:
        f.write("- Feature X works\n")
    with open(os.path.join(ipc_dir, "changed-files.txt"), "w") as f:
        f.write("fix.py\n")

    async def mock_exec(chat_id, cmd):
        if "git diff --name-only" in cmd:
            return (0, "fix.py\n", "")
        return (0, "all passed\n", "")

    sandbox.exec = mock_exec
    passed, failures = await sandbox.validate_work("gh-5", "repo")

    assert passed is True
    assert failures == []


@pytest.mark.asyncio
async def test_validate_work_fails_when_tests_fail(settings):
    """validate_work returns failure when tests exit non-zero."""
    sandbox = _make_sandbox(settings)
    container_name = "sandbox-gh-6"
    sandbox._containers = {"gh-6": container_name}

    ipc_dir = os.path.join(settings.ipc_base_dir, container_name)
    os.makedirs(ipc_dir, exist_ok=True)
    with open(os.path.join(ipc_dir, "pr-url.txt"), "w") as f:
        f.write("https://github.com/owner/repo/pull/1")
    with open(os.path.join(ipc_dir, "acceptance-criteria.md"), "w") as f:
        f.write("- Feature X works\n")
    with open(os.path.join(ipc_dir, "changed-files.txt"), "w") as f:
        f.write("fix.py\n")

    async def mock_exec(chat_id, cmd):
        if "git diff --name-only" in cmd:
            return (0, "fix.py\n", "")
        return (1, "FAILED test_foo.py::test_bar", "")

    sandbox.exec = mock_exec
    passed, failures = await sandbox.validate_work("gh-6", "repo")

    assert passed is False
    assert any("fail" in f.lower() or "FAILED" in f for f in failures)


@pytest.mark.asyncio
async def test_validate_work_fails_when_pr_url_missing(settings):
    """validate_work fails when pr-url.txt is absent."""
    sandbox = _make_sandbox(settings)
    container_name = "sandbox-gh-7"
    sandbox._containers = {"gh-7": container_name}

    ipc_dir = os.path.join(settings.ipc_base_dir, container_name)
    os.makedirs(ipc_dir, exist_ok=True)
    with open(os.path.join(ipc_dir, "acceptance-criteria.md"), "w") as f:
        f.write("- Feature X works\n")
    with open(os.path.join(ipc_dir, "changed-files.txt"), "w") as f:
        f.write("fix.py\n")

    async def mock_exec(chat_id, cmd):
        if "git diff --name-only" in cmd:
            return (0, "fix.py\n", "")
        return (0, "ok\n", "")

    sandbox.exec = mock_exec
    passed, failures = await sandbox.validate_work("gh-7", "repo")

    assert passed is False
    assert any("pr" in f.lower() or "PR" in f for f in failures)


@pytest.mark.asyncio
async def test_validate_work_fails_when_acceptance_criteria_missing(settings):
    """validate_work fails when acceptance-criteria.md is absent."""
    sandbox = _make_sandbox(settings)
    container_name = "sandbox-gh-8"
    sandbox._containers = {"gh-8": container_name}

    ipc_dir = os.path.join(settings.ipc_base_dir, container_name)
    os.makedirs(ipc_dir, exist_ok=True)
    with open(os.path.join(ipc_dir, "pr-url.txt"), "w") as f:
        f.write("https://github.com/owner/repo/pull/1")
    with open(os.path.join(ipc_dir, "changed-files.txt"), "w") as f:
        f.write("fix.py\n")

    async def mock_exec(chat_id, cmd):
        if "git diff --name-only" in cmd:
            return (0, "fix.py\n", "")
        return (0, "ok\n", "")

    sandbox.exec = mock_exec
    passed, failures = await sandbox.validate_work("gh-8", "repo")

    assert passed is False
    assert any("acceptance" in f.lower() or "criteria" in f.lower() for f in failures)


@pytest.mark.asyncio
async def test_validate_work_scope_creep_detection(settings):
    """Forbidden files in git diff output → scope creep failure."""
    sandbox = _make_sandbox(settings)
    container_name = "sandbox-gh-scope"
    sandbox._containers = {"gh-scope": container_name}

    ipc_dir = os.path.join(settings.ipc_base_dir, container_name)
    os.makedirs(ipc_dir, exist_ok=True)
    with open(os.path.join(ipc_dir, "pr-url.txt"), "w") as f:
        f.write("https://github.com/owner/repo/pull/1")
    with open(os.path.join(ipc_dir, "acceptance-criteria.md"), "w") as f:
        f.write("- Done")
    with open(os.path.join(ipc_dir, "changed-files.txt"), "w") as f:
        f.write("fix.py\npyproject.toml\nuv.lock\n")

    async def mock_exec(chat_id, cmd):
        if "git diff --name-only" in cmd:
            return (0, "fix.py\npyproject.toml\nuv.lock\n", "")
        return (0, "ok\n", "")

    sandbox.exec = mock_exec
    passed, failures = await sandbox.validate_work("gh-scope", "repo")

    assert passed is False
    assert any("pyproject.toml" in f for f in failures)
    assert any("Revert" in f or "revert" in f.lower() for f in failures)


@pytest.mark.asyncio
async def test_validate_work_manifest_undeclared_file(settings):
    """Manifest exists but changed file not declared → failure."""
    sandbox = _make_sandbox(settings)
    container_name = "sandbox-gh-manifest-undecl"
    sandbox._containers = {"gh-mu": container_name}

    ipc_dir = os.path.join(settings.ipc_base_dir, container_name)
    os.makedirs(ipc_dir, exist_ok=True)
    with open(os.path.join(ipc_dir, "pr-url.txt"), "w") as f:
        f.write("https://github.com/owner/repo/pull/1")
    with open(os.path.join(ipc_dir, "acceptance-criteria.md"), "w") as f:
        f.write("- Done\n")
    with open(os.path.join(ipc_dir, "changed-files.txt"), "w") as f:
        f.write("fix.py\n")

    async def mock_exec(chat_id, cmd):
        if "git diff --name-only" in cmd:
            return (0, "fix.py\nextra.py\n", "")
        return (0, "ok\n", "")

    sandbox.exec = mock_exec
    passed, failures = await sandbox.validate_work("gh-mu", "repo")

    assert passed is False
    assert any("extra.py" in f and "outside declared scope" in f for f in failures)


@pytest.mark.asyncio
async def test_validate_work_manifest_missing(settings):
    """No manifest file → failure."""
    sandbox = _make_sandbox(settings)
    container_name = "sandbox-gh-manifest-miss"
    sandbox._containers = {"gh-mm": container_name}

    ipc_dir = os.path.join(settings.ipc_base_dir, container_name)
    os.makedirs(ipc_dir, exist_ok=True)
    with open(os.path.join(ipc_dir, "pr-url.txt"), "w") as f:
        f.write("https://github.com/owner/repo/pull/1")
    with open(os.path.join(ipc_dir, "acceptance-criteria.md"), "w") as f:
        f.write("- Done\n")
    # No changed-files.txt

    async def mock_exec(chat_id, cmd):
        if "git diff --name-only" in cmd:
            return (0, "fix.py\n", "")
        return (0, "ok\n", "")

    sandbox.exec = mock_exec
    passed, failures = await sandbox.validate_work("gh-mm", "repo")

    assert passed is False
    assert any("manifest" in f.lower() or "changed-files.txt" in f for f in failures)


@pytest.mark.asyncio
async def test_validate_work_manifest_declares_forbidden_file(settings):
    """Manifest declares a forbidden file → denylist still catches it."""
    sandbox = _make_sandbox(settings)
    container_name = "sandbox-gh-manifest-forb"
    sandbox._containers = {"gh-mf": container_name}

    ipc_dir = os.path.join(settings.ipc_base_dir, container_name)
    os.makedirs(ipc_dir, exist_ok=True)
    with open(os.path.join(ipc_dir, "pr-url.txt"), "w") as f:
        f.write("https://github.com/owner/repo/pull/1")
    with open(os.path.join(ipc_dir, "acceptance-criteria.md"), "w") as f:
        f.write("- Done\n")
    with open(os.path.join(ipc_dir, "changed-files.txt"), "w") as f:
        f.write("fix.py\n.gitignore\n")

    async def mock_exec(chat_id, cmd):
        if "git diff --name-only" in cmd:
            return (0, "fix.py\n.gitignore\n", "")
        return (0, "ok\n", "")

    sandbox.exec = mock_exec
    passed, failures = await sandbox.validate_work("gh-mf", "repo")

    assert passed is False
    assert any(".gitignore" in f for f in failures)


@pytest.mark.asyncio
async def test_validate_work_collects_all_failures(settings):
    """validate_work aggregates multiple failures."""
    sandbox = _make_sandbox(settings)
    container_name = "sandbox-gh-9"
    sandbox._containers = {"gh-9": container_name}

    ipc_dir = os.path.join(settings.ipc_base_dir, container_name)
    os.makedirs(ipc_dir, exist_ok=True)
    # No pr-url.txt, no acceptance-criteria.md

    async def mock_exec(chat_id, cmd):
        if "git diff --name-only" in cmd:
            return (0, "fix.py\n", "")
        return (1, "FAILED", "")

    sandbox.exec = mock_exec
    passed, failures = await sandbox.validate_work("gh-9", "repo")

    assert passed is False
    assert len(failures) >= 2


# ------------------------------------------------------------------ #
# Group E: read_ipc_file()
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_read_ipc_file_returns_content(settings):
    """read_ipc_file reads real file from tmp_path IPC dir."""
    sandbox = _make_sandbox(settings)
    container_name = "sandbox-gh-ipc"
    sandbox._containers = {"gh-ipc": container_name}

    ipc_dir = os.path.join(settings.ipc_base_dir, container_name)
    os.makedirs(ipc_dir, exist_ok=True)
    with open(os.path.join(ipc_dir, "clarification.txt"), "w") as f:
        f.write("What Python version?")

    result = await sandbox.read_ipc_file("gh-ipc", "clarification.txt")
    assert result == "What Python version?"


@pytest.mark.asyncio
async def test_read_ipc_file_returns_none_when_missing(settings):
    """read_ipc_file returns None when file doesn't exist."""
    sandbox = _make_sandbox(settings)
    container_name = "sandbox-gh-missing"
    sandbox._containers = {"gh-missing": container_name}

    ipc_dir = os.path.join(settings.ipc_base_dir, container_name)
    os.makedirs(ipc_dir, exist_ok=True)

    result = await sandbox.read_ipc_file("gh-missing", "nonexistent.txt")
    assert result is None


# ------------------------------------------------------------------ #
# Group F: State persistence
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_state_round_trip(settings, tmp_path):
    """save_state writes real file, load_state reads it back."""
    state_path = str(tmp_path / "state.json")

    sandbox = _make_sandbox(settings)
    sandbox._containers = {"room-1": "sandbox-room-1"}
    sandbox._histories = {"room-1": [{"role": "user", "content": "hello"}]}

    # Patch STATE_PATH
    with patch("matrix_agent.sandbox.STATE_PATH", state_path):
        sandbox.save_state()
        assert os.path.exists(state_path)

        with open(state_path) as f:
            data = json.load(f)
        assert data["containers"] == {"room-1": "sandbox-room-1"}
        assert data["history"]["room-1"][0]["content"] == "hello"


# ------------------------------------------------------------------ #
# Group G: Container lifecycle
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_load_state_prunes_stale_containers(settings, tmp_path):
    """load_state() removes containers that are not 'running'."""
    state_path = str(tmp_path / "state.json")

    state = {
        "containers": {
            "chat-live": "sandbox-live",
            "chat-stale": "sandbox-stale"
        },
        "history": {
            "chat-live": [{"role": "user", "content": "live"}],
            "chat-stale": [{"role": "user", "content": "stale"}]
        }
    }
    with open(state_path, "w") as f:
        json.dump(state, f)

    sandbox = _make_sandbox(settings)

    mocker = SubprocessMocker()
    # Mock inspect for live container
    mocker.on(
        "podman", "inspect", "--format", "{{.State.Status}}", "sandbox-live",
        stdout=b"running\n"
    )
    # Mock inspect for stale container
    mocker.on(
        "podman", "inspect", "--format", "{{.State.Status}}", "sandbox-stale",
        stdout=b"exited\n"
    )

    with patch("asyncio.create_subprocess_exec", mocker), \
         patch("matrix_agent.sandbox.STATE_PATH", state_path):
        histories = await sandbox.load_state()

    assert sandbox._containers == {"chat-live": "sandbox-live"}
    assert "chat-stale" not in sandbox._containers
    assert "chat-live" in histories
    assert "chat-stale" not in histories
    assert histories["chat-live"][0]["content"] == "live"


@pytest.mark.asyncio
async def test_container_create_and_destroy(settings):
    """Container create + destroy lifecycle via subprocess."""
    mocker = SubprocessMocker()
    mocker.on("podman", "run", stdout=b"container-id")
    mocker.on("podman", "exec")
    mocker.on("podman", "stop")
    mocker.on("podman", "rm")

    sandbox = _make_sandbox(settings)

    with patch("asyncio.create_subprocess_exec", mocker), \
         patch("matrix_agent.sandbox.STATE_PATH", "/dev/null"):
        name = await sandbox.create("test-chat")
        assert "test-chat" in sandbox._containers
        assert name == "sandbox-test-chat"

        await sandbox.destroy("test-chat")
        assert "test-chat" not in sandbox._containers

    # Verify podman run was called
    run_calls = [c for c in mocker.calls if len(c) > 1 and c[1] == "run"]
    assert len(run_calls) >= 1
    # Verify stop/rm were called
    stop_calls = [c for c in mocker.calls if len(c) > 1 and c[1] == "stop"]
    rm_calls = [c for c in mocker.calls if len(c) > 1 and c[1] == "rm"]
    assert len(stop_calls) >= 1
    assert len(rm_calls) >= 1


@pytest.mark.asyncio
async def test_create_retry_on_already_in_use(settings):
    """create() should rm and retry if podman run fails with 'already in use'."""
    mocker = SubprocessMocker()

    # Track calls to "run" to provide different responses
    run_calls = []

    async def stateful_mocker(*args, **kwargs):
        if args[:2] == ("podman", "run"):
            run_calls.append(args)
            if len(run_calls) == 1:
                # First run fails
                return await SubprocessMocker().on(
                    "podman", "run", returncode=125, stderr=b"already in use"
                )(*args, **kwargs)
            # Second run succeeds
            return await SubprocessMocker().on(
                "podman", "run", stdout=b"new-container-id"
            )(*args, **kwargs)

        # Other calls (rm, exec) use the main mocker
        return await mocker(*args, **kwargs)

    mocker.on("podman", "rm", "-f")
    mocker.on("podman", "exec")

    sandbox = _make_sandbox(settings)

    with patch("asyncio.create_subprocess_exec", side_effect=stateful_mocker), \
         patch("matrix_agent.sandbox.STATE_PATH", "/dev/null"):
        name = await sandbox.create("retry-chat")
        assert name == "sandbox-retry-chat"
        assert "retry-chat" in sandbox._containers

    # Verify call sequence
    assert len(run_calls) == 2
    # Verify podman rm -f was called between runs
    rm_calls = [c for c in mocker.calls if c[:3] == ("podman", "rm", "-f")]
    assert len(rm_calls) == 1
    assert rm_calls[0][3] == "sandbox-retry-chat"


# ------------------------------------------------------------------ #
# Group H: get_host_port()
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_get_host_port_success(settings):
    """get_host_port returns the host port from podman port output."""
    mocker = SubprocessMocker()
    mocker.on("podman", "port", "sandbox-room-1", "8080", stdout=b"0.0.0.0:12345\n")

    sandbox = _make_sandbox(settings)
    sandbox._containers = {"room-1": "sandbox-room-1"}

    with patch("asyncio.create_subprocess_exec", mocker):
        port = await sandbox.get_host_port("room-1", 8080)

    assert port == 12345


@pytest.mark.asyncio
async def test_get_host_port_no_mapping(settings):
    """get_host_port returns None when podman port fails (no mapping)."""
    mocker = SubprocessMocker()
    mocker.on("podman", "port", "sandbox-room-1", "8080", returncode=1, stderr=b"no port")

    sandbox = _make_sandbox(settings)
    sandbox._containers = {"room-1": "sandbox-room-1"}

    with patch("asyncio.create_subprocess_exec", mocker):
        port = await sandbox.get_host_port("room-1", 8080)

    assert port is None


@pytest.mark.asyncio
async def test_get_host_port_no_container(settings):
    """get_host_port returns None when chat_id has no container."""
    sandbox = _make_sandbox(settings)
    # sandbox._containers is empty

    port = await sandbox.get_host_port("unknown-room", 8080)
    assert port is None
