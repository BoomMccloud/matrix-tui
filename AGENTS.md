# AGENTS.md

This file is for both humans and AI agents. It documents conventions, architecture decisions,
and gotchas discovered while working on this codebase. Append to it when you learn something
that would help the next person — or agent — working here.

**Do not delete existing entries. Append only.**

---

## How to add an entry

### Convention (coding pattern or style rule)
```
## Conventions
- Use asyncio.create_subprocess_exec (not shell=True) for all subprocess calls — avoids shell injection
- All container operations go through SandboxManager, never call podman directly from tools.py
```

### Decision (why something was done a certain way)
```
## Decisions
- [2026-02-24] Switched router from Sonnet 4 to Haiku 4.5 — 3x cheaper, sufficient for tool routing
- [2026-02-24] Named containers (sandbox-<slug>) instead of random IDs — enables reconnect after restart
```

### Gotcha (non-obvious behaviour or known trap)
```
## Gotchas
- /tmp on macOS is a symlink to /private/tmp — use /private/tmp for podman cp on Mac
- Gemini CLI must run from /workspace for GEMINI.md to be auto-loaded
- state.json is written atomically (write to .tmp, then rename) — do not write it directly
```

---

## Project

Matrix bot that gives each room an isolated Podman container with a Gemini CLI coding agent.
One container per room, named `sandbox-<room-slug>`. State persists across restarts via `state.json`.

## Architecture

- `src/matrix_agent/bot.py` — Matrix event handling, room lifecycle
- `src/matrix_agent/agent.py` — LLM tool-calling loop (orchestrator, Haiku 4.5)
- `src/matrix_agent/sandbox.py` — Podman container management + state persistence
- `src/matrix_agent/tools.py` — Tool schemas and dispatch
- `src/matrix_agent/config.py` — Pydantic settings from .env

## Conventions

- Use `asyncio.create_subprocess_exec` for all subprocess calls — no shell=True, no injection risk
- Tasks passed to Gemini as direct argv (`gemini -p <task>`), never via `sh -c "gemini -p '...'"`
- All container ops go through `SandboxManager` methods, never call podman directly from tools.py
- State written atomically: write to `state.json.tmp`, then `os.replace()` to avoid corruption

## Decisions

- [2026-02-24] Haiku 4.5 as orchestrator — 3x cheaper than Sonnet 4, sufficient for tool routing
- [2026-02-24] Named containers (`sandbox-<slug>`) — enables reconnect after bot restart
- [2026-02-24] Single `state.json` owned by SandboxManager — containers + histories in one file
- [2026-02-24] AfterAgent hook for `status.md` — more reliable than prompting Gemini to remember

## Gotchas

- `/tmp` on macOS is a symlink to `/private/tmp` — use `/private/tmp` for `podman cp` on Mac
- Gemini CLI must run from `/workspace` (via `--workdir`) for `GEMINI.md` to be auto-loaded
- E2EE Matrix rooms block messages — use unencrypted rooms with this bot
- Bot loses container mapping on restart unless named containers + state.json are in use
- `_synced` flag must be True before handling any Matrix events — pre-startup events are replayed

## Orchestrator Lifecycle

The orchestrator is the bot process (`uv run python -m matrix_agent`), typically running as a
systemd service. It owns all per-room state and coordinates between Matrix and sandboxes.

### Startup sequence

```
1. AsyncClient created (not yet connected)
2. Login to Matrix homeserver
3. Register event callbacks (invite, message, member)
4. Initial sync — replays all missed events since last disconnect
   └─ _synced = False during this phase; all callbacks return early
5. Catch-up joins: auto-join any rooms invited to while offline (no greeting sent)
6. load_state() — reads state.json from disk
   ├─ For each saved container: podman inspect → verify still running
   ├─ Live containers → reconnected (mapping restored)
   └─ Stale containers → dropped (history cleared, recreated on next message)
7. agent.load_histories() — restores per-room conversation histories into memory
8. _synced = True — event handling begins
9. sync_forever() — long-poll loop starts
```

### Shutdown sequence

```
1. sync_forever() exits (SIGTERM / SIGINT / unhandled exception)
2. destroy_all() — stops and removes every sandbox container
3. client.close() — disconnects from Matrix homeserver
```

State is **not** written on shutdown — it is written incrementally after every agent reply
and after every sandbox create/destroy. Shutdown is therefore safe to SIGKILL.

### Restart behaviour

- Containers survive a bot restart (they keep running in Podman)
- On next startup, load_state() reconnects live containers; stale ones are recreated on demand
- Conversation histories are restored from state.json — the orchestrator remembers prior turns
- Per-room worker tasks and queues are recreated fresh on the next incoming message

---

## Room & Container Lifecycle

Each Matrix room maps 1-to-1 with a Podman container. The container is created lazily on the
first message, not on invite.

### Room open (first message)

```
Matrix invite received
  └─ Bot joins room, sends greeting — no container yet

First user message arrives
  └─ _on_message enqueues text → _room_worker starts (asyncio.Task per room)
       └─ _process_message:
            ├─ sandbox.create(room_id)
            │    ├─ podman run -d --name sandbox-<slug> ...
            │    ├─ _init_workspace(): writes GEMINI.md, status.md, AfterAgent hook
            │    └─ save_state()
            ├─ Send "⏳ Working on it..."
            └─ agent.handle_message() → tool-calling loop → replies streamed back
```

### Subsequent messages (same room)

```
User message arrives
  └─ _on_message: put(text) onto room's asyncio.Queue
       ├─ If queue was empty → worker picks it up immediately
       └─ If worker is busy → send "⏳ Queued (position N)" ack
            └─ Worker processes in order, one message at a time
```

### Room close (bot kicked or last user leaves)

```
RoomMemberEvent received
  └─ sandbox.destroy(room_id)
       ├─ podman stop sandbox-<slug>
       ├─ podman rm -f sandbox-<slug>
       └─ save_state()
  └─ _cancel_worker(room_id) — cancels asyncio.Task, drops Queue
  └─ (if last user left) client.room_leave(room_id)
```

### Container naming

Container names are derived deterministically from the Matrix room ID:

```
room_id:  !abc123:example.com
slug:     -abc123-example-com   (non-alphanumeric → dash, leading dashes stripped)
name:     sandbox--abc123-example-com
```

This makes containers identifiable in `podman ps` and survivable across bot restarts.

### Per-room queue

Each room has exactly one `asyncio.Queue` and one `asyncio.Task` (the worker). The worker
is a `while True` loop that pulls messages and calls `_process_message` sequentially.
Rooms are fully independent — a slow task in room A does not delay room B.

```
Room A queue:  [msg1] → processing   Room B queue:  [msg1, msg2]
Room A worker: running _process_message(msg1)
Room B worker: running _process_message(msg1), msg2 waiting
```

---

## Commands

```bash
uv run python -m matrix_agent   # start the bot
uv run pytest tests/            # run tests
uv run ruff check src tests     # lint
podman build -t matrix-agent-sandbox:latest -f Containerfile .  # rebuild sandbox image
```
