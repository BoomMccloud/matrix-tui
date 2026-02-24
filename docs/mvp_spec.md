# Matrix Agent — Architecture & Design

> This document describes the current deployed architecture. See git history for the original Telegram-based MVP spec.

## Overview

A self-hosted agentic coding assistant accessible via Matrix chat. Each room gets an isolated Podman sandbox container. The orchestrator (Claude Sonnet via LiteLLM) routes tasks to tools; heavy coding is delegated to Gemini CLI running inside the sandbox.

## Architecture

```
Matrix Client (Element)
       |
Synapse homeserver (self-hosted, port 8008, no federation)
       |
Matrix Bot  (python-nio + LiteLLM)
       |
Claude Sonnet (orchestrator)
       |
Tools ─────────────────────────────────────────────────────────
  run_command     → podman exec (shell commands in sandbox)
  write_file      → podman exec (write files in sandbox)
  read_file       → podman exec (read files in sandbox)
  code            → podman exec gemini -p (Gemini CLI, streams output)
  run_tests       → podman exec ruff + pytest
  take_screenshot → podman exec playwright + podman cp
  self_update     → git pull + podman build + systemctl restart (host)
       |
Podman sandbox container (one per room)
  - Node.js 20, Python 3, git, gh CLI, Playwright
  - Gemini CLI for coding tasks
  - GITHUB_TOKEN for repo access and PR submission
```

## Design Decisions

- **Self-hosted Synapse over matrix.org.** matrix.org blocks long-poll sync connections from VPS IPs. Running Synapse locally on port 8008 (plain HTTP, no federation) eliminates this entirely.
- **Subprocess over podman-py.** `podman exec` via `asyncio.create_subprocess_exec` is simpler and more debuggable. No SDK quirks.
- **Gemini CLI as coding agent.** 1M token context — can read entire repos. Orchestrator delegates non-trivial coding to it rather than doing it directly. Gemini output streams to the Matrix room.
- **One container per room.** Full isolation. Container is created on first message, destroyed when all users leave.
- **State persisted to disk.** `state.json` on the host maps room IDs to container names and stores conversation history. Containers survive bot restarts.
- **Gemini hooks for IPC.** `UserInputRequired` hook writes a sentinel file to a bind-mounted host directory. Bot polls for it and sends a Matrix notification.
- **Self-updating.** Agent calls `self_update` tool → git pull + image rebuild + service restart. Sends result before restart kills the process.

## Stack

- **Python 3.12**, **uv** package manager
- **python-nio** — async Matrix SDK
- **litellm** — abstract LLM provider (Claude via OpenRouter, Gemini, etc.)
- **pydantic-settings** — env-based configuration
- **Podman** (CLI via subprocess) — container lifecycle
- **Playwright** — headless browser screenshots (inside sandbox)
- **Gemini CLI** — coding agent inside sandbox
- **gh CLI** — GitHub PR submission inside sandbox
- **Synapse** — self-hosted Matrix homeserver (Podman container)

## Project Structure

```
matrix-tui/
├── Containerfile                    # Sandbox image (Node + Python + Playwright + Gemini + gh)
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
3. **Tasks** — Sonnet routes tool calls; Gemini output streams to chat in ~800-char chunks
4. **Input required** — if Gemini needs input, IPC hook notifies the Matrix room
5. **Cleanup** — container and IPC directory destroyed when all users leave or bot is kicked

## Deployment

See [README.md](../README.md) for full deployment instructions.

Summary:
1. Fill in `.env` (only `VPS_IP`, passwords, and API keys needed)
2. `bash scripts/setup-synapse.sh` — sets up Synapse, creates accounts
3. Build sandbox image + install systemd service
4. Connect Element to `http://<VPS_IP>:8008`, invite the bot

## Future

- Non-blocking tool calls (Sonnet responds while Gemini works)
- Resource limits (`--memory`, `--cpus`) on sandbox containers
- E2EE support
- Multiple concurrent coding agents per room
