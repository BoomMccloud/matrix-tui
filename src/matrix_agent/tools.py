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
            "name": "plan",
            "description": (
                "Ask Gemini CLI to plan, analyze, or explain (1M token context). "
                "Use for: writing implementation plans, analyzing codebases, first-principles thinking, "
                "checking if a solution is the simplest approach. Gemini can read entire repos at once."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "What to plan or analyze. Be specific about goals and constraints.",
                    },
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "implement",
            "description": (
                "Ask Qwen Code to write or modify code. "
                "Use for: implementing features, fixing bugs, refactoring, writing tests. "
                "Pass the plan or requirements in the task description."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "What to implement. Include the plan, specific files, and acceptance criteria.",
                    },
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "review",
            "description": (
                "Ask Gemini CLI to review code changes (1M token context). "
                "Use after implementation to check for bugs, security issues, "
                "missed edge cases, and adherence to project conventions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "What to review. Reference specific files or describe what changed.",
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

    if name in ("plan", "implement", "review"):
        cli = "qwen" if name == "implement" else "gemini"
        log.info("Routing %s → %s", name, cli)
        if send_update:
            rc, stdout, stderr = await sandbox.code_stream(chat_id, args["task"], send_update, cli=cli)
        else:
            rc, stdout, stderr = await sandbox.code(chat_id, args["task"], cli=cli)
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
    """Run git pull + image rebuild, then restart the service."""
    repo = "/home/matrix-tui"

    async def run(cmd: list[str]) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=repo,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
        return proc.returncode or 0, stdout.decode().strip()

    log.info("self_update: git pull")
    rc, pull_out = await run(["git", "pull"])
    if rc != 0:
        return f"git pull failed (exit {rc}):\n{pull_out}"

    log.info("self_update: rebuilding sandbox image")
    rc, build_out = await run([
        "podman", "build", "-t", "matrix-agent-sandbox:latest", "-f", "Containerfile", ".",
    ])
    if rc != 0:
        return f"git pull OK, but image build failed (exit {rc}):\n{build_out}"

    # Everything succeeded — restart after a short delay so this result can be sent first
    log.info("self_update: restarting service")
    asyncio.create_task(_delayed_restart())
    return f"git pull:\n{pull_out}\n\nImage build: OK\n\nRestarting service in 2s..."


async def _delayed_restart():
    await asyncio.sleep(2)
    proc = await asyncio.create_subprocess_exec("systemctl", "restart", "matrix-agent")
    await proc.wait()
