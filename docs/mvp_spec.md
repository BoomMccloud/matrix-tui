# Matrix Agent MVP Spec

## Context
Build a self-hosted agentic coding assistant accessible via chat. Each conversation gets an isolated Podman container where the agent can write code, run commands, and take browser screenshots. The user interacts through Telegram (MVP) with a pluggable transport layer for future Matrix/Discord support.

## Design Decisions
- **Telegram for MVP transport.** Zero infrastructure — no homeserver, DNS, or TLS needed. One BotFather token and you're receiving messages. The bot layer is thin enough to swap later.
- **Subprocess over podman-py.** `podman exec` via subprocess is simpler, more debuggable, and avoids SDK quirks. Upgrade to `podman-py` later if warranted.
- **Playwright inside the container.** The sandbox image already has Node, so bundling Playwright there is self-contained. No host-side Chromium deps. Screenshots are copied out via `podman cp`.
- **Context is ephemeral.** Conversation history lives in-memory only. Lost on restart. Persistence is a future module.
- **Timeouts at two layers.** Command-level timeout on `podman exec` (hard kill). Agent-level max turns (prevent infinite tool loops).
- **Resource limits post-MVP.** Container `--memory` / `--cpus` constraints deferred but acknowledged.

## Architecture

```
Telegram App  <-->  Telegram Bot API  <-->  Bot (python-telegram-bot)
                                                    |
                                              Agent (LLM tool loop)
                                              [max_agent_turns cap]
                                                    |
                                              Podman Container
                                               (code sandbox)
                                            [command_timeout cap]
                                                    |
                                              Playwright (in-container)
```

## Stack
- **Python 3.12**, **uv** for package management
- **python-telegram-bot** — async Telegram bot SDK
- **podman** (CLI via subprocess) — container lifecycle
- **litellm** — abstract LLM provider (Claude, OpenAI, etc.)
- **Playwright** — headless browser screenshots (runs inside sandbox container)
- **pydantic-settings** — env-based configuration

## Project Structure

```
matrix-tui/
├── pyproject.toml
├── Containerfile                    # Sandbox image (node + python + playwright)
├── scripts/
│   ├── validate_podman.py           # Phase V1 validation
│   └── validate_screenshot.py       # Phase V2 validation
├── src/
│   └── matrix_agent/
│       ├── __init__.py
│       ├── __main__.py              # Entry point
│       ├── config.py                # Pydantic settings (env vars)
│       ├── sandbox.py               # Podman container lifecycle (subprocess)
│       ├── tools.py                 # Tool schemas + execution
│       ├── agent.py                 # LLM tool-calling loop
│       └── bot.py                   # Telegram bot
```

---

## Phase 1: Validation Scripts (do these first on VPS)

These validate the riskiest assumptions before building the full system.

### V1 — Podman CLI (`scripts/validate_podman.py`)

Standalone script that validates the core Podman workflow via subprocess:

1. `podman pull docker.io/library/python:3.12-slim`
2. `podman run -d` — start a detached container
3. `podman exec` — run `echo hello`, verify stdout capture
4. `podman exec` — run `python3 -c 'print(1+1)'`, verify output
5. Port-map a simple HTTP server and verify it's reachable from host
6. `podman stop` + `podman rm` — cleanup

### V2 — In-Container Screenshots (`scripts/validate_screenshot.py`)

Standalone script that validates Playwright works inside the container:

1. Build sandbox image from `Containerfile` (includes Playwright + Chromium)
2. Start container running `python3 -m http.server 8080`
3. `podman exec` — run Playwright inside the container to screenshot `localhost:8080`
4. `podman cp` — copy screenshot PNG to host
5. Verify PNG file exists and is >0 bytes

---

## Phase 2: Core Implementation

### Step 1: Project scaffolding
- `pyproject.toml` with deps: `python-telegram-bot`, `litellm`, `pydantic-settings`
- `Containerfile` — sandbox base image: Node.js 20 + Python 3.12 + Playwright + Chromium + git
- `config.py` — env-based settings:
  - `telegram_bot_token` — from BotFather
  - `llm_api_key`, `llm_model` (default: `claude-sonnet-4-20250514`)
  - `podman_path` (default: `podman`)
  - `sandbox_image` (default: `matrix-agent-sandbox:latest`)
  - `command_timeout_seconds` (default: `120`)
  - `max_agent_turns` (default: `25`)

### Step 2: Sandbox layer (`sandbox.py`)
`SandboxManager` class wrapping `podman` CLI via `asyncio.create_subprocess_exec`:
- `create(chat_id) -> str` — pull/build image, `podman run -d`, return container ID
- `exec(chat_id, command, timeout) -> (exit_code, stdout, stderr)` — `podman exec` with timeout
- `write_file(chat_id, path, content)` — pipe content via `podman exec sh -c 'cat > {path}'`
- `read_file(chat_id, path) -> str` — `podman exec cat {path}`
- `screenshot(chat_id, url) -> bytes` — `podman exec` Playwright screenshot + `podman cp` PNG out
- `get_host_port(chat_id, container_port) -> int` — parse `podman port`
- `destroy(chat_id)` — `podman stop` + `podman rm`

Internal state: `dict[str, str]` mapping chat_id to container_id.

### Step 3: Tools (`tools.py`)
JSON-schema tool definitions compatible with LiteLLM:
- `run_command(command: str) -> str` — dispatches to `sandbox.exec()`
- `write_file(path: str, content: str) -> str` — dispatches to `sandbox.write_file()`
- `read_file(path: str) -> str` — dispatches to `sandbox.read_file()`
- `take_screenshot(url: str) -> str` — dispatches to `sandbox.screenshot()`, returns base64

### Step 4: Agent loop (`agent.py`)
- LiteLLM `acompletion()` with tool definitions
- Loop: messages → response → execute tool_calls → append results → repeat
- **Terminates** when: LLM returns a text response (no tool calls), or `max_agent_turns` reached
- System prompt: coding assistant with container access, explain what you're doing
- Per-chat message list (in-memory, lost on restart)

### Step 5: Telegram bot (`bot.py`)
- `python-telegram-bot` async application
- `/start` command → create sandbox container, reply with greeting
- On text message → pass to agent loop → send text replies
- On agent screenshot → send as photo via `send_photo()`
- Basic error handling: catch exceptions, send error message to user

### Step 6: Entry point (`__main__.py`)
- Load config, init SandboxManager, init bot with agent, `application.run_polling()`

---

## Future (post-MVP)
- **Matrix transport**: Conduit + Caddy + matrix-nio, swap in behind transport interface
- **Conversation persistence**: store message history (SQLite or files), reload on restart
- **Multi-chat isolation**: each Telegram chat = separate container (already designed for this)
- **Resource limits**: `--memory 512m --cpus 1` on sandbox containers
- **Auth**: restrict bot to allowed Telegram user IDs
- **Persistent storage**: bind-mount volumes per chat for code persistence

## Verification
1. Create Telegram bot via BotFather, get token
2. Set `TELEGRAM_BOT_TOKEN` and `LLM_API_KEY` env vars
3. Build sandbox image: `podman build -t matrix-agent-sandbox .`
4. `uv run python -m matrix_agent`
5. Open Telegram → message the bot
6. Send: "Create a simple Express server showing a neon green 'Hello Matrix' page, start it, and screenshot it"
7. Verify: bot responds, code runs in container, screenshot appears in chat
