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
Matrix Client → Bot (bot.py) → MatrixChannel ─┐
                                               ├─→ TaskRunner (core.py) ─→ Decider (decider.py)
GitHub Webhook → GitHubChannel (channels.py) ──┘        │                       │
                                                   SandboxManager          LiteLLM + Tools
                                                   (sandbox.py)           (tools.py)
```

- **core.py** — `TaskRunner`: channel-agnostic queue/worker lifecycle. Owns `_queues`, `_workers`, `_processing` set. `enqueue(task_id, message, channel)` creates per-task workers. `reconcile_loop()` cleans up invalid tasks every 60s.
- **channels.py** — `ChannelAdapter` ABC (`send_update`, `deliver_result`, `deliver_error`, `is_valid`). `GitHubChannel` handles webhooks (HMAC-verified), posts status comments via `gh` CLI.
- **bot.py** — Matrix event handling, room lifecycle. `MatrixChannel` adapter streams output to Matrix rooms. Delegates all task work to `TaskRunner.enqueue()`.
- **decider.py** — LLM orchestrator loop (Haiku via LiteLLM). Per-task conversation history. Routes tools: `plan`/`review` → Gemini, `implement` → Qwen. Accepts per-channel `system_prompt`.
- **sandbox.py** — Podman container creation/destruction, state persistence. Named containers (`sandbox-<slug>`) enable reconnection after restart. Atomic state.json writes via tmp+rename.
- **tools.py** — Tool schemas and execution dispatch. Tools: `plan`, `implement`, `review`, `run_command`, `read_file`, `write_file`, `run_tests`, `take_screenshot`, `self_update`, `create_pull_request`.
- **config.py** — Pydantic settings from `.env`. Includes `github_webhook_port`, `github_webhook_secret`, `github_token`.

## Key Conventions (from AGENTS.md)

- **Subprocess safety**: Always `asyncio.create_subprocess_exec()`, never `shell=True`
- **Container ops**: All through SandboxManager only
- **State persistence**: Atomic writes (`state.json.tmp` → `os.replace()`)
- **Per-task isolation**: Each task (Matrix room or GitHub issue) has one Queue + one worker Task; tasks are independent
- **Startup**: load_state → load_histories → destroy_orphans → login → sync → sync_forever
- **Channel adapters**: New channels (Slack, Discord, CLI) require only a new `ChannelAdapter` subclass
- **Gemini CLI**: Must run from `/workspace` for GEMINI.md auto-loading

## Gotchas

- macOS `/tmp` symlinks to `/private/tmp` — use resolved paths for IPC
- E2EE not supported (libolm not installed)
- `self_update` tool runs on the VPS host, not inside the sandbox container
- Histories are in-memory but persisted to state.json after every agent reply
