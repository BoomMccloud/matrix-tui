# GitHub Issue-Driven Task Pipeline

## Overview

GitHub issues labeled `agent-task` trigger autonomous coding tasks. Each issue gets its own container, decider conversation history, and message queue — same as Matrix rooms.

The task runner (`core.py`) is **channel-agnostic**. Channel adapters (Matrix `bot.py`, GitHub `channels.py`) subclass `ChannelAdapter` (ABC). The runner owns the lifecycle: container creation, decider loop, output delivery.

## Channel Protocol

```python
class ChannelAdapter(ABC):
    system_prompt: str

    async def send_update(self, task_id: str, text: str) -> None:
        """Stream intermediate output (Matrix) or no-op (GitHub)."""
        ...

    async def deliver_result(self, task_id: str, text: str) -> None:
        """Post final success message."""
        ...

    async def deliver_error(self, task_id: str, error: str) -> None:
        """Post final error message."""
        ...

    async def is_valid(self, task_id: str) -> bool:
        """Check if the task is still active (room exists, issue open, etc.)."""
        ...
```

## Architecture

```
GitHub Issue (labeled "agent-task")
  → Webhook POST → GitHubChannel (channels.py)
  → TaskRunner.enqueue(task_id, message, channel)
  → Per-issue worker: Decider (Haiku) with channel.system_prompt
    → Tools: plan/implement/review (Gemini/Qwen in container), create_pull_request
    → On completion: calls create_pull_request(title, body) tool
  → channel.deliver_result() posts status comment via gh CLI

Matrix message
  → nio callback → Bot (bot.py)
  → TaskRunner.enqueue(task_id, message, channel)
  → Per-room worker: Decider (Haiku) with channel.system_prompt
    → Same tools, same loop
  → channel.send_update() streams output to Matrix room
```

## Enqueue Signature

```python
async def enqueue(self, task_id: str, message: str, channel: Channel) -> None
```

TaskRunner never parses task IDs or routes by prefix. The caller passes the appropriate `Channel` instance. Adding new channels (Slack, Discord, CLI) requires only a new `ChannelAdapter` subclass.

## Status Log (GitHub only)

| Event | Comment |
|---|---|
| Task picked up | 🤖 Working on this issue... |
| Completed | ✅ Completed — PR: {url} |
| Failed | ❌ Failed: {error} |

Only final status is posted as a GitHub comment. `send_update` is a no-op for GitHub to avoid spamming issues. Intermediate progress is available via internal logging (see Logging below). Matrix continues streaming intermediate output.

## Logging

Internal task progress (phase transitions, tool calls, errors) is logged via Python logging at INFO level. This provides observability without posting intermediate comments to GitHub issues. A future phase may expose a log viewer or optionally post summaries to GitHub.

## task_id Format

`task_id = f"gh-{issue_number}"`

Examples: `gh-42`, `gh-7`

Container names derived via `_container_name()` which slugifies to `sandbox-gh-42`. Single-repo only for v1 — if multi-repo is needed later, embed the repo in the task_id then.

## Webhook Events

| GitHub Event | Action | Response |
|---|---|---|
| `issues` / `labeled` with `agent-task` | New task | Create worker, start working |
| `issue_comment` / `created` on `agent-task` issue | Human input | Queue for existing worker |

## Initial Message Format

Issue input is normalized to a sequence of messages, not a single blob:
- First enqueue call: issue title + body as one message
- Each existing comment: separate enqueue call, in chronological order
- Future comments arriving via webhook: enqueue as individual messages

This ensures the decider sees a consistent shape regardless of when the agent picks up the issue.

**Backfill race guard:** The `_processing` check in `enqueue()` covers this — if a webhook `issue_comment` arrives while backfilling existing comments, the worker already exists and the message is simply appended to the queue. No special dedup needed beyond what `_processing` provides.

## GitHub System Prompt

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

## Changes Required

| File | Change |
|---|---|
| `tools.py` | Add `create_pull_request(title: str, body: str)` tool. It deterministically executes the git branching, committing, pushing, and `gh pr create` commands via the sandbox, returning the PR URL. |
| `config.py` | Add `github_webhook_port: int = 8090`, `github_webhook_secret: str = ""` |
| `core.py` | Refactor `AgentCore` → `TaskRunner`: owns `_queues`, `_workers`, `_processing` set. `enqueue(task_id, message, channel)` creates queue/worker. `ChannelAdapter` ABC replaces callback params. `reconcile()` iterates running containers, calls `channel.is_valid(task_id)` on the channel stored per-task. No startup re-enqueue — orphan containers from crashes are destroyed (conversation history is lost, restart is meaningless). |
| `channels.py` | Subclasses `ChannelAdapter`. `task_id` = `gh:repo#number`. `issue_comment` handler. `deliver_result`/`deliver_error` post comments via `gh` CLI. Builds initial messages as a sequence (title+body, then each comment). `is_valid(task_id)` checks issue is open with label. |
| `decider.py` | Add `GITHUB_SYSTEM_PROMPT`. `handle_message()` accepts `system_prompt` parameter, uses it on first call for a `task_id`. |
| `bot.py` | Subclasses `ChannelAdapter`. Remove per-room queue/worker logic (moved to TaskRunner). `_on_message` calls `task_runner.enqueue()`. Remove `destroy_all()` from shutdown. `is_valid(task_id)` returns False if `task_id` not in `client.rooms`. |
| `__main__.py` | Create `TaskRunner(decider, sandbox)`. Pass to both `Bot` and `GitHubChannel`. Start/stop GitHubChannel. |
| `sandbox.py` | No changes. |

## Reconcile

TaskRunner owns a single reconcile loop (every 60s). It iterates all running containers and calls `channel.is_valid(task_id)` on the `ChannelAdapter` instance stored per-task. No prefix-based routing — the channel is associated at enqueue time.

- **Matrix** (bot.py): `is_valid` returns False if `task_id` not in `client.rooms`
- **GitHub** (channels.py): `is_valid` returns False if issue is closed or `agent-task` label removed

No `destroy_all()` on shutdown. Containers persist across restarts. Reconcile loop cleans up orphans.

## Startup Recovery

On startup, TaskRunner scans existing containers. Orphan containers from a previous crash are **destroyed** — conversation history is in-memory and lost on crash, so re-enqueueing would produce a confused agent with no context. Clean slate is the honest approach.

If persistent conversation state is added in a future phase, startup recovery can be revisited.

## Idempotency

In-memory `_processing: set[str]` on TaskRunner (single source of truth). Checked when webhook arrives. Prevents duplicate workers.

```python
# Single-instance only. No cross-process dedup.
_processing: set[str] = set()
```

## Security

Webhook HMAC signature verification (`github_webhook_secret`) is deferred to a later phase. For v1, validate the workflow end-to-end first, then harden. The webhook port should not be exposed to the public internet without signature verification.

**Phase 1.5 (same PR):** Add shared-secret query parameter check as a minimal auth gate:

```python
# channels.py webhook handler
expected = config.github_webhook_secret
if expected and request.query.get("secret") != expected:
    return web.Response(status=403)
```

This is not a substitute for HMAC verification but prevents drive-by container creation. Configure the webhook URL as `http://host:8090/webhook?secret=<token>`.

## Not in v1

- No heartbeat
- No queue-peeking — sequential message processing
- No mid-tool interruption
- No auto-merge — PR created, human reviews
- No streaming output to GitHub (use internal logging for observability)
- No full webhook HMAC verification (add before public exposure)
- No startup re-enqueue (containers from crashes are destroyed)

## Verification

```bash
uv run pytest tests/ -v

# Test 1: Label an issue → "Working" comment appears
# Test 2: Agent completes → PR created, "Completed" comment posted
# Test 3: Agent fails → "Failed" comment posted
# Test 4: Comment on issue while working → queued, processed next
# Test 5: Re-label in-progress issue → skipped (idempotency)
# Test 6: Close issue → container cleaned up by reconcile
# Test 7: Bot restart → orphan containers destroyed (clean slate)
# Test 8: Reconcile cleans orphan containers for closed issues
# Test 9: Webhook with wrong/missing secret → 403
```

## Implementation Status

All steps complete. See `docs/implementation-guide.md` for the step-by-step guide used during implementation.
