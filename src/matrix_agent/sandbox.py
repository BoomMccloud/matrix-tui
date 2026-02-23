"""Podman sandbox manager — one container per chat."""

import asyncio
import logging
import os
import tempfile

from .config import Settings

log = logging.getLogger(__name__)


class SandboxManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.podman = settings.podman_path
        self.image = settings.sandbox_image
        self.timeout = settings.command_timeout_seconds
        self._containers: dict[str, str] = {}  # chat_id -> container_id

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

    async def create(self, chat_id: str) -> str:
        if chat_id in self._containers:
            return self._containers[chat_id]

        env_flags: list[str] = []
        if self.settings.gemini_api_key:
            env_flags += ["-e", f"GEMINI_API_KEY={self.settings.gemini_api_key}"]

        rc, out, err = await self._run(
            "run", "-d",
            "--shm-size=256m",
            *env_flags,
            self.image,
            "sleep", "infinity",
        )
        if rc != 0:
            raise RuntimeError(f"Failed to create container: {err}")
        cid = out.strip()
        self._containers[chat_id] = cid
        log.info("Created container %s for chat %s", cid[:12], chat_id)
        return cid

    async def exec(self, chat_id: str, command: str) -> tuple[int, str, str]:
        cid = self._containers.get(chat_id)
        if not cid:
            raise RuntimeError(f"No container for chat {chat_id}")
        return await self._run("exec", cid, "sh", "-c", command)

    async def write_file(self, chat_id: str, path: str, content: str) -> str:
        cid = self._containers.get(chat_id)
        if not cid:
            raise RuntimeError(f"No container for chat {chat_id}")

        # Ensure parent directory exists
        await self._run("exec", cid, "mkdir", "-p", os.path.dirname(path))

        rc, out, err = await self._run(
            "exec", "-i", cid, "sh", "-c", f"cat > {path}",
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
        cid = self._containers.get(chat_id)
        if not cid:
            raise RuntimeError(f"No container for chat {chat_id}")

        container_path = "/tmp/screenshot.png"
        script = self.settings.screenshot_script
        rc, out, err = await self._run(
            "exec", cid, "node", script, url, container_path,
            timeout=30,
        )
        if rc != 0:
            log.error("Screenshot failed: %s", err)
            return None

        # Copy out via podman cp to a temp file
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False, dir="/private/tmp") as f:
            host_path = f.name

        rc, out, err = await self._run("cp", f"{cid}:{container_path}", host_path)
        if rc != 0:
            log.error("Screenshot cp failed: %s", err)
            return None

        try:
            with open(host_path, "rb") as f:
                return f.read()
        finally:
            os.unlink(host_path)

    async def get_host_port(self, chat_id: str, container_port: int) -> int | None:
        cid = self._containers.get(chat_id)
        if not cid:
            return None
        rc, out, err = await self._run("port", cid, str(container_port))
        if rc != 0:
            return None
        # Output like "0.0.0.0:12345"
        try:
            return int(out.strip().split(":")[-1])
        except (ValueError, IndexError):
            return None

    async def destroy(self, chat_id: str) -> None:
        cid = self._containers.pop(chat_id, None)
        if not cid:
            return
        await self._run("stop", cid, timeout=15)
        await self._run("rm", "-f", cid, timeout=15)
        log.info("Destroyed container %s for chat %s", cid[:12], chat_id)

    async def code(self, chat_id: str, task: str) -> tuple[int, str, str]:
        """Run Gemini CLI on a task. Task is passed as a direct argv argument (no shell escaping needed)."""
        cid = self._containers.get(chat_id)
        if not cid:
            raise RuntimeError(f"No container for chat {chat_id}")
        return await self._run(
            "exec", cid,
            "gemini", "-p", task,
            timeout=self.settings.coding_timeout_seconds,
        )

    async def destroy_all(self) -> None:
        for chat_id in list(self._containers):
            await self.destroy(chat_id)
