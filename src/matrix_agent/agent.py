"""LLM agent with tool-calling loop."""

import logging

import litellm

from .config import Settings
from .sandbox import SandboxManager
from .tools import TOOL_SCHEMAS, execute_tool

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a coding assistant running inside a sandboxed container. You can:
- Run shell commands (run_command)
- Read and write files (read_file, write_file)
- Take browser screenshots of web pages served from the container (take_screenshot)
- Delegate coding and analysis tasks to Gemini CLI (code)

The container has Node.js 20, Python 3, git, and Gemini CLI installed.
Work in /workspace. When you start a web server, use take_screenshot to show the result.

Use the `code` tool for any non-trivial coding task — writing features, fixing bugs, refactoring,
reviewing code, or explaining a codebase. Gemini has 1M token context and can read entire repos.
Use run_command for simple shell operations. Use code for anything requiring code intelligence.

IMPORTANT — two distinct environments:
- sandbox container (/workspace): run_command, read_file, write_file, code, take_screenshot all operate HERE
- VPS host: use self_update ONLY for updating the bot itself (git pull + service restart)
Never use run_command to try to update the bot or restart the service — that runs inside the container, not the host.
Explain what you're doing as you work.\
"""


class Agent:
    def __init__(self, settings: Settings, sandbox: SandboxManager):
        self.settings = settings
        self.sandbox = sandbox
        self.max_turns = settings.max_agent_turns
        # Per-chat message history (in-memory, lost on restart)
        self._histories: dict[str, list[dict]] = {}

    def _get_history(self, chat_id: str) -> list[dict]:
        if chat_id not in self._histories:
            self._histories[chat_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
        return self._histories[chat_id]

    async def handle_message(self, chat_id: str, user_text: str):
        """Process a user message. Yields (text, image_bytes|None) tuples."""
        messages = self._get_history(chat_id)
        messages.append({"role": "user", "content": user_text})

        for turn in range(self.max_turns):
            response = await litellm.acompletion(
                model=self.settings.llm_model,
                messages=messages,
                tools=TOOL_SCHEMAS,
                api_key=self.settings.llm_api_key,
            )

            choice = response.choices[0]
            msg = choice.message
            messages.append(msg.model_dump(exclude_none=True))

            # If no tool calls, we have a final text response
            if not msg.tool_calls:
                if msg.content:
                    yield msg.content, None
                return

            # Execute each tool call
            for tc in msg.tool_calls:
                log.info("Tool call: %s(%s)", tc.function.name, tc.function.arguments[:100])
                text_result, image = await execute_tool(
                    self.sandbox, chat_id, tc.function.name, tc.function.arguments,
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": text_result,
                })
                if image:
                    yield None, image

        yield "Reached maximum turns. Here's where I got to — let me know if you'd like me to continue.", None
