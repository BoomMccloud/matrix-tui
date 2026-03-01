# Crash Recovery: Channel-Aware Task Lifecycle

## Context

After a crash/restart, all in-memory state (`_queues`, `_workers`, `_channels`, `_processing`) is lost. `destroy_orphans()` then destroys all surviving containers because `_processing` is empty. GitHub issues with `agent-task` are abandoned. Matrix rooms get no notification. The fix: let each channel recover its pending tasks before orphan cleanup runs.

## Design

Add `recover_tasks()` to `ChannelAdapter`. Each channel scans for work it should resume. The startup sequence becomes:

```
load_state → load_histories → GitHub recover_tasks → start GitHub channel → bot.setup() → Matrix recovery → destroy_orphans → bot.run()
```

Containers are only destroyed if **no channel claimed them**.

## Changes Required

### 1. `channels.py` — Add `recover_tasks()` to ABC + implement for GitHub

Add default no-op to `ChannelAdapter`:
```python
async def recover_tasks(self) -> list[tuple[str, str]]:
    """Return (task_id, message) pairs to re-enqueue after restart."""
    return []
```

Implement in `GitHubChannel`:
- Run `gh issue list --repo {settings.github_repo} --label agent-task --state open --json number,title,body`
- Return `[(f"gh-{n}", f"# {title}\n\n{body}")]` for each issue
- Post "Bot restarted — resuming work on this issue." comment on each

### 1b. `config.py` — Add `github_repo` setting

```python
github_repo: str = ""  # e.g. "owner/repo" — required for crash recovery of GitHub tasks
```

`recover_tasks()` needs to know which repo to query. Webhook payloads provide this at runtime, but recovery has no payload. Also update `.env.example`.

### 2. `core.py` — Add `pre_register()` to TaskRunner

```python
async def pre_register(self, task_id: str, channel: ChannelAdapter) -> None:
    """Register a task so destroy_orphans() preserves its container.

    Creates queue + worker + adds to _processing without enqueuing a message.
    The worker idles on queue.get() until a message arrives or reconcile()
    cleans it up via is_valid().
    """
```

Used for Matrix recovery where we want the container to survive but wait for the user to respond.

No changes to `destroy_orphans()` logic — the ordering fix makes it correct.

### 3. `bot.py` — Split `run()` into `setup()` + `run()`, add Matrix recovery

**`setup()`**: login → register callbacks → initial sync → catch-up joins → `_recover_matrix_rooms()` → set `_synced = True`

**`run()`**: start reconcile loop → `sync_forever` → cleanup on exit

**`_recover_matrix_rooms()`**: For each room_id in `sandbox._containers` that's still in `client.rooms`:
- Call `task_runner.pre_register(room_id, MatrixChannel(self, room_id))`
- Send message: "I restarted. Your workspace and conversation history are intact. Send a message to continue."

### 4. `__main__.py` — Reorder startup

```python
histories = await sandbox.load_state()
decider.load_histories(histories)

github_channel = GitHubChannel(...) if settings.github_token else None
if github_channel:
    # Recover before start() to avoid race: a webhook arriving between
    # start() and recover_tasks() would cause a duplicate enqueue.
    recovered = await github_channel.recover_tasks()
    await github_channel.start()
    for task_id, msg in recovered:
        await task_runner.enqueue(task_id, msg, github_channel)

bot = Bot(settings, sandbox, decider, task_runner)
await bot.setup()              # Matrix sync + Matrix recovery (pre_register)
await task_runner.destroy_orphans()  # now _processing has all recovered tasks
await bot.run()                # sync_forever
```

### 5. Tests

- `test_core.py`: Add test for `pre_register()` — task_id in `_processing` but queue empty
- `test_core.py`: Update `test_destroy_orphans` — verify recovered tasks survive
- `test_channels.py`: Add test for `GitHubChannel.recover_tasks()` with mocked `gh` CLI
- `test_core.py`: Test that `destroy_orphans` preserves pre-registered tasks

## Edge Cases

| Scenario | Behavior |
|---|---|
| Issue closed while bot was down | `recover_tasks` uses `--state open`, skips it. Container destroyed by `destroy_orphans`. |
| Bot kicked from Matrix room while down | `_recover_matrix_rooms` checks `client.rooms`, skips it. Container destroyed. |
| Webhook arrives for already-recovered issue | Cannot happen — `recover_tasks()` runs before `start()` opens the webhook port. After startup, the `_processing` guard in `_handle_webhook` prevents duplicates. |
| Container died during downtime | `load_state()` only keeps running containers. No container = no recovery needed. |

## Verification

```bash
uv run ruff check src tests
uv run pytest tests/ -v

# Manual: restart bot while a GitHub issue is in-progress → bot posts "resuming" comment
# Manual: restart bot while Matrix room has active task → bot posts "I restarted" message
```
