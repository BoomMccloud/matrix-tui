# Implementation Guide: GitHub Issue-Driven Task Pipeline

This guide walks through each step to implement the spec in `docs/github-issues-spec.md`. Complete the steps in order — each builds on the previous one. Run `uv run ruff check src tests` and `uv run pytest tests/ -v` after each step to catch regressions early.

---

## Step 1: Add config fields

**File:** `src/matrix_agent/config.py`

Add two fields to the `Settings` class, after the existing `github_token` field:

```python
github_webhook_port: int = 8090
github_webhook_secret: str = ""
```

These are already referenced by `channels.py` and `core.py` but don't exist in `Settings` yet, so those files will currently crash at runtime.

**Verify:** `uv run python -c "from matrix_agent.config import Settings"` should not error.

---

## Step 2: Add `system_prompt` parameter to `Decider.handle_message()`

**File:** `src/matrix_agent/decider.py`

### 2a: Add `GITHUB_SYSTEM_PROMPT`

Add this constant after the existing `SYSTEM_PROMPT`:

```python
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
```

### 2b: Add `system_prompt` parameter to `handle_message()`

Current signature:

```python
async def handle_message(self, chat_id, user_text, send_update=None):
```

Change to:

```python
async def handle_message(self, chat_id, user_text, send_update=None, system_prompt=None):
```

### 2c: Update `_get_history()` to accept the prompt

Current `_get_history` always uses the module-level `SYSTEM_PROMPT`:

```python
def _get_history(self, chat_id: str) -> list[dict]:
    if chat_id not in self._histories:
        self._histories[chat_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    return self._histories[chat_id]
```

Change to:

```python
def _get_history(self, chat_id: str, system_prompt: str | None = None) -> list[dict]:
    if chat_id not in self._histories:
        prompt = system_prompt or SYSTEM_PROMPT
        self._histories[chat_id] = [{"role": "system", "content": prompt}]
    return self._histories[chat_id]
```

Then update the call site inside `handle_message()` from:

```python
history = self._get_history(chat_id)
```

to:

```python
history = self._get_history(chat_id, system_prompt=system_prompt)
```

**Why this is safe:** The `system_prompt` is only used on first call for a `chat_id` (when the history is created). Subsequent calls for the same `chat_id` get the existing history — the parameter is ignored. This means `bot.py` can pass `None` (default) and get the existing `SYSTEM_PROMPT` behavior unchanged.

**Verify:** `uv run pytest tests/test_multi_agent.py -v` should still pass (it calls `handle_message` without `system_prompt`, which defaults to `None`).

---

## Step 3: Add `create_pull_request` tool

**File:** `src/matrix_agent/tools.py`

### 3a: Add the tool schema

Add to the `TOOL_SCHEMAS` list:

```python
{
    "type": "function",
    "function": {
        "name": "create_pull_request",
        "description": "Create a git branch, commit all changes, push, and open a GitHub pull request. Returns the PR URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "PR title"
                },
                "body": {
                    "type": "string",
                    "description": "PR body (reference the issue, e.g. 'Closes #42')"
                }
            },
            "required": ["title", "body"]
        }
    }
},
```

### 3b: Add the execution logic

Add a handler in `execute_tool()`. Inside the `if/elif` chain, add before the final `else`:

```python
elif name == "create_pull_request":
    title = arguments["title"]
    body = arguments["body"]
    result = await _create_pull_request(sandbox, chat_id, title, body)
    return result, None
```

### 3c: Implement the helper function

Add this function (anywhere in `tools.py`, before `execute_tool` is fine):

```python
async def _create_pull_request(sandbox, chat_id, title, body):
    """Branch, commit, push, and open a PR. Returns the PR URL or error."""
    import re

    # Derive a branch name from the PR title
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50]
    branch = f"agent/{slug}"

    commands = [
        f"git checkout -b {branch}",
        "git add -A",
        f"git commit -m {_shell_quote(title)}",
        f"git push -u origin {branch}",
        f"gh pr create --title {_shell_quote(title)} --body {_shell_quote(body)}",
    ]

    for cmd in commands:
        rc, stdout, stderr = await sandbox.exec(chat_id, cmd)
        if rc != 0:
            return f"Failed at `{cmd}`:\n{stderr or stdout}"

    # The last command's stdout contains the PR URL
    return stdout.strip()


def _shell_quote(s):
    """Single-quote a string for shell safety."""
    return "'" + s.replace("'", "'\\''") + "'"
```

**Key points:**
- Each git command runs sequentially — if any fails, we return the error immediately.
- The branch name is derived deterministically from the title.
- `_shell_quote` prevents shell injection (all commands run via `sandbox.exec` which uses `sh -c`).
- `gh pr create` returns the PR URL on stdout.

**Verify:** `uv run ruff check src/matrix_agent/tools.py`

---

## Step 4: Expand `ChannelAdapter` ABC and update `GitHubChannel`

**File:** `src/matrix_agent/channels.py`

### 4a: Add missing abstract methods to `ChannelAdapter`

The current `ChannelAdapter` has `start`, `stop`, `deliver_result`, `deliver_error`. Add `send_update`, `is_valid`, and `system_prompt`:

```python
from abc import ABC, abstractmethod

class ChannelAdapter(ABC):
    system_prompt: str = ""  # subclasses override

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send_update(self, task_id: str, text: str) -> None: ...

    @abstractmethod
    async def deliver_result(self, task_id: str, text: str) -> None: ...

    @abstractmethod
    async def deliver_error(self, task_id: str, error: str) -> None: ...

    @abstractmethod
    async def is_valid(self, task_id: str) -> bool: ...
```

### 4b: Update `GitHubChannel` to implement all methods

Add the import at the top:

```python
from .decider import GITHUB_SYSTEM_PROMPT
```

Then in the `GitHubChannel` class:

```python
class GitHubChannel(ChannelAdapter):
    system_prompt = GITHUB_SYSTEM_PROMPT

    # ... existing __init__, _make_app, start, stop ...

    async def send_update(self, task_id: str, text: str) -> None:
        # No-op for GitHub — avoid spamming issues with intermediate output
        pass

    async def is_valid(self, task_id: str) -> bool:
        """Check if the issue is still open with the agent-task label."""
        # Extract issue number from task_id format "gh-42"
        issue_number = task_id.split("-", 1)[1]
        proc = await asyncio.create_subprocess_exec(
            "gh", "issue", "view", issue_number, "--json", "state,labels",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return False
        import json
        data = json.loads(stdout)
        if data.get("state") != "OPEN":
            return False
        labels = [l["name"] for l in data.get("labels", [])]
        return "agent-task" in labels
```

### 4c: Implement `deliver_result` and `deliver_error`

Replace the existing stubs:

```python
async def deliver_result(self, task_id: str, result: str) -> None:
    issue_number = task_id.split("-", 1)[1]
    body = f"✅ Completed — {result}"
    await asyncio.create_subprocess_exec(
        "gh", "issue", "comment", issue_number, "--body", body,
    )

async def deliver_error(self, task_id: str, error: str) -> None:
    issue_number = task_id.split("-", 1)[1]
    body = f"❌ Failed: {error}"
    await asyncio.create_subprocess_exec(
        "gh", "issue", "comment", issue_number, "--body", body,
    )
```

### 4d: Add `issue_comment` webhook handling

In `_handle_webhook`, the current code only handles `issues`/`labeled`. Add handling for `issue_comment`/`created`:

```python
async def _handle_webhook(self, request):
    # ... existing secret check ...

    payload = await request.json()
    event_type = request.headers.get("X-GitHub-Event", "")
    action = payload.get("action", "")

    if event_type == "issues" and action == "labeled":
        # ... existing label handling (build Task, call submit_task) ...
        pass

    elif event_type == "issue_comment" and action == "created":
        issue = payload["issue"]
        labels = [l["name"] for l in issue.get("labels", [])]
        if "agent-task" not in labels:
            return web.Response(text="not an agent-task issue")
        task_id = f"gh-{issue['number']}"
        comment_body = payload["comment"]["body"]
        # self.submit_task will be replaced with self.enqueue in Step 5
        # For now, just note this needs to enqueue the comment
        pass

    return web.Response(text="ok")
```

**Note:** The `submit_task` callback will be replaced with `TaskRunner.enqueue()` in Step 5. For now, get the routing logic right.

### 4e: Add "Working" comment on task pickup

In the `issues`/`labeled` handler, post the initial status comment:

```python
issue_number = issue["number"]
await asyncio.create_subprocess_exec(
    "gh", "issue", "comment", str(issue_number),
    "--body", "🤖 Working on this issue...",
)
```

**Verify:** `uv run ruff check src/matrix_agent/channels.py`

---

## Step 5: Refactor `AgentCore` → `TaskRunner`

**File:** `src/matrix_agent/core.py`

This is the biggest change. The `TaskRunner` takes over queue/worker ownership from `bot.py`.

### 5a: Rename and restructure

Replace the class. Here is the full target shape:

```python
import asyncio
import logging
from .sandbox import SandboxManager, _container_name
from .decider import Decider
from .channels import ChannelAdapter

logger = logging.getLogger(__name__)


class TaskRunner:
    def __init__(self, decider: Decider, sandbox: SandboxManager):
        self.decider = decider
        self.sandbox = sandbox
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._channels: dict[str, ChannelAdapter] = {}  # task_id -> channel
        self._processing: set[str] = set()

    async def enqueue(self, task_id: str, message: str, channel: ChannelAdapter) -> None:
        """Add a message for a task. Creates the worker on first call."""
        if task_id not in self._queues:
            self._queues[task_id] = asyncio.Queue()
            self._channels[task_id] = channel
            self._processing.add(task_id)
            self._workers[task_id] = asyncio.create_task(
                self._worker(task_id)
            )
        await self._queues[task_id].put(message)

    async def _worker(self, task_id: str) -> None:
        """Process messages sequentially for a single task."""
        channel = self._channels[task_id]
        queue = self._queues[task_id]
        try:
            while True:
                message = await queue.get()
                try:
                    await self._process(task_id, message, channel)
                except Exception:
                    logger.exception("Error processing %s", task_id)
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            pass

    async def _process(self, task_id: str, message: str, channel: ChannelAdapter) -> None:
        """Run the decider loop for one message."""
        # Ensure container exists
        if task_id not in self.sandbox._containers:
            await self.sandbox.create(task_id)

        # Define send_update callback for streaming
        async def send_update(chunk: str) -> None:
            await channel.send_update(task_id, chunk)

        # Run decider
        try:
            final_text = None
            async for text, image in self.decider.handle_message(
                task_id, message,
                send_update=send_update,
                system_prompt=channel.system_prompt,
            ):
                if text:
                    final_text = text
            if final_text:
                await channel.deliver_result(task_id, final_text)
        except Exception as e:
            await channel.deliver_error(task_id, str(e))
            raise

    async def reconcile(self) -> None:
        """Destroy containers for tasks that are no longer valid."""
        for task_id in list(self._channels):
            channel = self._channels[task_id]
            if not await channel.is_valid(task_id):
                logger.info("Reconcile: cleaning up %s", task_id)
                await self._cleanup(task_id)

    async def _cleanup(self, task_id: str) -> None:
        """Cancel worker, destroy container, remove all tracking state."""
        if task_id in self._workers:
            self._workers[task_id].cancel()
            del self._workers[task_id]
        self._queues.pop(task_id, None)
        self._channels.pop(task_id, None)
        self._processing.discard(task_id)
        if task_id in self.sandbox._containers:
            await self.sandbox.destroy(task_id)

    async def reconcile_loop(self) -> None:
        """Run reconcile every 60s."""
        while True:
            await asyncio.sleep(60)
            try:
                await self.reconcile()
            except Exception:
                logger.exception("Reconcile error")

    async def destroy_orphans(self) -> None:
        """On startup, destroy containers with no active worker (crash recovery)."""
        for chat_id in list(self.sandbox._containers):
            if chat_id not in self._processing:
                logger.info("Destroying orphan container: %s", chat_id)
                await self.sandbox.destroy(chat_id)
```

### What changed vs the old `AgentCore`:
- **Renamed** `AgentCore` → `TaskRunner`
- **Removed** `submit()` (replaced by `enqueue()`)
- **Added** `_queues`, `_workers`, `_channels`, `_processing` — these used to live on `Bot`
- **Added** `_worker()` loop — moved from `Bot._room_worker()`
- **Added** `reconcile()` and `reconcile_loop()` — moved from `Bot._reconcile_loop()`
- **Added** `destroy_orphans()` — startup cleanup
- **`_process()`** calls `decider.handle_message()` with the channel's `system_prompt` and delivers results via the channel adapter (not callbacks)

---

## Step 6: Refactor `Bot` to use `TaskRunner`

**File:** `src/matrix_agent/bot.py`

This is the second biggest change. `Bot` stops managing queues/workers and delegates to `TaskRunner`.

### 6a: Change `__init__` to accept `TaskRunner`

Current:

```python
def __init__(self, settings, sandbox, decider):
    self.settings = settings
    self.sandbox = sandbox
    self.decider = decider
    self.client = AsyncClient(settings.matrix_homeserver, settings.matrix_user)
    self._synced = False
    self._queues: dict[str, asyncio.Queue] = {}
    self._workers: dict[str, asyncio.Task] = {}
```

Change to:

```python
def __init__(self, settings, sandbox, decider, task_runner):
    self.settings = settings
    self.sandbox = sandbox
    self.decider = decider
    self.task_runner = task_runner
    self.client = AsyncClient(settings.matrix_homeserver, settings.matrix_user)
    self._synced = False
```

**Delete** `self._queues` and `self._workers` — `TaskRunner` owns these now.

### 6b: Simplify `_on_message`

Current `_on_message` creates queues, spawns workers, and enqueues. Replace the queue/worker logic with a single call:

```python
async def _on_message(self, room, event):
    if event.sender == self.client.user_id:
        return
    if not self._synced:
        return

    # Create a MatrixChannel adapter for this room (see 6d)
    channel = MatrixChannel(self, room.room_id)
    await self.task_runner.enqueue(room.room_id, event.body, channel)
```

### 6c: Delete methods that moved to `TaskRunner`

Remove these methods from `Bot`:
- `_room_worker()` — now `TaskRunner._worker()`
- `_process_message()` — now `TaskRunner._process()`
- `_reconcile_loop()` — now `TaskRunner.reconcile_loop()`
- `_cancel_worker()` — now `TaskRunner._cleanup()`

**Keep** these methods on `Bot` (they are Matrix-specific):
- `_on_invite()`, `_on_member()`, `_keep_typing()`, `_watch_ipc()`, `_send_image()`
- `_format_notification()`, `_format_progress()`, `_format_result()`
- `run()`

### 6d: Create `MatrixChannel` adapter

Add this class in `bot.py` (or as an inner class — keep it close to where it's used):

```python
from .channels import ChannelAdapter
from .decider import SYSTEM_PROMPT

class MatrixChannel(ChannelAdapter):
    system_prompt = SYSTEM_PROMPT

    def __init__(self, bot: "Bot", room_id: str):
        self.bot = bot
        self.room_id = room_id

    async def start(self) -> None:
        pass  # Matrix client lifecycle is managed by Bot.run()

    async def stop(self) -> None:
        pass

    async def send_update(self, task_id: str, text: str) -> None:
        """Send intermediate output as a Matrix message."""
        content = {"msgtype": "m.text", "body": f"```\n{text}\n```"}
        await self.bot.client.room_send(
            self.room_id, "m.room.message", content
        )

    async def deliver_result(self, task_id: str, text: str) -> None:
        content = {"msgtype": "m.text", "body": text}
        await self.bot.client.room_send(
            self.room_id, "m.room.message", content
        )

    async def deliver_error(self, task_id: str, error: str) -> None:
        content = {"msgtype": "m.text", "body": f"Error: {error}"}
        await self.bot.client.room_send(
            self.room_id, "m.room.message", content
        )

    async def is_valid(self, task_id: str) -> bool:
        return task_id in self.bot.client.rooms
```

### 6e: Update `run()` shutdown

In `Bot.run()`, the shutdown section currently does:

```python
await self.sandbox.destroy_all()
```

**Remove that line.** Containers persist across restarts. The reconcile loop (now on `TaskRunner`) handles cleanup.

Also update the reconcile task start. Change:

```python
reconcile_task = asyncio.create_task(self._reconcile_loop())
```

to:

```python
reconcile_task = asyncio.create_task(self.task_runner.reconcile_loop())
```

### 6f: Update `_on_member` to use `TaskRunner._cleanup()`

Where `_on_member` currently calls `self._cancel_worker(room_id)` and `self.sandbox.destroy(room_id)`, replace with:

```python
await self.task_runner._cleanup(room.room_id)
```

**Verify:** `uv run ruff check src/matrix_agent/bot.py`

---

## Step 7: Wire everything in `__main__.py`

**File:** `src/matrix_agent/__main__.py`

Current:

```python
async def main():
    settings = Settings()
    sandbox = SandboxManager(settings)
    decider = Decider(settings, sandbox)
    bot = Bot(settings, sandbox, decider)
    await bot.run()
```

Change to:

```python
import asyncio
from .config import Settings
from .sandbox import SandboxManager
from .decider import Decider
from .core import TaskRunner
from .bot import Bot
from .channels import GitHubChannel


async def main():
    settings = Settings()
    sandbox = SandboxManager(settings)
    decider = Decider(settings, sandbox)
    task_runner = TaskRunner(decider, sandbox)

    # Destroy orphan containers from any previous crash
    await sandbox.load_state()
    await task_runner.destroy_orphans()

    bot = Bot(settings, sandbox, decider, task_runner)

    # Only start GitHub channel if a token is configured
    github_channel = None
    if settings.github_token:
        github_channel = GitHubChannel(
            submit_task=...,  # see note below
            settings=settings,
        )
        await github_channel.start()

    try:
        await bot.run()
    finally:
        if github_channel:
            await github_channel.stop()
```

### Important: Wiring `GitHubChannel` to `TaskRunner`

The current `GitHubChannel.__init__` takes a `submit_task` callback. This needs to change to use `task_runner.enqueue()` instead. There are two approaches:

**Option A (simpler):** Pass `task_runner` directly to `GitHubChannel`:

```python
# In GitHubChannel.__init__:
def __init__(self, task_runner, settings):
    self.task_runner = task_runner
    # ...

# In _handle_webhook, when a labeled issue arrives:
task_id = f"gh-{issue['number']}"
message = f"# {issue['title']}\n\n{issue.get('body', '')}"
await self.task_runner.enqueue(task_id, message, self)
```

**Option B:** Keep the callback pattern. Either works. Option A is more explicit.

**Go with Option A.** Update `GitHubChannel.__init__` to take `task_runner` instead of `submit_task`. Remove the `Task` dataclass (no longer needed — `enqueue` takes `task_id` + `message` + `channel`).

Then in `__main__.py`:

```python
github_channel = GitHubChannel(task_runner=task_runner, settings=settings)
```

**Verify:** `uv run python -c "from matrix_agent.__main__ import main"` should import without errors.

---

## Step 8: Update `GitHubChannel` webhook to use `enqueue`

**File:** `src/matrix_agent/channels.py`

Now that `GitHubChannel` has a `task_runner` reference, update `_handle_webhook`:

```python
async def _handle_webhook(self, request):
    # Secret check
    expected = self.settings.github_webhook_secret
    if expected and request.query.get("secret") != expected:
        return web.Response(status=403)

    payload = await request.json()
    event_type = request.headers.get("X-GitHub-Event", "")
    action = payload.get("action", "")

    if event_type == "issues" and action == "labeled":
        label = payload.get("label", {}).get("name", "")
        if label != "agent-task":
            return web.Response(text="ignored label")

        issue = payload["issue"]
        task_id = f"gh-{issue['number']}"

        # Idempotency: skip if already processing
        if task_id in self.task_runner._processing:
            return web.Response(text="already processing")

        # Post "Working" comment
        await asyncio.create_subprocess_exec(
            "gh", "issue", "comment", str(issue["number"]),
            "--body", "🤖 Working on this issue...",
        )

        # Enqueue title+body as first message
        message = f"# {issue['title']}\n\n{issue.get('body', '')}"
        await self.task_runner.enqueue(task_id, message, self)

        # Backfill existing comments
        # (use gh api to fetch, enqueue each one)
        proc = await asyncio.create_subprocess_exec(
            "gh", "api", f"repos/{self._repo(payload)}/issues/{issue['number']}/comments",
            "--jq", ".[].body",
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout:
            for comment in stdout.decode().strip().split("\n"):
                if comment.strip():
                    await self.task_runner.enqueue(task_id, comment.strip(), self)

    elif event_type == "issue_comment" and action == "created":
        issue = payload["issue"]
        labels = [l["name"] for l in issue.get("labels", [])]
        if "agent-task" not in labels:
            return web.Response(text="not an agent-task issue")

        task_id = f"gh-{issue['number']}"
        comment_body = payload["comment"]["body"]
        await self.task_runner.enqueue(task_id, comment_body, self)

    return web.Response(text="ok")


def _repo(self, payload):
    """Extract 'owner/repo' from webhook payload."""
    repo = payload.get("repository", {})
    return repo.get("full_name", "")
```

---

## Step 9: Update tests

### 9a: Fix `test_integration.py`

This test imports `AgentCore` from `core.py`. Update imports:

```python
# Old
from matrix_agent.core import AgentCore

# New
from matrix_agent.core import TaskRunner
```

The integration tests use `core.submit()` which no longer exists. You have two options:

1. **Rewrite tests to use `enqueue()`** — create a mock `ChannelAdapter` and call `task_runner.enqueue(task_id, message, channel)`.
2. **Add a thin helper for tests** — not recommended, keep tests honest.

Go with option 1. Example:

```python
from matrix_agent.channels import ChannelAdapter

class MockChannel(ChannelAdapter):
    system_prompt = "You are a test agent."

    def __init__(self):
        self.results = []
        self.errors = []

    async def start(self): pass
    async def stop(self): pass
    async def send_update(self, task_id, text): pass
    async def deliver_result(self, task_id, text):
        self.results.append(text)
    async def deliver_error(self, task_id, error):
        self.errors.append(error)
    async def is_valid(self, task_id):
        return True
```

### 9b: Fix `test_multi_agent.py`

This test creates a `Decider` directly and calls `handle_message()`. Since we only added an optional `system_prompt` parameter, **this test should still pass unchanged**. Verify.

### 9c: Add new tests for the spec verification list

Add a new test file `tests/test_github_channel.py`:

```python
"""Tests for GitHub issue-driven pipeline (spec verification tests 1-9)."""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from matrix_agent.channels import GitHubChannel, ChannelAdapter
from matrix_agent.core import TaskRunner


# Test 5: Re-label in-progress issue → skipped (idempotency)
@pytest.mark.asyncio
async def test_idempotency_skip_duplicate():
    task_runner = MagicMock(spec=TaskRunner)
    task_runner._processing = {"gh-42"}
    task_runner.enqueue = AsyncMock()

    channel = GitHubChannel(task_runner=task_runner, settings=MagicMock())
    # Simulate webhook for already-processing issue
    # task_runner.enqueue should NOT be called
    # (test the actual webhook handler with a mock request)


# Test 9: Webhook with wrong/missing secret → 403
@pytest.mark.asyncio
async def test_webhook_wrong_secret_returns_403():
    # Create GitHubChannel with a secret configured
    # Send a request without the secret
    # Assert 403 response
    pass
```

Fill in the test bodies following the patterns from the existing test files.

---

## Step 10: Final verification

Run the full test suite and linter:

```bash
uv run ruff check src tests
uv run pytest tests/ -v
```

Walk through the spec's verification checklist:

| # | Test | How to verify |
|---|---|---|
| 1 | Label an issue → "Working" comment | Webhook POST with `issues`/`labeled` payload |
| 2 | Agent completes → PR created | Mock decider returns text, check `deliver_result` called |
| 3 | Agent fails → "Failed" comment | Mock decider raises, check `deliver_error` called |
| 4 | Comment while working → queued | Enqueue two messages, verify sequential processing |
| 5 | Re-label in-progress → skipped | `task_id` in `_processing`, verify `enqueue` not called twice |
| 6 | Close issue → reconcile cleanup | `is_valid` returns False, verify `_cleanup` called |
| 7 | Bot restart → orphans destroyed | Call `destroy_orphans()`, verify containers destroyed |
| 8 | Reconcile cleans orphans | Same as 6 |
| 9 | Wrong secret → 403 | HTTP request without `?secret=`, verify 403 |

---

## Summary: File change order

1. `config.py` — add 2 fields (smallest change, unblocks everything)
2. `decider.py` — add `GITHUB_SYSTEM_PROMPT`, add `system_prompt` param
3. `tools.py` — add `create_pull_request` tool
4. `channels.py` — expand `ChannelAdapter`, flesh out `GitHubChannel`
5. `core.py` — rewrite `AgentCore` → `TaskRunner` (biggest change)
6. `bot.py` — refactor to use `TaskRunner`, add `MatrixChannel`
7. `__main__.py` — wire everything together
8. Tests — update imports, add new test file

Each step can be committed independently. Run lint + tests after each.
