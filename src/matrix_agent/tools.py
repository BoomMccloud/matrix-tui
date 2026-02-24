"""Tool definitions and dispatch for the agent."""

import asyncio
import base64
import json
import logging

from .sandbox import SandboxManager

log = logging.getLogger(__name__)

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command in the sandbox container. Returns stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file in the sandbox container.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path in the container",
                    },
                    "content": {
                        "type": "string",
                        "description": "File content to write",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the sandbox container.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path in the container",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "code",
            "description": (
                "Delegate a coding or analysis task to Gemini CLI (1M token context). "
                "Use this for writing new code, bug fixes, refactoring, code review, "
                "and explaining how a codebase works. Gemini can read entire repos at once. "
                "The task is passed safely without shell escaping issues."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "What to code, analyze, or explain. Be specific about files and goals.",
                    },
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "self_update",
            "description": (
                "Update the bot itself on the VPS host: runs git pull then restarts the systemd service. "
                "Use this when the user asks to update the bot, pull latest changes, or restart the service. "
                "This operates on the HOST, not inside the sandbox container."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "take_screenshot",
            "description": "Take a browser screenshot of a URL accessible from inside the container. Use this after starting a web server to see the result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to screenshot (e.g. http://localhost:3000)",
                    },
                },
                "required": ["url"],
            },
        },
    },
]


async def execute_tool(
    sandbox: SandboxManager, chat_id: str, name: str, arguments: str,
) -> tuple[str, bytes | None]:
    """Execute a tool call. Returns (text_result, optional_image_bytes)."""
    args = json.loads(arguments)

    if name == "run_command":
        rc, stdout, stderr = await sandbox.exec(chat_id, args["command"])
        output = stdout
        if stderr:
            output += f"\nSTDERR:\n{stderr}"
        if rc != 0:
            output += f"\n[exit code: {rc}]"
        # Truncate very long output
        if len(output) > 10000:
            output = output[:10000] + "\n... (truncated)"
        return output, None

    if name == "write_file":
        result = await sandbox.write_file(chat_id, args["path"], args["content"])
        return result, None

    if name == "read_file":
        result = await sandbox.read_file(chat_id, args["path"])
        if len(result) > 10000:
            result = result[:10000] + "\n... (truncated)"
        return result, None

    if name == "code":
        rc, stdout, stderr = await sandbox.code(chat_id, args["task"])
        output = stdout
        if stderr:
            output += f"\nSTDERR:\n{stderr}"
        if rc != 0:
            output += f"\n[exit code: {rc}]"
        if len(output) > 10000:
            output = output[:10000] + "\n... (truncated)"
        return output, None

    if name == "self_update":
        return await _self_update(), None

    if name == "take_screenshot":
        img = await sandbox.screenshot(chat_id, args["url"])
        if img:
            return "Screenshot taken successfully.", img
        return "Screenshot failed.", None

    return f"Unknown tool: {name}", None


async def _self_update() -> str:
    """Run git pull then restart the systemd service on the host."""
    async def run(cmd: list[str]) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd="/home/matrix-tui",
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        return proc.returncode or 0, stdout.decode().strip()

    log.info("self_update: running git pull")
    rc, out = await run(["git", "pull"])
    if rc != 0:
        return f"git pull failed (exit {rc}):\n{out}"

    pull_output = out
    log.info("self_update: restarting matrix-agent service")

    # Restart is fire-and-forget — the process will be killed mid-response,
    # so we send the pull result before the restart takes effect.
    asyncio.create_task(_delayed_restart())

    return f"git pull:\n{pull_output}\n\nRestarting service in 2s..."


async def _delayed_restart():
    await asyncio.sleep(2)
    proc = await asyncio.create_subprocess_exec(
        "systemctl", "restart", "matrix-agent",
    )
    await proc.wait()
