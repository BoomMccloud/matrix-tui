"""Podman sandbox manager — one container per chat."""

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from typing import Any

from .config import Settings

log = logging.getLogger(__name__)

STATE_PATH = "/home/matrix-tui/state.json"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mABCDEFGHJKSTfisu]")


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
        # Reference to agent histories — set by Agent after construction
        self._histories: dict[str, list[dict]] | None = None

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
        if rc != 0:
            raise RuntimeError(f"Failed to create container: {err}")

        self._containers[chat_id] = name
        log.info("Created container %s for chat %s", name, chat_id)
        await self._init_workspace(name)
        self.save_state()
        return name

    async def _init_workspace(self, container_name: str) -> None:
        """Initialize workspace coordination files on container creation."""
        async def write(path: str, content: str) -> None:
            await self._run(
                "exec", "-i", container_name, "sh", "-c", f"mkdir -p $(dirname {path}) && cat > {path}",
                stdin_data=content.encode(),
            )

        # status.md — append-only worklog, shared by all agents, not in git
        await write("/workspace/status.md", """\
# Status Log

Append one line per task in this format:
[YYYY-MM-DD HH:MM] <what was done>

Example:
[2026-02-24 10:12] Cloned matrix-tui repo, ran /init to generate GEMINI.md
[2026-02-24 10:31] Added error handling to sandbox.py create() method
[2026-02-24 11:05] Fixed off-by-one in container name slug — replaced spaces with dashes

""")

        # GEMINI.md — auto-loaded by Gemini CLI from cwd (/workspace), not in git
        # Imports status.md for session history. Repo GEMINI.md (generated by /init)
        # is loaded automatically when Gemini runs inside the repo directory.
        await write("/workspace/GEMINI.md", """\
# Workspace Context

This file is your instruction set. It is loaded automatically on every invocation.

## Prior work (auto-imported)

@status.md

## Rules

1. Before starting any task, read status.md (imported above) to understand what has already been done.
2. When working inside a cloned repo, read AGENTS.md in the repo root for conventions and architecture.
3. After completing each task, append one line to /workspace/status.md:
   [YYYY-MM-DD HH:MM] <what was done>
4. When you discover a convention, gotcha, or architectural decision worth remembering,
   append it to AGENTS.md in the repo root. Use the format shown in that file.
5. After cloning a new repo, run `/init` inside the repo directory to generate
   a project-specific GEMINI.md with codebase context.

## What NOT to put in status.md
- Do not put decisions or conventions here — those go in AGENTS.md
- Do not edit old entries — append only
- One line per task, no multi-line entries
""")

        # AfterAgent + UserInputRequired hooks
        await write("/workspace/.gemini/settings.json", """\
{
  "hooks": {
    "AfterAgent": [
      {
        "hooks": [
          {
            "name": "append-status",
            "type": "command",
            "command": "/workspace/.gemini/hooks/after-agent.sh",
            "timeout": 5000
          }
        ]
      }
    ],
    "AfterTool": [
      {
        "hooks": [
          {
            "name": "progress-ipc",
            "type": "command",
            "command": "/workspace/.gemini/hooks/after-tool.sh",
            "timeout": 5000
          }
        ]
      }
    ],
    "Notification": [
      {
        "hooks": [
          {
            "name": "notify-matrix",
            "type": "command",
            "command": "/workspace/.gemini/hooks/notification.sh",
            "timeout": 5000
          }
        ]
      }
    ]
  }
}
""")

        await write("/workspace/.gemini/hooks/after-agent.sh", """\
#!/bin/sh
# AfterAgent hook — writes result to IPC, appends timestamp to status.md.
# Reads JSON from stdin (Gemini hook protocol), writes JSON to stdout.
input=$(cat)
echo "$input" > /workspace/.ipc/event-result.json 2>> /workspace/.ipc/hook-errors.log
timestamp=$(date '+%Y-%m-%d %H:%M')
echo "[$timestamp] Gemini session completed" >> /workspace/status.md 2>> /workspace/.ipc/hook-errors.log
echo '{"continue": true}'
""")

        await write("/workspace/.gemini/hooks/after-tool.sh", """\
#!/bin/sh
# AfterTool hook — writes tool progress to IPC for host watcher.
input=$(cat)
echo "$input" > /workspace/.ipc/event-progress.json 2>> /workspace/.ipc/hook-errors.log
echo '{}'
""")

        await write("/workspace/.gemini/hooks/notification.sh", """\
#!/bin/sh
# Notification hook — writes sentinel file to IPC dir for host watcher.
# Gemini sends JSON on stdin with message, notification_type, details fields.
cat > /workspace/.ipc/notification.json
echo '{}'
""")

        # Qwen wrapper — captures output and writes IPC event-result.json (no hook support)
        await write("/workspace/.qwen-wrapper.sh", """\
#!/bin/sh
# Wrapper for qwen CLI — writes event-result.json on completion.
# Usage: .qwen-wrapper.sh "prompt text"
output=$(qwen -y -p "$1" 2>&1) || true
rc=$?
timestamp=$(date '+%Y-%m-%dT%H:%M:%S')
cat > /workspace/.ipc/event-result.json <<IPCEOF
{"cli": "qwen", "exit_code": $rc, "timestamp": "$timestamp"}
IPCEOF
if [ $rc -ne 0 ]; then
  echo "wrapper error: qwen exited $rc" >> /workspace/.ipc/hook-errors.log
fi
echo "$output"
exit $rc
""")

        # Qwen Code settings — DashScope international endpoint
        await write("/root/.qwen/settings.json", """\
{
  "modelProviders": {
    "openai": [
      {
        "id": "qwen3-coder-next",
        "name": "qwen3-coder-next",
        "baseUrl": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "envKey": "DASHSCOPE_API_KEY"
      }
    ]
  },
  "security": { "auth": { "selectedType": "openai" } },
  "model": { "name": "qwen3-coder-next" }
}
""")

        # Make hooks executable
        await self._run(
            "exec", container_name,
            "chmod", "+x",
            "/workspace/.gemini/hooks/after-agent.sh",
            "/workspace/.gemini/hooks/after-tool.sh",
            "/workspace/.gemini/hooks/notification.sh",
            "/workspace/.qwen-wrapper.sh",
        )

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
            self.podman, "exec", "--workdir", "/workspace", name,
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
