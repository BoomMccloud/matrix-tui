"""LLM decider — routing loop that decides which tool to call next."""

import logging
import time

import litellm

from .config import Settings
from .sandbox import SandboxManager
from .tools import TOOL_SCHEMAS, execute_tool

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a coding assistant running inside a sandboxed container. You have three coding agents:

- plan(task) — Gemini CLI (1M token context). Use for planning, analysis, and explaining codebases.
- implement(task) — Qwen Code. Use for writing code, fixing bugs, and refactoring.
- review(task) — Gemini CLI. Use after implementation to review changes.

You also have:
- run_command — run shell commands in the sandbox
- read_file / write_file — read and write files in the sandbox
- run_tests — run lint (ruff) and tests (pytest)
- take_screenshot — take a browser screenshot of a URL in the sandbox
- self_update — update the bot itself on the VPS host

The container has Node.js 20, Python 3, git, Gemini CLI, and Qwen Code installed.
Work in /workspace. When you start a web server, use take_screenshot to show the result.

Typical workflow:
1. plan() — understand the codebase and design the approach
2. implement() — write the code, passing the plan as context
3. run_tests() — verify lint and tests pass
4. review() — check for bugs, security issues, missed edge cases
5. If review finds issues, implement() again with the feedback

Always pass enough context between agents. Each agent invocation is independent —
include the plan in the implement() task, and describe what changed in the review() task.
Use run_command for simple shell operations. Use plan/implement/review for anything requiring code intelligence.

After cloning a repo, always run: plan(task="run /init to generate GEMINI.md for this repo")
This lets Gemini analyze the codebase and write its own project context file.

IMPORTANT — two distinct environments:
- sandbox container (/workspace): run_command, read_file, write_file, plan, implement, review, take_screenshot all operate HERE
- VPS host: use self_update ONLY for updating the bot itself (runs deploy.sh: git pull + rebuild sandbox image + service restart)
Never use run_command to try to update the bot or restart the service — that runs inside the container, not the host.

When modifying the bot's own code:
1. run_command: git clone https://github.com/BoomMccloud/matrix-tui /workspace/matrix-tui
2. plan/implement/review: work on /workspace/matrix-tui
3. run_command: cd /workspace/matrix-tui && git checkout -b <branch> && git add -A && git commit -m "..."
4. run_command: cd /workspace/matrix-tui && git push origin <branch>
5. run_command: cd /workspace/matrix-tui && gh pr create --title "..." --body "..."
6. Tell the user the PR URL and wait for them to review/merge
7. After merge: self_update() to pull and restart
To test a branch before merging: self_update(branch="<branch>")

Explain what you're doing as you work.\
"""

GITHUB_SYSTEM_PROMPT = """You are an autonomous coding agent working on a GitHub issue.
Your goal is to understand the issue, implement the fix or feature, and create a pull request.

Workflow:
1. plan() — understand the codebase and design the approach
2. implement() — write the code
3. run_tests() — verify lint and tests pass
4. review() — check for bugs and edge cases
5. If review finds issues, implement() again

After completing and verifying code changes:
Do NOT manually run `git` or `gh` commands. Instead, call the `create_pull_request(title, body)` tool.
The tool will automatically handle branching, committing, pushing, and opening the PR.
Provide a clear PR title and a body that references the issue (e.g., "Closes #123").

Report the PR URL (returned by the tool) as your final message.
If you cannot complete the task, explain what's blocking you.
"""


class Decider:
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

    def _get_history(self, chat_id: str, system_prompt: str | None = None) -> list[dict]:
        if chat_id not in self._histories:
            prompt = system_prompt or SYSTEM_PROMPT
            self._histories[chat_id] = [{"role": "system", "content": prompt}]
        return self._histories[chat_id]

    async def handle_message(self, chat_id: str, user_text: str, send_update=None, system_prompt: str | None = None):
        """Process a user message. Yields (text, image_bytes|None) tuples."""
        messages = self._get_history(chat_id, system_prompt=system_prompt)
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
