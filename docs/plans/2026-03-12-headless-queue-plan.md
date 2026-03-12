# Headless / Queue-Driven Execution Mode Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a headless execution path that can run matrix-tui workflows via CLI or a Redis-backed queue without requiring Matrix rooms, while keeping Matrix/GitHub modes intact.

**Architecture:** Introduce headless-specific settings, prompts, and channel adapters that plug into the existing TaskRunner/Sandbox/Decider stack. Provide a CLI entrypoint and a Redis worker loop that translate jobs into Decider messages and publish structured results.

**Tech Stack:** Python 3.12, asyncio, aiohttp, redis (redis.asyncio), pydantic-settings, pytest/pytest-asyncio, fakeredis.

---

### Task 1: Add settings and CLI mode selection

**Files:**
- Modify: `src/matrix_agent/__main__.py`
- Modify: `src/matrix_agent/config.py`
- Test: `tests/test_config.py`, `tests/test_shutdown.py` (or new `tests/test_main_default_mode.py`)

**Step 1: Write failing tests for new settings defaults and CLI arg parsing**
- Add tests asserting new env-backed settings (headless/redis fields) default values and that CLI `--mode` is required for non-default paths.

**Step 2: Implement argparse wiring and new Settings fields**
- Extend `Settings` with headless + redis options; parse CLI args (mode + payload/workflow/correlation-id) and pass into Settings/init flow.

**Step 3: Run tests to confirm parsing and defaults**
- Run: `pytest tests/test_config.py tests/test_shutdown.py -k headless -v`

**Step 4: Commit**
- `git add src/matrix_agent/config.py src/matrix_agent/__main__.py tests/...`
- `git commit -m "feat: add headless mode settings and CLI parsing"`

---

### Task 2: Headless prompt + channel scaffolding (CLI)

**Files:**
- Create: `src/matrix_agent/headless.py` (Headless system prompt + message builder + channel classes)
- Modify: `src/matrix_agent/decider.py` (export headless prompt constant if needed)
- Modify: `src/matrix_agent/__init__.py` (optional exports)
- Test: `tests/test_headless_channel.py`

**Step 1: Write failing tests for HeadlessCliChannel output and message construction**
- Tests should ensure `HeadlessCliChannel.deliver_result` emits JSON with correlation id/status and that the message builder includes workflow + payload.

**Step 2: Implement headless prompt/message builder and HeadlessCliChannel**
- Add HEADLESS_SYSTEM_PROMPT, message template, and CLI channel implementing `ChannelAdapter` methods (updates buffered/printed, exit codes on error).

**Step 3: Run tests**
- `pytest tests/test_headless_channel.py -v`

**Step 4: Commit**
- `git add src/matrix_agent/headless.py src/matrix_agent/decider.py tests/test_headless_channel.py`
- `git commit -m "feat: add headless prompt and CLI channel"`

---

### Task 3: Redis worker + channel

**Files:**
- Modify/Create: `src/matrix_agent/headless.py` (RedisChannel + RedisWorker)
- Modify: `pyproject.toml` (add redis, fakeredis to dev extras)
- Test: `tests/test_headless_redis.py`

**Step 1: Write failing tests using `fakeredis` for list + stream modes**
- Cover job parsing, correlation id generation, result/update publishing, and stream ack behavior.

**Step 2: Implement RedisChannel and RedisWorker**
- Async Redis client hookup, job schema validation, BLPOP/XREADGROUP loops, ack/ttl handling, graceful shutdown hooks.

**Step 3: Run tests**
- `pytest tests/test_headless_redis.py -v`

**Step 4: Commit**
- `git add src/matrix_agent/headless.py pyproject.toml tests/test_headless_redis.py`
- `git commit -m "feat: add redis-backed headless worker"`

---

### Task 4: Wire modes into runtime

**Files:**
- Modify: `src/matrix_agent/__main__.py`
- Modify: `tests/test_integration.py` (or new targeted test for default/no-headless path)

**Step 1: Write failing tests asserting default Matrix/GitHub startup remains unchanged when mode is omitted**
- Ensure headless/redis paths are opt-in and that existing behavior continues to instantiate Bot/GitHubChannel.

**Step 2: Implement mode dispatch**
- In `__main__.py`, route to Matrix/Bot (default), Headless CLI single-run, or Redis worker based on parsed mode.

**Step 3: Run tests**
- `pytest tests/test_integration.py -k headless -v`

**Step 4: Commit**
- `git add src/matrix_agent/__main__.py tests/test_integration.py`
- `git commit -m "feat: add runtime dispatch for headless and redis modes"`

---

### Task 5: Documentation and examples

**Files:**
- Modify: `README.md`
- Create: `docs/plans/2026-03-12-headless-queue-design.md` (already) reference; add usage section
- Test: n/a

**Step 1: Document CLI and Redis usage with sample commands and job schema**
- Add examples for enqueueing via `redis-cli`, consuming results, and single-shot CLI invocation.

**Step 2: Spot-check rendering**
- `grep`/`python -m markdown` if available (manual check).

**Step 3: Commit**
- `git add README.md`
- `git commit -m "docs: document headless/queue modes"`

---

### Task 6: Final verification

**Files:**
- n/a (commands)

**Step 1: Run full test suite**
- `pytest -v`

**Step 2: Commit/push if clean**
- `git status`
- `git push -u origin headless-queue-design`

**Step 3: Open PR**
- `gh pr create --title "feat: add headless/queue execution modes" --body "Ref: openclaw/nisto-home#282"`
