"""Tool definitions and dispatch for the agent."""

import base64
import json

from .sandbox import SandboxManager

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

    if name == "take_screenshot":
        img = await sandbox.screenshot(chat_id, args["url"])
        if img:
            return "Screenshot taken successfully.", img
        return "Screenshot failed.", None

    return f"Unknown tool: {name}", None
