# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Matrix Agent — a self-hosted agentic coding assistant accessible via Matrix chat and GitHub issues. Each task gets an isolated Podman container with a full dev environment. Two execution paths exist: Matrix chat uses an LLM router (Haiku via LiteLLM) that delegates to Gemini CLI and Qwen Code inside the sandbox; GitHub issues use Gemini CLI directly in a single session that plans, implements, tests, and creates PRs.

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
                                               ├─→ TaskRunner (core.py)
GitHub Webhook → GitHubChannel (channels.py) ──┘        │
                                                        ├─→ _process_matrix → Decider (decider.py) → LiteLLM + Tools
                                                        └─→ _process_github → Gemini CLI (single session)
                                                               │
                                                          SandboxManager (sandbox.py)
```

- **core.py** — `TaskRunner`: channel-agnostic queue/worker lifecycle. Owns `_queues`, `_workers`, `_processing` set. `enqueue(task_id, message, channel)` creates per-task workers. Routes `gh-*` task IDs to `_process_github`, all others to `_process_matrix`. `reconcile_loop()` cleans up invalid tasks every 60s.
- **channels.py** — `ChannelAdapter` ABC (`send_update`, `deliver_result`, `deliver_error`, `is_valid`). `GitHubChannel` handles webhooks (HMAC-verified), posts status comments via `gh` CLI.
- **bot.py** — Matrix event handling, room lifecycle. `MatrixChannel` adapter streams output to Matrix rooms. Delegates all task work to `TaskRunner.enqueue()`.
- **decider.py** — LLM orchestrator loop (Haiku via LiteLLM). Per-task conversation history. Routes tools: `plan`/`review` → Gemini CLI, `implement` → Qwen Code. Used only by the Matrix chat path.
- **sandbox.py** — Podman container creation/destruction, state persistence, Gemini CLI session management, post-run validation (`validate_work`). Named containers (`sandbox-<slug>`) enable reconnection after restart. Atomic state.json writes via tmp+rename.
- **tools.py** — Tool schemas and execution dispatch. Tools: `plan`, `implement`, `review`, `run_command`, `read_file`, `write_file`, `run_tests`, `take_screenshot`, `self_update`, `create_pull_request`.
- **config.py** — Pydantic settings from `.env`. Includes `github_webhook_port`, `github_webhook_secret`, `github_token`, `gemini_model`.

## Key Conventions

- **Subprocess safety**: Always `asyncio.create_subprocess_exec()`, never `shell=True`
- **Container ops**: All through SandboxManager only
- **State persistence**: Atomic writes (`state.json.tmp` → `os.replace()`)
- **Per-task isolation**: Each task (Matrix room or GitHub issue) has one Queue + one worker Task; tasks are independent
- **Startup**: load_state → load_histories → destroy_orphans → login → sync → sync_forever
- **Channel adapters**: New channels (Slack, Discord, CLI) require only a new `ChannelAdapter` subclass
- **Gemini CLI**: Must run from `/workspace` for GEMINI.md auto-loading

## Matrix Chat Path

```
Matrix room message
  → MatrixChannel → TaskRunner._process_matrix
    → Decider.handle_message (LiteLLM, multi-turn)
      → Routes tool calls: plan/review → Gemini CLI, implement → Qwen Code
      → Conversation history persisted per-task
    → deliver_result back to Matrix room
```

## GitHub Issue Path

```
GitHub Issue (labeled agent-task)
  → Webhook (HMAC-verified) → GitHubChannel → TaskRunner._process_github
    → Clone repo into sandbox container
    → Single Gemini CLI session via /fix-issue command (cmd-fix-issue.toml)
      → Gemini plans, declares scope (changed-files.txt), implements, tests, creates PR
      → SessionStart hook auto-installs deps + runs baseline tests
    → Host validation (validate_work): tests, scope check, PR URL, acceptance criteria
    → If validation fails: retry with feedback (max 3 attempts)
    → deliver_result (PR URL) or deliver_error
  → CI runs on PR
    → If CI fails → ci-feedback.yml comments ⚠️ on issue + reopens it
    → Bot picks up reopened issue → single Gemini session via /fix-ci command
    → Fixes, force-pushes → CI re-runs → loop until green
```

## Gemini CLI Hooks (inside sandbox)

All hooks live in `src/matrix_agent/templates/` and are copied to `/workspace/.gemini/` at container init.

- **SessionStart** (`hook-session-start.sh`) — Detects project type (Python/Node/Rust/Go), installs deps, runs baseline tests → `/workspace/.baseline-tests.txt`
- **BeforeTool** (`hook-before-tool.sh`) — Blocks `git push` without `--force`, blocks `git add -A`/`git add .`, blocks writes to forbidden files (pyproject.toml, uv.lock, .gemini/, .github/, etc.)
- **AfterTool** (`hook-after-tool.sh`) — Writes tool progress JSON to `/workspace/.ipc/event-progress.json`
- **AfterAgent** (`hook-after-agent.sh`) — Writes final result JSON to `/workspace/.ipc/event-result.json`, appends timestamp to `status.md`

## IPC Directory (`/workspace/.ipc/`)

Host-mounted volume for communication between the sandbox and the host. Read by `validate_work()` and `_process_github`.

- `changed-files.txt` — File manifest (one file per line), checked against forbidden list
- `acceptance-criteria.md` — Testable acceptance criteria
- `pr-url.txt` — PR URL after creation (source of truth for success)
- `clarification.txt` — Questions if issue is unclear (stops pipeline, posts to issue)

## Context Persistence Across Sessions

Gemini CLI sessions are stateless (no `--resume`). Cross-session context relies on:

- **`GEMINI.md`** — Auto-loaded by Gemini CLI. Imports `@status.md` for prior work history.
- **`status.md`** — Appended by AfterAgent hook with timestamps. Auto-imported into every session.
- **Container filesystem** — Files, git state, and IPC directory persist between sessions within the same task.

No LLM conversation history is carried between Gemini CLI sessions. Each session starts fresh with only GEMINI.md context + filesystem state.

## Safety Guardrails

- **BeforeTool hook**: Blocks `git push` (except `--force`), `git add -A`/`.`, writes to forbidden files
- **validate_work()**: Post-run check for test failures, scope creep (undeclared files), forbidden file modifications, missing PR URL
- **Forbidden files**: `pyproject.toml`, `uv.lock`, `package-lock.json`, `Cargo.lock`, `go.sum`, `.gitignore`, `CLAUDE.md`, `AGENTS.md`, and paths under `.gemini/`, `.claude/`, `.github/`, `scripts/`, `src/matrix_agent/templates/`
- **CI feedback**: Uses `⚠️` prefix (not `🤖`) to avoid being filtered by backfill logic
- **Tool usage footer**: Appended to all completion comments for observability

## Gotchas

- macOS `/tmp` symlinks to `/private/tmp` — use resolved paths for IPC
- E2EE not supported (libolm not installed)
- `self_update` tool runs on the VPS host, not inside the sandbox container
- Histories are in-memory but persisted to state.json after every agent reply
- Gemini CLI `@file` imports in GEMINI.md only support `.md` files; missing files log `[ERROR]` but don't stop execution
