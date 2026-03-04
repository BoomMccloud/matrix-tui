"""Podman sandbox manager — one container per chat."""

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .config import Settings

log = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"

# Maps template filename -> container destination path
TEMPLATES = {
    "GEMINI.md": "/workspace/GEMINI.md",
    "status.md": "/workspace/status.md",
    "settings.json": "/workspace/.gemini/settings.json",
    "hook-session-start.sh": "/workspace/.gemini/hooks/session-start.sh",
    "hook-after-agent.sh": "/workspace/.gemini/hooks/after-agent.sh",
    "hook-after-tool.sh": "/workspace/.gemini/hooks/after-tool.sh",
    "hook-notification.sh": "/workspace/.gemini/hooks/notification.sh",
    "hook-before-tool.sh": "/workspace/.gemini/hooks/before-tool.sh",
    "cmd-fix-issue.toml": "/workspace/.gemini/commands/fix-issue.toml",
    "cmd-fix-ci.toml": "/workspace/.gemini/commands/fix-ci.toml",
    "skill-delegate-qwen.md": "/workspace/.gemini/skills/delegate-qwen/SKILL.md",
    "qwen-wrapper.sh": "/workspace/.qwen-wrapper.sh",
    "qwen-settings.json": "/root/.qwen/settings.json",
}

# Templates that need chmod +x
_EXECUTABLE_TEMPLATES = {
    "hook-session-start.sh",
    "hook-after-agent.sh",
    "hook-after-tool.sh",
    "hook-notification.sh",
    "hook-before-tool.sh",
    "qwen-wrapper.sh",
}

STATE_PATH = "/home/matrix-tui/state.json"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mABCDEFGHJKSTfisu]")

# Forbidden files — shared by check_forbidden(), validate_work(), and hook-before-tool.sh
# Keep in sync with DENIED_NAMES / DENIED_DIRS in hook-before-tool.sh
FORBIDDEN_NAMES = {
    "pyproject.toml", "uv.lock", "package-lock.json", "Cargo.lock", "go.sum",
    ".gitignore", "CLAUDE.md", "AGENTS.md", "Containerfile", "Makefile",
    "pr-url.txt", "acceptance-criteria.md", "status.md", "GEMINI.md",
    "conftest.py", "__init__.py",
}
FORBIDDEN_PREFIXES = (".gemini/", ".claude/", ".github/", "scripts/", "src/matrix_agent/templates/")


def check_forbidden(file_list: list[str]) -> list[str]:
    """Return list of forbidden files from a file list. Empty = clean."""
    return [
        f for f in file_list
        if f in FORBIDDEN_NAMES
        or any(f.startswith(p) for p in FORBIDDEN_PREFIXES)
    ]


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _container_name(chat_id: str) -> str:
    """Stable container name derived from room ID — safe for podman --name."""
    slug = re.sub(r"[^a-zA-Z0-9_.-]", "-", chat_id).strip("-")
    return f"sandbox-{slug}"


class SandboxManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.podman = settings.podman_path
        self.image = settings.sandbox_image
        self.timeout = settings.command_timeout_seconds
        self._containers: dict[str, str] = {}  # chat_id -> container_name
        # Reference to decider histories — set by Decider after construction
        self._histories: dict[str, list[dict]] | None = None

    def has_container(self, chat_id: str) -> bool:
        return chat_id in self._containers

    def container_ids(self) -> list[str]:
        return list(self._containers)

    async def _run(
        self, *args: str, timeout: int | None = None, stdin_data: bytes | None = None,
    ) -> tuple[int, str, str]:
        timeout = timeout or self.timeout
        proc = await asyncio.create_subprocess_exec(
            self.podman, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if stdin_data else None,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=stdin_data), timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 1, "", f"Command timed out after {timeout}s"
        return proc.returncode or 0, stdout.decode(), stderr.decode()

    # ------------------------------------------------------------------ #
    # State persistence
    # ------------------------------------------------------------------ #

    def save_state(self) -> None:
        """Atomically write containers + histories to state.json."""
        state = {
            "containers": self._containers,
            "history": self._histories or {},
        }
        tmp = STATE_PATH + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, STATE_PATH)
            log.debug("State saved (%d rooms)", len(self._containers))
        except Exception:
            log.exception("Failed to save state")

    async def load_state(self) -> dict[str, list[dict]]:
        """Load state.json. Returns histories dict (containers loaded into self._containers).
        Verifies each container is still running; removes stale entries."""
        if not os.path.exists(STATE_PATH):
            log.info("No state.json found — starting fresh")
            return {}

        try:
            with open(STATE_PATH) as f:
                state = json.load(f)
        except Exception:
            log.exception("Failed to read state.json — starting fresh")
            return {}

        containers: dict[str, str] = state.get("containers", {})
        histories: dict[str, list[dict]] = state.get("history", {})

        # Verify each container is still alive
        live: dict[str, str] = {}
        for chat_id, name in containers.items():
            rc, out, _ = await self._run("inspect", "--format", "{{.State.Status}}", name)
            if rc == 0 and out.strip() == "running":
                live[chat_id] = name
                log.info("Reconnected container %s for %s", name, chat_id)
            else:
                log.info("Stale container %s for %s — will recreate on next message", name, chat_id)
                histories.pop(chat_id, None)

        self._containers = live
        return histories

    # ------------------------------------------------------------------ #
    # Container lifecycle
    # ------------------------------------------------------------------ #

    async def create(self, chat_id: str) -> str:
        if chat_id in self._containers:
            return self._containers[chat_id]

        name = _container_name(chat_id)

        ipc_host = os.path.join(self.settings.ipc_base_dir, name)
        os.makedirs(ipc_host, exist_ok=True)

        env_flags: list[str] = []
        if self.settings.gemini_api_key:
            env_flags += ["-e", f"GEMINI_API_KEY={self.settings.gemini_api_key}"]
        if self.settings.gemini_model:
            env_flags += ["-e", f"GEMINI_MODEL={self.settings.gemini_model}"]
        if self.settings.dashscope_api_key:
            env_flags += ["-e", f"DASHSCOPE_API_KEY={self.settings.dashscope_api_key}"]
        if self.settings.github_token:
            env_flags += ["-e", f"GITHUB_TOKEN={self.settings.github_token}"]

        rc, out, err = await self._run(
            "run", "-d",
            "--name", name,
            "--shm-size=256m",
            "-v", f"{ipc_host}:/workspace/.ipc:Z",
            *env_flags,
            self.image,
            "sleep", "infinity",
        )
        if rc != 0 and "already in use" in err:
            log.warning("Stale container %s found — removing and retrying", name)
            await self._run("rm", "-f", name, timeout=15)
            rc, out, err = await self._run(
                "run", "-d",
                "--name", name,
                "--shm-size=256m",
                "-v", f"{ipc_host}:/workspace/.ipc:Z",
                *env_flags,
                self.image,
                "sleep", "infinity",
            )
        if rc != 0:
            raise RuntimeError(f"Failed to create container: {err}")

        self._containers[chat_id] = name
        log.info("Created container %s for chat %s", name, chat_id)
        await self._init_workspace(name)
        self.save_state()
        return name

    async def _init_workspace(self, container_name: str) -> None:
        """Initialize workspace coordination files on container creation."""
        # Build a shell script that writes all templates in one exec call
        script_parts = []
        for template_name, container_path in TEMPLATES.items():
            content = (_TEMPLATES_DIR / template_name).read_text()
            # Use heredoc per file — delimiter includes template name for uniqueness
            delimiter = f"EOF_{template_name.replace('.', '_').replace('-', '_')}"
            script_parts.append(
                f"mkdir -p $(dirname {container_path})\n"
                f"cat > {container_path} <<'{delimiter}'\n"
                f"{content}\n"
                f"{delimiter}"
            )

        # chmod executable templates
        exec_paths = [
            TEMPLATES[name] for name in _EXECUTABLE_TEMPLATES
            if name in TEMPLATES
        ]
        if exec_paths:
            script_parts.append(f"chmod +x {' '.join(exec_paths)}")

        # Git identity
        script_parts.append('git config --global user.email "bot@matrix-agent"')
        script_parts.append('git config --global user.name "Matrix Agent"')

        # gh CLI auth
        if self.settings.github_token:
            script_parts.append("gh auth setup-git")

        script = "\n".join(script_parts)
        rc, out, err = await self._run(
            "exec", "-i", container_name, "sh",
            stdin_data=script.encode(),
            timeout=30,
        )
        if rc != 0:
            log.error("_init_workspace failed for %s: %s", container_name, err[:500])

    async def exec(self, chat_id: str, command: str) -> tuple[int, str, str]:
        name = self._containers.get(chat_id)
        if not name:
            raise RuntimeError(f"No container for chat {chat_id}")
        return await self._run("exec", name, "sh", "-c", command)

    async def write_file(self, chat_id: str, path: str, content: str) -> str:
        name = self._containers.get(chat_id)
        if not name:
            raise RuntimeError(f"No container for chat {chat_id}")

        await self._run("exec", name, "mkdir", "-p", os.path.dirname(path))

        rc, out, err = await self._run(
            "exec", "-i", name, "sh", "-c", f"cat > {path}",
            stdin_data=content.encode(),
        )
        if rc != 0:
            return f"Error writing file: {err}"
        return f"Wrote {len(content)} bytes to {path}"

    async def read_file(self, chat_id: str, path: str) -> str:
        rc, out, err = await self.exec(chat_id, f"cat {path}")
        if rc != 0:
            return f"Error reading file: {err}"
        return out

    async def screenshot(self, chat_id: str, url: str) -> bytes | None:
        name = self._containers.get(chat_id)
        if not name:
            raise RuntimeError(f"No container for chat {chat_id}")

        container_path = "/tmp/screenshot.png"
        script = self.settings.screenshot_script
        rc, out, err = await self._run(
            "exec", name, "node", script, url, container_path,
            timeout=30,
        )
        if rc != 0:
            log.error("Screenshot failed: %s", err)
            return None

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False, dir="/tmp") as f:
            host_path = f.name

        rc, out, err = await self._run("cp", f"{name}:{container_path}", host_path)
        if rc != 0:
            log.error("Screenshot cp failed: %s", err)
            return None

        try:
            with open(host_path, "rb") as f:
                return f.read()
        finally:
            os.unlink(host_path)

    async def get_host_port(self, chat_id: str, container_port: int) -> int | None:
        name = self._containers.get(chat_id)
        if not name:
            return None
        rc, out, err = await self._run("port", name, str(container_port))
        if rc != 0:
            return None
        try:
            return int(out.strip().split(":")[-1])
        except (ValueError, IndexError):
            return None

    async def destroy(self, chat_id: str) -> None:
        name = self._containers.pop(chat_id, None)
        if not name:
            return
        await self._run("stop", name, timeout=15)
        await self._run("rm", "-f", name, timeout=15)
        ipc_host = os.path.join(self.settings.ipc_base_dir, name)
        shutil.rmtree(ipc_host, ignore_errors=True)
        log.info("Destroyed container %s for chat %s", name, chat_id)
        self.save_state()

    async def code_stream(
        self,
        chat_id: str,
        task: str,
        on_chunk: Callable[[str], Awaitable[Any]],
        cli: str = "gemini",
        chunk_size: int = 800,
        auto_accept: bool = False,
        workdir: str = "/workspace",
    ) -> tuple[int, str, str]:
        """Run a coding CLI, streaming stdout to on_chunk() as it arrives."""
        import time
        name = self._containers.get(chat_id)
        if not name:
            raise RuntimeError(f"No container for chat {chat_id}")

        # Use qwen wrapper when auto_accept — it writes IPC event-result.json
        if cli == "qwen" and auto_accept:
            cli_args = ["/workspace/.qwen-wrapper.sh", task]
        else:
            cli_args = [cli]
            if auto_accept:
                cli_args.append("-y")
            cli_args += ["-p", task]

        log.info("[%s] %s starting: %s", name, cli, task[:200])
        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            self.podman, "exec", "--workdir", workdir, name,
            *cli_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        buffer: list[str] = []

        async def flush():
            if buffer:
                await on_chunk("".join(buffer))
                buffer.clear()

        async def read_stdout():
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = _strip_ansi(raw.decode(errors="replace"))
                stdout_parts.append(line)
                if line.strip():
                    log.info("[%s] %s", name, line.rstrip())
                buffer.append(line)
                if sum(len(b) for b in buffer) >= chunk_size:
                    await flush()

        async def read_stderr():
            assert proc.stderr is not None
            async for raw in proc.stderr:
                stderr_parts.append(raw.decode(errors="replace"))

        try:
            await asyncio.wait_for(
                asyncio.gather(read_stdout(), read_stderr()),
                timeout=self.settings.coding_timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await flush()
            elapsed = time.monotonic() - t0
            log.warning("[%s] %s timed out after %.0fs", name, cli, elapsed)
            return 1, "".join(stdout_parts), f"Command timed out after {self.settings.coding_timeout_seconds}s"

        await flush()
        await proc.wait()
        elapsed = time.monotonic() - t0
        rc = proc.returncode or 0
        stdout_len = sum(len(s) for s in stdout_parts)
        log.info("[%s] %s finished in %.1fs (exit=%d, stdout=%d chars)", name, cli, elapsed, rc, stdout_len)
        return rc, "".join(stdout_parts), "".join(stderr_parts)

    async def run_gemini_session(
        self,
        chat_id: str,
        prompt: str,
        on_chunk: Callable[[str], Awaitable[Any]],
        repo_name: str,
    ) -> tuple[int, str, str | None]:
        """Run a single Gemini CLI session for a GitHub issue.

        Wraps code_stream() with repo-specific workdir, then reads IPC files.
        Returns (exit_code, stdout, pr_url_or_none).
        """
        workdir = f"/workspace/{repo_name}"
        rc, stdout, stderr = await self.code_stream(
            chat_id, prompt, on_chunk,
            cli="gemini", auto_accept=True, workdir=workdir,
        )

        # Read PR URL from IPC (source of truth for success)
        pr_url = None
        name = self._containers.get(chat_id)
        if name:
            ipc_host = os.path.join(self.settings.ipc_base_dir, name)
            pr_url_path = os.path.join(ipc_host, "pr-url.txt")
            if os.path.exists(pr_url_path):
                with open(pr_url_path) as f:
                    pr_url = f.read().strip() or None

        return rc, stdout, pr_url

    async def read_ipc_file(self, chat_id: str, filename: str) -> str | None:
        """Read a file from the container's IPC directory. Returns content or None."""
        name = self._containers.get(chat_id)
        if not name:
            return None
        ipc_host = os.path.join(self.settings.ipc_base_dir, name)
        path = os.path.join(ipc_host, filename)
        if os.path.exists(path):
            with open(path) as f:
                content = f.read().strip()
            return content or None
        return None

    def write_ipc_file(self, chat_id: str, filename: str, content: str) -> None:
        """Write a file to the container's IPC directory."""
        name = self._containers.get(chat_id)
        if not name:
            return
        ipc_host = os.path.join(self.settings.ipc_base_dir, name)
        os.makedirs(ipc_host, exist_ok=True)
        path = os.path.join(ipc_host, filename)
        with open(path, "w") as f:
            f.write(content)

    async def validate_work(
        self, chat_id: str, repo_name: str,
    ) -> tuple[bool, list[str]]:
        """Host-side validation after Gemini exits.

        Returns (passed, list_of_failure_descriptions).
        """
        failures: list[str] = []
        repo_path = f"/workspace/{repo_name}"

        # Resolve container and IPC dir once
        name = self._containers.get(chat_id)
        ipc_host = os.path.join(self.settings.ipc_base_dir, name) if name else None

        # 1. Run tests — detect project type from marker files
        detect_and_test = (
            f"cd {repo_path} && "
            "if [ -f pyproject.toml ] || [ -f setup.py ]; then "
            "  uv run ruff check . 2>&1 && uv run pytest -v 2>&1; "
            "elif [ -f package.json ]; then "
            "  npm run lint 2>&1; npm test 2>&1; "
            "elif [ -f Cargo.toml ]; then "
            "  cargo test 2>&1; "
            "elif [ -f go.mod ]; then "
            "  go test ./... 2>&1; "
            "else "
            "  echo 'No recognized project type — skipping tests'; "
            "fi"
        )
        rc, stdout, stderr = await self.exec(chat_id, detect_and_test)
        if rc != 0:
            test_output = (stdout + stderr)[:3000]
            failures.append(f"Tests/lint failing:\n{test_output}")

        # 2. Check scope — flag forbidden files and excessive changes
        # Compare against the merge base with main/master to only see the bot's changes
        rc, stdout, _ = await self.exec(
            chat_id,
            f"cd {repo_path} && "
            "base=$(git merge-base HEAD origin/main 2>/dev/null || git merge-base HEAD origin/master 2>/dev/null || echo HEAD~1) && "
            "git diff --name-only $base HEAD 2>/dev/null || echo 'no commits'",
        )
        if stdout.strip() and stdout.strip() != "no commits":
            changed_files = [f.strip() for f in stdout.strip().splitlines() if f.strip()]
            log.info("[%s] Scope: %d files changed: %s", chat_id[:20], len(changed_files), ", ".join(changed_files))

            # Check file manifest — declared scope
            if ipc_host:
                manifest_path = os.path.join(ipc_host, "changed-files.txt")
                if os.path.exists(manifest_path):
                    with open(manifest_path) as mf:
                        declared = {line.strip() for line in mf if line.strip()}
                    undeclared = [f for f in changed_files if f not in declared]
                    if undeclared:
                        failures.append(
                            f"Files modified outside declared scope: {', '.join(undeclared)}. "
                            f"Declared: {', '.join(sorted(declared))}. "
                            f"Revert: git checkout $base -- {' '.join(undeclared)}"
                        )
                else:
                    failures.append(
                        "No file manifest (changed-files.txt) found — "
                        "write declared files to /workspace/.ipc/changed-files.txt"
                    )

            # Block forbidden file patterns
            forbidden = check_forbidden(changed_files)
            if forbidden:
                failures.append(
                    f"Scope creep: modified forbidden files: {', '.join(forbidden)}. "
                    f"Revert with: git checkout HEAD~1 -- {' '.join(forbidden)}"
                )

        # 3. Check pr-url.txt exists
        if ipc_host:
            pr_url_path = os.path.join(ipc_host, "pr-url.txt")
            if not os.path.exists(pr_url_path):
                failures.append("No PR created (pr-url.txt missing)")

            # 4. Check acceptance criteria
            ac_path = os.path.join(ipc_host, "acceptance-criteria.md")
            if not os.path.exists(ac_path) or os.path.getsize(ac_path) == 0:
                failures.append("Acceptance criteria not generated (acceptance-criteria.md missing or empty)")

        passed = len(failures) == 0
        if passed:
            log.info("[%s] validate_work: all checks passed", chat_id[:20])
        else:
            log.warning("[%s] validate_work: %d failures: %s",
                        chat_id[:20], len(failures),
                        "; ".join(f[:80] for f in failures))
        return passed, failures

    async def code(self, chat_id: str, task: str, cli: str = "gemini", auto_accept: bool = False) -> tuple[int, str, str]:
        """Run a coding CLI on a task. Task passed as direct argv — no shell escaping needed.
        Runs from /workspace so context files are auto-loaded."""
        name = self._containers.get(chat_id)
        if not name:
            raise RuntimeError(f"No container for chat {chat_id}")
        cli_args = [cli]
        if auto_accept:
            cli_args.append("-y")
        cli_args += ["-p", task]
        return await self._run(
            "exec", "--workdir", "/workspace", name,
            *cli_args,
            timeout=self.settings.coding_timeout_seconds,
        )

    async def destroy_all(self) -> None:
        for chat_id in list(self._containers):
            await self.destroy(chat_id)
