# Implementation Guide: Crash Recovery

**Based on Spec**: docs/lifecycle-mgmt.md
**Verification Report**: docs/lifecycle-mgmt-verification-report.md
**Generated**: 2026-03-01

---

## Overview

After a crash/restart, all in-memory state is lost and `destroy_orphans()` kills all containers. This guide implements channel-aware crash recovery: each channel recovers its pending tasks before orphan cleanup, so containers survive restarts.

### Files You Will Modify

| File | Action | Summary |
|---|---|---|
| `src/matrix_agent/config.py` | Modify | Add `github_repo` setting |
| `src/matrix_agent/channels.py` | Modify | Add `recover_tasks()` to ABC + GitHub impl |
| `src/matrix_agent/core.py` | Modify | Add `pre_register()` method |
| `src/matrix_agent/bot.py` | Modify | Split `run()` into `setup()` + `run()`, add `_recover_matrix_rooms()` |
| `src/matrix_agent/__main__.py` | Modify | Reorder startup sequence |
| `tests/test_core.py` | Modify | Add tests for `pre_register` and updated `destroy_orphans` |
| `tests/test_channels.py` | Modify | Add test for `recover_tasks()` |

### Out of Scope — DO NOT MODIFY

- `sandbox.py` — no changes needed
- `decider.py` — no changes needed
- `tools.py` — no changes needed

---

## Prerequisites

```bash
# Verify tests pass before starting
uv run pytest tests/ -v
uv run ruff check src tests
```

---

## Step 1: Add `github_repo` to config.py

### Goal

`recover_tasks()` needs to know which repo to query via `gh issue list --repo`.

### File

`src/matrix_agent/config.py`

### Find This Location

Navigate to **line 34**. You should see:

```python
    github_token: str = ""
    github_webhook_port: int = 8090
    github_webhook_secret: str = ""
```

### Action: Add after line 34

```python
    github_repo: str = ""
```

So it reads:

```python
    github_token: str = ""
    github_repo: str = ""
    github_webhook_port: int = 8090
    github_webhook_secret: str = ""
```

### Verify This Step

```bash
uv run ruff check src/matrix_agent/config.py
```

---

## Step 2: Add `recover_tasks()` to ChannelAdapter ABC and GitHubChannel

### Goal

Add a default no-op `recover_tasks()` to the ABC, then implement it in `GitHubChannel` to scan for open `agent-task` issues.

### File

`src/matrix_agent/channels.py`

### 2a: Add default method to ChannelAdapter

Navigate to **line 35** (after `is_valid`). Add the following after the `is_valid` abstract method:

```python
    async def recover_tasks(self) -> list[tuple[str, str]]:
        """Return (task_id, message) pairs to re-enqueue after restart."""
        return []
```

Note: This is a **concrete** default method (not abstract), so existing subclasses don't need to implement it.

### 2b: Implement `recover_tasks()` in GitHubChannel

Navigate to `GitHubChannel`, after the `is_valid` method (currently line 97, after step 2a it will be ~line 101). Add the following method **before** `_handle_webhook`:

```python
    async def recover_tasks(self) -> list[tuple[str, str]]:
        """Scan for open agent-task issues to resume after restart."""
        repo = self.settings.github_repo
        if not repo:
            log.warning("github_repo not set — skipping crash recovery for GitHub tasks")
            return []

        proc = await asyncio.create_subprocess_exec(
            "gh", "issue", "list",
            "--repo", repo,
            "--label", "agent-task",
            "--state", "open",
            "--json", "number,title,body",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.error("gh issue list failed: %s", stderr.decode())
            return []

        issues = json.loads(stdout)
        results = []
        for issue in issues:
            number = issue["number"]
            task_id = f"gh-{number}"
            message = f"# {issue['title']}\n\n{issue.get('body', '')}"
            results.append((task_id, message))

            # Post recovery comment
            await asyncio.create_subprocess_exec(
                "gh", "issue", "comment", str(number),
                "--repo", repo,
                "--body", "🤖 Bot restarted — resuming work on this issue.",
            )

        log.info("GitHub recovery: found %d open agent-task issues", len(results))
        return results
```

### Common Mistakes

**Mistake: Forgetting `--repo` flag**
```python
# WRONG — relies on CWD being a git repo (fragile on VPS)
"gh", "issue", "list", "--label", "agent-task", ...

# CORRECT — explicit repo
"gh", "issue", "list", "--repo", repo, "--label", "agent-task", ...
```

### Verify This Step

```bash
uv run ruff check src/matrix_agent/channels.py
```

---

## Step 3: Add `pre_register()` to TaskRunner

### Goal

Allow Matrix recovery to register a task in `_processing` without enqueuing a message, so `destroy_orphans()` won't kill the container.

### File

`src/matrix_agent/core.py`

### Find This Location

Navigate to **line 21** (the `enqueue` method). Add the new method **before** `enqueue`:

```python
    async def pre_register(self, task_id: str, channel: ChannelAdapter) -> None:
        """Register a task so destroy_orphans() preserves its container.

        Creates queue + worker + adds to _processing without enqueuing a message.
        The worker idles on queue.get() until a message arrives or reconcile()
        cleans it up via is_valid().
        """
        if task_id in self._queues:
            return
        self._queues[task_id] = asyncio.Queue()
        self._channels[task_id] = channel
        self._processing.add(task_id)
        self._workers[task_id] = asyncio.create_task(
            self._worker(task_id)
        )
```

### Common Mistakes

**Mistake: Forgetting the idempotency guard**
```python
# WRONG — would overwrite existing queue/worker if called twice
async def pre_register(self, task_id, channel):
    self._queues[task_id] = asyncio.Queue()
    ...

# CORRECT — early return if already registered
async def pre_register(self, task_id, channel):
    if task_id in self._queues:
        return
    ...
```

### Verify This Step

```bash
uv run ruff check src/matrix_agent/core.py
```

---

## Step 4: Split `Bot.run()` into `setup()` + `run()`, add Matrix recovery

### Goal

Split the current `run()` so that Matrix sync + recovery happens in `setup()`, and `sync_forever` happens in `run()`. This lets `__main__.py` call `destroy_orphans()` between setup and run.

### File

`src/matrix_agent/bot.py`

### Find This Location

Navigate to **line 204** — the current `run()` method. Replace the **entire** `run()` method (lines 204–235) with three methods:

```python
    async def _recover_matrix_rooms(self) -> None:
        """Pre-register tasks for Matrix rooms that survived the restart."""
        for room_id in list(self.sandbox._containers):
            if room_id in self.client.rooms:
                log.info("Recovering Matrix room %s", room_id)
                channel = MatrixChannel(self, room_id)
                await self.task_runner.pre_register(room_id, channel)
                await self.client.room_send(
                    room_id, "m.room.message",
                    {"msgtype": "m.text", "body":
                     "I restarted. Your workspace and conversation history are intact. Send a message to continue."},
                )

    async def setup(self):
        """Login, sync, recover Matrix rooms. Call before destroy_orphans()."""
        await self._login()

        self.client.add_event_callback(self._on_invite, InviteMemberEvent)
        self.client.add_event_callback(self._on_message, RoomMessageText)
        self.client.add_event_callback(self._on_member, RoomMemberEvent)

        log.info("Starting initial sync...")
        resp = await self.client.sync(timeout=10000)
        log.info("Initial sync result: %s", type(resp).__name__)

        # Auto-join pending invites from before startup (no greeting — stale invites)
        for room_id in list(self.client.invited_rooms):
            log.info("catch-up join (no greeting) for %s", room_id)
            await self.client.join(room_id)

        await self._recover_matrix_rooms()

        # State + histories already loaded in __main__.py
        self._synced = True
        log.info("Initial sync complete, now listening")

    async def run(self):
        """Start sync_forever loop. Call after setup() and destroy_orphans()."""
        async def on_sync(response):
            log.info("Sync OK: next_batch=%s", response.next_batch)

        self.client.add_response_callback(on_sync, SyncResponse)

        reconcile_task = asyncio.create_task(self.task_runner.reconcile_loop())
        try:
            await self.client.sync_forever(timeout=30000)
        finally:
            reconcile_task.cancel()
            log.info("Shutting down")
            await self.client.close()
```

### Common Mistakes

**Mistake: Setting `_synced = True` before recovery**
```python
# WRONG — messages arriving during recovery would be processed before pre_register
self._synced = True
await self._recover_matrix_rooms()

# CORRECT — recover first, then enable message processing
await self._recover_matrix_rooms()
self._synced = True
```

**Mistake: Putting `_recover_matrix_rooms` in `run()` instead of `setup()`**

Recovery must happen before `destroy_orphans()`. Since `__main__.py` calls `setup() → destroy_orphans() → run()`, recovery must be in `setup()`.

### Verify This Step

```bash
uv run ruff check src/matrix_agent/bot.py
```

---

## Step 5: Reorder startup in `__main__.py`

### Goal

Move `destroy_orphans()` after all recovery, and add GitHub recovery before `bot.setup()`.

### File

`src/matrix_agent/__main__.py`

### Action: Replace lines 19–42 (the entire `main()` function body)

```python
async def main():
    settings = Settings()
    sandbox = SandboxManager(settings)
    decider = Decider(settings, sandbox)
    task_runner = TaskRunner(decider, sandbox)

    # Load persisted state and restore histories
    histories = await sandbox.load_state()
    decider.load_histories(histories)

    # GitHub recovery: scan for open issues before starting webhook server
    github_channel = None
    if settings.github_token:
        github_channel = GitHubChannel(task_runner=task_runner, settings=settings)
        recovered = await github_channel.recover_tasks()
        await github_channel.start()
        for task_id, msg in recovered:
            await task_runner.enqueue(task_id, msg, github_channel)

    # Matrix recovery: sync + pre_register surviving rooms
    bot = Bot(settings, sandbox, decider, task_runner)
    await bot.setup()

    # Now _processing contains all recovered tasks — safe to destroy orphans
    await task_runner.destroy_orphans()

    try:
        await bot.run()
    finally:
        if github_channel:
            await github_channel.stop()
```

### Common Mistakes

**Mistake: Calling `destroy_orphans()` before recovery (the original bug!)**
```python
# WRONG — this is the current code, it's the bug we're fixing
await task_runner.destroy_orphans()  # kills everything
bot = Bot(...)
await bot.run()

# CORRECT — recover first, then destroy orphans
await bot.setup()                    # recovery happens here
await task_runner.destroy_orphans()  # now _processing is populated
await bot.run()
```

**Mistake: Calling `github_channel.start()` before `recover_tasks()`**
```python
# WRONG — webhook port opens, race condition with recovery
await github_channel.start()
recovered = await github_channel.recover_tasks()

# CORRECT — recover before opening the port
recovered = await github_channel.recover_tasks()
await github_channel.start()
```

### Verify This Step

```bash
uv run ruff check src/matrix_agent/__main__.py
```

---

## Step 6: Add tests

### File

`tests/test_core.py`

### 6a: Add `test_pre_register` after `test_enqueue_creates_queue_and_worker` (line 84)

```python
@pytest.mark.asyncio
async def test_pre_register():
    """pre_register() adds task to _processing with empty queue."""
    sandbox = _make_sandbox()
    decider = _make_decider([])
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    await runner.pre_register("task-pr", channel)

    assert "task-pr" in runner._processing
    assert "task-pr" in runner._queues
    assert "task-pr" in runner._workers
    assert "task-pr" in runner._channels
    assert runner._queues["task-pr"].empty()

    await runner._cleanup("task-pr")


@pytest.mark.asyncio
async def test_pre_register_idempotent():
    """pre_register() is a no-op if task already registered."""
    sandbox = _make_sandbox()
    decider = _make_decider([])
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    await runner.pre_register("task-pr2", channel)
    original_queue = runner._queues["task-pr2"]

    await runner.pre_register("task-pr2", channel)
    assert runner._queues["task-pr2"] is original_queue  # same object

    await runner._cleanup("task-pr2")
```

### 6b: Add `test_destroy_orphans_preserves_pre_registered` after existing `test_destroy_orphans` (line 186)

```python
@pytest.mark.asyncio
async def test_destroy_orphans_preserves_pre_registered():
    """destroy_orphans() does not destroy containers for pre-registered tasks."""
    sandbox = _make_sandbox()
    sandbox._containers = {
        "recovered-1": "sandbox-recovered-1",
        "orphan-1": "sandbox-orphan-1",
    }
    decider = _make_decider([])
    runner = TaskRunner(decider, sandbox)
    channel = MockChannel()

    # Pre-register one task (simulates recovery)
    await runner.pre_register("recovered-1", channel)

    await runner.destroy_orphans()

    # Orphan destroyed, recovered preserved
    sandbox.destroy.assert_called_once_with("orphan-1")
    assert "recovered-1" in runner._processing

    await runner._cleanup("recovered-1")
```

### File

`tests/test_channels.py`

### 6c: Add `test_recover_tasks` at the end of the file (after line 215)

```python
@pytest.mark.asyncio
async def test_recover_tasks_returns_open_issues():
    """recover_tasks() returns (task_id, message) pairs for open agent-task issues."""
    from unittest.mock import patch

    task_runner = _make_task_runner()
    settings = SimpleNamespace(
        github_webhook_port=0,
        github_webhook_secret="",
        github_token="ghp_fake",
        github_repo="owner/repo",
    )
    channel = GitHubChannel(task_runner=task_runner, settings=settings)

    gh_output = json.dumps([
        {"number": 10, "title": "Fix bug", "body": "Details here"},
        {"number": 11, "title": "Add feature", "body": "More details"},
    ]).encode()

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(gh_output, b""))

    with patch("matrix_agent.channels.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        results = await channel.recover_tasks()

    assert len(results) == 2
    assert results[0] == ("gh-10", "# Fix bug\n\nDetails here")
    assert results[1] == ("gh-11", "# Add feature\n\nMore details")


@pytest.mark.asyncio
async def test_recover_tasks_skips_when_no_repo():
    """recover_tasks() returns empty list when github_repo is not set."""
    task_runner = _make_task_runner()
    settings = SimpleNamespace(
        github_webhook_port=0,
        github_webhook_secret="",
        github_token="ghp_fake",
        github_repo="",
    )
    channel = GitHubChannel(task_runner=task_runner, settings=settings)

    results = await channel.recover_tasks()
    assert results == []
```

### 6d: Update `test_channel_adapter_has_required_abstract_methods` (line 20–24)

The `recover_tasks()` method is **not abstract** (it has a default implementation), so this test should **not change**. No action needed here.

### Verify This Step

```bash
uv run pytest tests/test_core.py tests/test_channels.py -v
```

---

## Final Verification

### Run all tests

```bash
uv run pytest tests/ -v
```

All tests must pass.

### Run linter

```bash
uv run ruff check src tests
```

No errors should appear.

### Run the bot locally (smoke test)

```bash
uv run python -m matrix_agent
```

Check logs for:
- `GitHub recovery: found N open agent-task issues` (if GitHub configured)
- `Recovering Matrix room !xxx` (if rooms survived restart)
- `Destroying orphan container: xxx` (only for unclaimed containers)

---

## Pre-Submission Checklist

- [ ] `config.py`: `github_repo` field added
- [ ] `channels.py`: `recover_tasks()` default on ABC + GitHub implementation
- [ ] `core.py`: `pre_register()` method added
- [ ] `bot.py`: `run()` split into `setup()` + `run()`, `_recover_matrix_rooms()` added
- [ ] `__main__.py`: Startup reordered (recover → destroy_orphans → run)
- [ ] All tests pass: `uv run pytest tests/ -v`
- [ ] Linter passes: `uv run ruff check src tests`
