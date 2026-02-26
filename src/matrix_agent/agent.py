"""LLM agent with tool-calling loop."""

import logging
import time

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
After Gemini writes or modifies code, always call run_tests to verify lint and tests pass before reporting success.

After cloning a repo, always run: code(task="run /init to generate GEMINI.md for this repo", ...)
This lets Gemini analyze the codebase and write its own project context file.

IMPORTANT — two distinct environments:
- sandbox container (/workspace): run_command, read_file, write_file, code, take_screenshot all operate HERE
- VPS host: use self_update ONLY for updating the bot itself (runs deploy.sh: git pull + rebuild sandbox image + service restart)
Never use run_command to try to update the bot or restart the service — that runs inside the container, not the host.
Explain what you're doing as you work.\
"""


class Agent:
    def __init__(self, settings: Settings, sandbox: SandboxManager):
        self.settings = settings
        self.sandbox = sandbox
        self.max_turns = settings.max_agent_turns
        self._histories: dict[str, list[dict]] = {}
        # Give sandbox a reference so it can persist histories with state
        self.sandbox._histories = self._histories

    def load_histories(self, histories: dict[str, list[dict]]) -> None:
        """Populate in-memory histories from persisted state."""
        self._histories.update(histories)

    def _get_history(self, chat_id: str) -> list[dict]:
        if chat_id not in self._histories:
            self._histories[chat_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
        return self._histories[chat_id]

    async def handle_message(self, chat_id: str, user_text: str, send_update=None):
        """Process a user message. Yields (text, image_bytes|None) tuples."""
        messages = self._get_history(chat_id)
        messages.append({"role": "user", "content": user_text})
        log.info("[%s] User message: %s", chat_id[:20], user_text[:200])

        for turn in range(self.max_turns):
            log.info("[%s] Turn %d/%d — calling LLM (%s)", chat_id[:20], turn + 1, self.max_turns, self.settings.llm_model)
            t0 = time.monotonic()
            kwargs = dict(
                model=self.settings.llm_model,
                messages=messages,
                tools=TOOL_SCHEMAS,
                api_key=self.settings.llm_api_key,
            )
            if self.settings.llm_api_base:
                kwargs["api_base"] = self.settings.llm_api_base
            response = await litellm.acompletion(**kwargs)
            llm_elapsed = time.monotonic() - t0
            log.info("[%s] LLM responded in %.1fs", chat_id[:20], llm_elapsed)

            choice = response.choices[0]
            msg = choice.message
            # Build a clean assistant message dict for history.
            # Some providers (e.g. MiniMax) are strict about the format.
            assistant_msg: dict = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            messages.append(assistant_msg)

            # If no tool calls, we have a final text response
            if not msg.tool_calls:
                log.info("[%s] Final response on turn %d: %s", chat_id[:20], turn + 1, (msg.content or "")[:200])
                if msg.content:
                    self.sandbox.save_state()
                    yield msg.content, None
                return

            # Execute each tool call
            for tc in msg.tool_calls:
                log.info("[%s] Tool call: %s(%s)", chat_id[:20], tc.function.name, tc.function.arguments[:200])
                t0 = time.monotonic()
                text_result, image = await execute_tool(
                    self.sandbox, chat_id, tc.function.name, tc.function.arguments,
                    send_update=send_update,
                )
                tool_elapsed = time.monotonic() - t0
                log.info("[%s] Tool %s completed in %.1fs (result: %d chars)", chat_id[:20], tc.function.name, tool_elapsed, len(text_result))
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": text_result,
                })
                if image:
                    yield None, image

        log.warning("[%s] Hit max turns (%d)", chat_id[:20], self.max_turns)
        self.sandbox.save_state()
        yield "Reached maximum turns. Here's where I got to — let me know if you'd like me to continue.", None
