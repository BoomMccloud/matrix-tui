# Matrix Agent — Architecture & Design

> This document describes the current deployed architecture. See git history for the original Telegram-based MVP spec.

## Overview

A self-hosted agentic coding assistant accessible via Matrix chat. Each room gets an isolated Podman sandbox container. The orchestrator (Haiku via LiteLLM) routes tasks to two coding agents: Gemini CLI for planning/review and Qwen Code for implementation.

## Architecture

```
Matrix Client (Element)
       |
Synapse homeserver (self-hosted, port 8008, no federation)
       |
Matrix Bot  (python-nio + LiteLLM)
       |
Haiku (orchestrator)
       |
Tools ─────────────────────────────────────────────────────────
  plan            → podman exec gemini -p (Gemini CLI, planning/analysis)
  implement       → podman exec qwen -p (Qwen Code, writes code)
  review          → podman exec gemini -p (Gemini CLI, code review)
  run_command     → podman exec (shell commands in sandbox)
  write_file      → podman exec (write files in sandbox)
  read_file       → podman exec (read files in sandbox)
  run_tests       → podman exec ruff + pytest
  take_screenshot → podman exec playwright + podman cp
  self_update     → git pull + podman build + systemctl restart (host)
       |
Podman sandbox container (one per room)
  - Node.js 20, Python 3, git, gh CLI, Playwright
  - Gemini CLI for planning/review, Qwen Code for implementation
  - GITHUB_TOKEN for repo access and PR submission
```

## Design Decisions

- **Self-hosted Synapse over matrix.org.** matrix.org blocks long-poll sync connections from VPS IPs. Running Synapse locally on port 8008 (plain HTTP, no federation) eliminates this entirely.
- **Subprocess over podman-py.** `podman exec` via `asyncio.create_subprocess_exec` is simpler and more debuggable. No SDK quirks.
- **Two coding agents.** Gemini CLI (1M context) handles planning and review — can read entire repos. Qwen Code handles implementation. The orchestrator routes by task type via `plan`, `implement`, `review` tools.
- **One container per room.** Full isolation. Container is created on first message, destroyed when all users leave.
- **State persisted to disk.** `state.json` on the host maps room IDs to container names and stores conversation history. Containers survive bot restarts.
- **Notification hooks for IPC.** Gemini's `Notification` hook writes events to a bind-mounted host directory. Bot polls and sends Matrix notifications. See `notification_hook.md`.
- **Self-updating.** Agent calls `self_update` tool → git pull + image rebuild + service restart. Sends result before restart kills the process.

## Stack

- **Python 3.12**, **uv** package manager
- **python-nio** — async Matrix SDK
- **litellm** — abstract LLM provider (Claude via OpenRouter, Gemini, etc.)
- **pydantic-settings** — env-based configuration
- **Podman** (CLI via subprocess) — container lifecycle
- **Playwright** — headless browser screenshots (inside sandbox)
- **Gemini CLI** — planning/review agent inside sandbox
- **Qwen Code** — implementation agent inside sandbox
- **gh CLI** — GitHub PR submission inside sandbox
- **Synapse** — self-hosted Matrix homeserver (Podman container)

## Project Structure

```
matrix-tui/
├── Containerfile                    # Sandbox image (Node + Python + Playwright + Gemini + Qwen + gh)
├── scripts/
│   ├── setup-synapse.sh             # One-time Synapse homeserver setup
│   └── deploy.sh                    # Manual deploy (git pull + image rebuild + restart)
├── src/
│   └── matrix_agent/
│       ├── __main__.py              # Entry point
│       ├── config.py                # Pydantic settings (env vars, derives from VPS_IP)
│       ├── sandbox.py               # Container lifecycle, IPC, workspace init
│       ├── tools.py                 # Tool schemas + execution + self_update
│       ├── agent.py                 # LLM tool-calling loop with streaming
│       └── bot.py                   # Matrix bot, room workers, IPC watcher
└── /home/matrix-tui/state.json     # Runtime state (containers + histories)
```

## Room Lifecycle

1. **Invite** — bot joins and sends greeting
2. **First message** — sandbox container created, workspace initialized (`GEMINI.md`, `status.md`, hooks)
3. **Tasks** — orchestrator routes to plan/implement/review; agent output streams to chat in ~800-char chunks
4. **Notifications** — Gemini `Notification` hook surfaces events (e.g. ToolPermission) to the Matrix room
5. **Cleanup** — container and IPC directory destroyed when all users leave or bot is kicked

## Deployment

See [README.md](../README.md) for full deployment instructions.

Summary:
1. Fill in `.env` (only `VPS_IP`, passwords, and API keys needed)
2. `bash scripts/setup-synapse.sh` — sets up Synapse, creates accounts
3. Build sandbox image + install systemd service
4. Connect Element to `http://<VPS_IP>:8008`, invite the bot

## Roadmap

### ~~Step 1 — Multi-agent routing~~ ✓ Done
Implemented. `plan` and `review` route to Gemini CLI, `implement` routes to Qwen Code. See `multi_agent_routing.md`.

### Step 2 — tmux persistent sessions (next)
Run each agent (Gemini, Qwen) in its own tmux session inside the container. Enables context persistence across invocations and bidirectional communication (orchestrator can answer agent questions mid-task). See `tmux_gemini_sessions.md`.

### Step 3 — Structured programming loop
`/spec → /analyze → /verify → /go` workflow using the plan/implement/review tools with tmux sessions. Requires non-blocking tool calls so the bot can accept commands while agents work. See `programming-loop-spec.md`.

### Step 4 — Channel-agnostic task pipeline

#### Phase 1 — GitHub webhook + blocking execution ✓ Done
- `channels.py`: `Task` dataclass, `ChannelAdapter` ABC, `GitHubChannel` (HMAC-verified webhook, `POST /webhook/github`)
- `core.py`: `AgentCore.submit()` — creates container, clones repo, runs `code_stream()` with `auto_accept=True`, fires `on_result`/`on_error` callbacks
- `sandbox.py`: `auto_accept` flag on `code()` and `code_stream()` (passes `-y` to Gemini CLI)
- Blocking model: `submit()` awaits the full Gemini run before returning

#### Phase 2 — Hook-driven event model (non-blocking)
Replace blocking `code_stream()` with fire-and-forget execution. Gemini CLI hooks handle state transitions via IPC files on the bind-mounted `.ipc/` directory.

**Hooks to wire up** (see [Gemini CLI hook events](https://googlegemini.wiki/gemini-cli/hooks)):

| Hook | Purpose |
|---|---|
| `SessionStart` | Inject task context (`task.json`) into Gemini's context — richer than cramming into `-p` |
| `AfterAgent` | **Completion signal.** Write `result.json` to IPC with status/stdout/error. Can `retry` or `halt` |
| `AfterTool` | Streaming progress — write `progress.json` after each tool execution |
| `BeforeTool` | Gate dangerous ops (block writes outside `/workspace`, destructive commands) |
| `Notification` | Forward advisory messages to channel (already wired for Matrix) |
| `SessionEnd` | Final cleanup, mark task done/failed in IPC |
| `PreCompress` | Checkpoint save before context compression on long tasks |

**Architecture change:**
```
core.submit()  →  launch gemini (fire-and-forget)
                     │
                     ├─ SessionStart hook reads .ipc/task.json, injects context
                     ├─ AfterTool hook writes .ipc/progress.json
                     ├─ AfterAgent hook writes .ipc/result.json
                     └─ SessionEnd hook writes .ipc/done.json

Host IPC watcher (single asyncio loop per container)
  watches $ipc_base_dir/sandbox-*/
  dispatches → ChannelAdapter.deliver_result / deliver_error / deliver_progress
```

**Benefits over Phase 1:**
- Non-blocking: doesn't tie up an asyncio task per running Gemini session
- Streaming progress to GitHub/Matrix before task completes
- Retry/escalation logic lives inside the container (AfterAgent can halt or retry)
- BeforeTool gating adds policy enforcement inside Gemini's own loop
- UserInputRequired escalation via IPC (write file, host responds or escalates to channel)

### Other
- Resource limits (`--memory`, `--cpus`) on sandbox containers
- E2EE support
