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

## Commands

```bash
uv run python -m matrix_agent   # start the bot
uv run pytest tests/            # run tests
uv run ruff check src tests     # lint
podman build -t matrix-agent-sandbox:latest -f Containerfile .  # rebuild sandbox image
```
