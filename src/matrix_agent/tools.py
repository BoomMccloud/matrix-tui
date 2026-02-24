"""Tool definitions and dispatch for the agent."""

import asyncio
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
            "name": "run_tests",
            "description": (
                "Run lint (ruff) and tests (pytest) in the sandbox container. "
                "Call this after writing or modifying code to verify the build is clean. "
                "Returns pass/fail status and any errors."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory to run tests in. Defaults to /workspace.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "self_update",
            "description": (
                "Update the bot itself on the VPS host: runs deploy.sh (git pull + rebuild sandbox image + restart service). "
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
    send_update=None,
) -> tuple[str, bytes | None]:
    """Execute a tool call. Returns (text_result, optional_image_bytes)."""
    args = json.loads(arguments) if arguments and arguments.strip() else {}

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
        if send_update:
            rc, stdout, stderr = await sandbox.code_stream(chat_id, args["task"], send_update)
        else:
            rc, stdout, stderr = await sandbox.code(chat_id, args["task"])
        output = stdout
        if stderr:
            output += f"\nSTDERR:\n{stderr}"
        if rc != 0:
            output += f"\n[exit code: {rc}]"
        if len(output) > 10000:
            output = output[:10000] + "\n... (truncated)"
        return output, None

    if name == "run_tests":
        path = args.get("path", "/workspace")
        lint_rc, lint_out, lint_err = await sandbox.exec(chat_id, f"cd {path} && ruff check .")
        test_rc, test_out, test_err = await sandbox.exec(chat_id, f"cd {path} && pytest -v 2>&1 || true")
        lint_result = lint_out or lint_err or "No issues."
        test_result = test_out or test_err or "No output."
        status = "PASS" if lint_rc == 0 and test_rc == 0 else "FAIL"
        output = f"[{status}]\n\n=== Lint (ruff) ===\n{lint_result}\n\n=== Tests (pytest) ===\n{test_result}"
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
    """Run deploy.sh on the host (git pull + rebuild sandbox image + restart service)."""
    script = "/home/matrix-tui/scripts/deploy.sh"
    log.info("self_update: running %s", script)

    proc = await asyncio.create_subprocess_exec(
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd="/home/matrix-tui",
    )

    # Capture output up until the service restart kills us.
    # We fire the wait in a task so we can return partial output quickly.
    asyncio.create_task(_wait_deploy(proc))
    await asyncio.sleep(2)

    # Read whatever output is available so far
    assert proc.stdout is not None
    output = await proc.stdout.read(4096)
    return f"deploy.sh output (may be truncated — service restarting):\n{output.decode().strip()}"


async def _wait_deploy(proc):
    try:
        await asyncio.wait_for(proc.wait(), timeout=120)
    except Exception:
        pass
