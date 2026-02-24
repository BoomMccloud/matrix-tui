# Memory & Persistence Spec

## Problem

The bot loses all state on restart:
- Orchestrator forgets which containers belong to which rooms
- Gemini CLI starts each task with no knowledge of prior work
- Users have to re-explain context after every bot restart or self-update

## Two-Phase Solution

---

## Phase 1 — Orchestrator Memory (survives reboots)

**Goal:** The main agent remembers rooms, containers, and conversation context across restarts.

### What gets persisted

A single JSON file on the VPS host: `/home/matrix-tui/state.json`

```json
{
  "containers": {
    "!roomid:matrix.org": "container-name"
  },
  "history": {
    "!roomid:matrix.org": [
      {"role": "user", "content": "..."},
      {"role": "assistant", "content": "..."}
    ]
  }
}
```

### Container naming

Instead of random container IDs, containers are named after a slug of the room ID:

```
podman run --name "sandbox-abc123" ...
```

On startup, the bot reads `state.json`, calls `podman inspect <name>` to confirm the container is still alive, and reconnects if it is. If the container is gone, it removes the stale entry and creates a fresh one on next message.

### Changes

| File | Change |
|------|--------|
| `sandbox.py` | Name containers on `create()`, add `save_state()` / `load_state()` |
| `agent.py` | Load history from state on init, save after each reply |
| `bot.py` | Call `load_state()` on startup instead of `_reconnect_containers()` |

### State write triggers

- After `sandbox.create()` — new container mapping saved
- After `sandbox.destroy()` — entry removed, state saved
- After each agent reply — message history saved

State is written atomically (write to `.tmp`, rename) to avoid corruption on crash.

### Pass criteria

1. Bot starts, sends a message, gets a reply
2. Bot restarts (`self_update`)
3. Send another message to the same room — bot remembers prior conversation and reconnects to the same container

---

## Phase 2 — Gemini Agent Memory (workspace context files)

**Goal:** Gemini CLI reads prior task history before each invocation and appends what it did, so future invocations build on prior work.

### Workspace file layout

```
/workspace/
├── context.md      # Accumulated learnings — Gemini reads + appends
├── status.md       # Append-only log of what each task did
└── <repo files>    # Cloned repository or work files
```

### context.md format

```markdown
# Context

## Conventions discovered
- This project uses pytest, not unittest
- Main entry point is src/main.py
- All functions must have type hints

## Prior tasks
- [2026-02-24] Added prime checker function to utils.py
- [2026-02-24] Fixed off-by-one in loop at utils.py:14
```

### status.md format

```markdown
# Status Log

[2026-02-24 02:30] Task: write a prime checker
[2026-02-24 02:30] Created utils.py with is_prime(). All checks pass.

[2026-02-24 02:45] Task: add tests for prime checker
[2026-02-24 02:45] Created test_utils.py. 5 tests, all passing.
```

### Gemini invocation with context

The `code()` method in `sandbox.py` is updated to:

1. Write `/workspace/task.md` with the current task (already escaping-safe via stdin pipe)
2. Prepend context injection to the prompt:

```
Read /workspace/context.md and /workspace/status.md for prior work context, then:
<actual task>

After completing, append a one-line summary to /workspace/status.md and any
discovered conventions to /workspace/context.md.
```

3. Call `gemini -p "<injected prompt>"` — still via direct argv, no shell

### Workspace initialization

On `sandbox.create()`, write initial empty files:

```bash
mkdir -p /workspace
echo "# Context\n" > /workspace/context.md
echo "# Status Log\n" > /workspace/status.md
```

If the container is reconnected (Phase 1), these files already exist with prior content — no overwrite.

### Pass criteria

1. Ask Gemini to write a function → it creates the file, appends to status.md
2. Bot restarts (container survives via Phase 1)
3. Ask Gemini to add tests → it reads context.md, knows the function exists, writes compatible tests without being told

---

## What is NOT in scope

- Trimming old history (history grows unbounded — acceptable for now)
- Encrypting state.json (contains no secrets, only message text and container names)
- Multi-room context sharing (each room is fully isolated)
