# tmux-based Persistent Gemini CLI Sessions

## Context

Currently, every `code` tool call spawns a fresh `gemini -p <task>` process that exits after completion. This means:
- No context persistence — Gemini reloads the codebase every time
- No bidirectional communication — if Gemini asks a question, nobody answers
- The orchestrator can't interact with a running Gemini session

The solution: run Gemini CLI interactively inside a tmux session per container. The orchestrator sends tasks via `tmux send-keys` and reads output via `tmux capture-pane`, detecting the `>` prompt for completion. This enables persistent context and mid-task conversation.

## Architecture

```
Orchestrator (agent.py)
  │
  ├─ code("fix the bug")
  │    └─ sandbox.code_stream()
  │         ├─ _ensure_gemini_session()     # lazy init: tmux + gemini --yolo
  │         ├─ tmux send-keys -l "fix the bug" Enter
  │         ├─ poll tmux capture-pane       # stream output to Matrix
  │         └─ detect ">" prompt            # Gemini is done/waiting
  │
  ├─ respond_to_gemini("yes, use the async version")
  │    └─ sandbox.code_stream()             # same mechanism, sends to same session
  │         ├─ tmux send-keys -l "yes, use the async version" Enter
  │         ├─ poll capture-pane
  │         └─ detect ">" prompt
  │
  └─ Gemini remembers both interactions (persistent session)
```

## Changes by File

### Containerfile
Add `tmux` to the existing apt-get line.

### config.py
New settings:
- `gemini_poll_interval: float = 0.5` — seconds between capture-pane polls
- `gemini_startup_timeout: int = 30` — seconds to wait for initial `>` prompt

### sandbox.py (core work)

**New state in `__init__`:**
- `self._gemini_sessions: dict[str, bool]` — tracks which rooms have active sessions
- `self._pane_line_offset: dict[str, int]` — tracks last read position per room

**New methods:**

`_ensure_gemini_session(chat_id)`:
- Called lazily on first `code` call for a room
- Creates tmux session: `podman exec <name> tmux new-session -d -s gemini -x 220 -y 50`
- Sends: `cd /workspace && gemini --yolo` (auto-approves all tool calls)
- Polls capture-pane until `>` prompt appears (startup complete)
- Sets `history-limit 50000` to manage scrollback

`_capture_pane(chat_id)`:
- Runs `podman exec <name> tmux capture-pane -t gemini -p -S -`
- Strips ANSI codes, returns full pane content

`_wait_for_prompt(chat_id, timeout)`:
- Polls capture-pane every `gemini_poll_interval` seconds
- Returns new output (since last offset) when last non-empty line is `>`
- Raises `TimeoutError` if prompt not seen within timeout

**Rewritten `code_stream()`:**
1. `_ensure_gemini_session()`
2. For short tasks (<500 chars): `tmux send-keys -t gemini -l <task>` + `Enter`
3. For long tasks: write to `/workspace/.ipc/current_task.txt` via IPC volume mount, then send-keys a short reference
4. Poll capture-pane, stream new chunks to `on_chunk` callback
5. When `>` prompt detected on last non-empty line, return accumulated output

**Rewritten `code()`:** Same as code_stream but without the streaming callback.

**Updated `destroy()`:** Kill tmux session, clean up `_gemini_sessions` and `_pane_line_offset`.

**Updated `load_state()`:** After reconnecting a running container, check if tmux session exists (`tmux has-session -t gemini`). If so, restore tracking state and set offset to current pane length.

### tools.py

New `respond_to_gemini` tool:
```json
{
  "name": "respond_to_gemini",
  "description": "Send a follow-up response to the running Gemini CLI session. Use when Gemini asked a question or needs clarification.",
  "parameters": {
    "type": "object",
    "properties": {
      "response": {
        "type": "string",
        "description": "The response to send to Gemini's question"
      }
    },
    "required": ["response"]
  }
}
```

Internally uses the same `code_stream()` / tmux send-keys mechanism. The difference from `code` is semantic — helps the orchestrator LLM distinguish between "new task" and "answer Gemini's question".

### agent.py

Update SYSTEM_PROMPT to explain:
- Gemini runs in a persistent session and remembers previous tasks within the same room
- When Gemini asks a question, use `respond_to_gemini` to answer
- `code` sends a new task; `respond_to_gemini` continues the conversation

## Prompt Detection

Gemini CLI shows `>` when waiting for user input in interactive mode. Detection logic:

1. Capture full pane with `tmux capture-pane -t gemini -p -S -`
2. Strip ANSI escape codes
3. Find last non-empty line
4. If it equals `>` (or ends with `>`), Gemini is idle

**False positive mitigation:** Only match when `>` is the entire trimmed last line, not part of output content (e.g. code with `>` operators).

**Crash detection:** If the last line shows a shell prompt (`$` or `#`) instead of `>`, Gemini has crashed. `_ensure_gemini_session` should detect this and restart.

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| `>` prompt false positive in output | Match only when `>` is the entire last non-empty line after ANSI stripping |
| Multiline/special chars break send-keys | Write to IPC file for tasks >500 chars; use `-l` (literal) flag for short ones |
| Gemini crashes mid-session | `_ensure_gemini_session` checks session exists, detects shell `$` prompt, recreates |
| tmux scrollback overflow on long sessions | Set `history-limit 50000` on session creation |
| Race condition on startup | Poll for `>` prompt with configurable timeout before sending first task |
| Partial output reads | Offset-based tracking ensures each poll returns only new content |

## Testing

1. Rebuild container: `podman build -t matrix-agent-sandbox:latest -f Containerfile .`
2. Write a test script (`scripts/test-tmux-session.sh`) that:
   - Creates a container with tmux installed
   - Starts a tmux session with `gemini --yolo`
   - Sends a task via send-keys
   - Polls capture-pane for output and `>` prompt
   - Sends a follow-up task (verifies context persistence)
3. Lint and test: `uv run ruff check src tests` and `uv run pytest tests/`
4. End-to-end on VPS: deploy, send a multi-step task via Matrix, verify Gemini remembers context across `code` calls
