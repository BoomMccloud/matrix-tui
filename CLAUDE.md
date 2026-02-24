# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Matrix Agent — a self-hosted agentic coding assistant accessible via Matrix chat. Each Matrix room gets an isolated Podman container with a full dev environment. The bot uses an LLM (default Haiku 4.5 via OpenRouter) as an orchestrator/router and delegates heavy coding work to Gemini CLI (1M token context) inside the sandbox.

## Commands

```bash
uv run python -m matrix_agent          # start the bot
uv run pytest tests/                    # run tests
uv run ruff check src tests             # lint
podman build -t matrix-agent-sandbox:latest -f Containerfile .  # rebuild sandbox image
sudo systemctl restart matrix-agent     # restart on VPS
bash scripts/deploy.sh                  # full deploy: pull + rebuild + restart
```

## Architecture

```
Matrix Client → Synapse Homeserver → Bot (bot.py)
                                       ├─ per-room asyncio.Queue + worker Task
                                       ├─ Agent (agent.py) — LiteLLM tool-calling loop
                                       └─ SandboxManager (sandbox.py) — Podman containers
                                            └─ Tools (tools.py): run_command, read_file,
                                               write_file, code (Gemini CLI), take_screenshot,
                                               run_tests, self_update
```

- **bot.py** — Matrix event handling, room lifecycle, per-room message queuing. One container per room, created lazily. Streams Gemini output back to chat.
- **agent.py** — LLM orchestrator loop. Maintains per-room conversation history. Iterates up to `max_agent_turns` calling tools via function calling.
- **sandbox.py** — Podman container creation/destruction, state persistence. Named containers (`sandbox-<room-slug>`) enable reconnection after restart. Atomic state.json writes via tmp+rename.
- **tools.py** — Tool schemas and execution dispatch. The `code` tool delegates to Gemini CLI with streaming output.
- **config.py** — Pydantic settings derived from `.env`. `VPS_IP` auto-derives Matrix URLs.

## Key Conventions (from AGENTS.md)

- **Subprocess safety**: Always `asyncio.create_subprocess_exec()`, never `shell=True`
- **Container ops**: All through SandboxManager only
- **State persistence**: Atomic writes (`state.json.tmp` → `os.replace()`)
- **Per-room isolation**: Each room has one Queue + one worker Task; rooms are independent
- **Startup**: login → sync → load_state (reconnect containers) → load_histories → sync_forever
- **Gemini CLI**: Must run from `/workspace` for GEMINI.md auto-loading

## Gotchas

- macOS `/tmp` symlinks to `/private/tmp` — use resolved paths for IPC
- E2EE not supported (libolm not installed)
- `self_update` tool runs on the VPS host, not inside the sandbox container
- Histories are in-memory but persisted to state.json after every agent reply
