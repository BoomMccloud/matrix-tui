# Headless / Queue-Driven Execution Design — 2026-03-12

## Context
- Current modes: Matrix chat (MatrixChannel + Bot) and GitHub agent-task webhook (GitHubChannel). Both rely on Matrix presence and/or GitHub issues to drive TaskRunner → Decider → Sandbox workflow.
- Goal: allow matrix-tui to execute workflows without a Matrix room, triggered via CLI flags or a Redis-backed work queue, while preserving existing Matrix + GitHub behavior.

## Requirements & Assumptions
- Headless CLI invocation: `matrix-tui --headless --workflow <name> --payload <json|path>` runs exactly one job, no Matrix login.
- Queue mode: consume Redis list/stream (default keys: `matrix-tui:jobs`, `matrix-tui:results`), dispatch jobs, and publish results (and optional progress) back to Redis.
- Job schema carries workflow name, payload (json), correlation id, and optional result key/TTL. Defaults provided when missing.
- Keep Matrix/GitHub flows unchanged by default; headless is additive and opt-in via flags/env.
- Deliver minimal logging-friendly output for headless runs (JSON result), plus structured result payload in Redis.
- Assume adding `redis>=5` dependency is acceptable; use async client to match existing event loop.

### Open Questions (to confirm before implementation)
1) Redis primitive preference: list (BLPOP) vs stream (XREADGROUP) for backpressure/ack/replay?
2) Expected workflow vocabulary: are workflows free-form strings for the LLM prompt, or a fixed set (e.g., plan/implement/review) we should validate against?
3) Result payload shape: should updates be streamed separately (e.g., `matrix-tui:results:<id>:updates`) or appended to a single list/stream entry?
4) Default TTL for result keys (if any) and whether failures should also emit a retryable status.

## Current Architecture (relevant pieces)
- `TaskRunner` manages per-task queues, ties `ChannelAdapter` implementations to the LLM `Decider`, and ensures sandbox containers exist per task id.
- `Decider` drives LiteLLM tool calls using `SYSTEM_PROMPT`; `ChannelAdapter.system_prompt` can override per source (Matrix uses it; GitHub bypasses).
- `SandboxManager` handles container lifecycle and persists histories/state.
- `Bot` + `MatrixChannel` own Matrix login/message handling. `GitHubChannel` exposes webhook + crash recovery for `agent-task` issues.

## Approaches Considered
1) **Add Headless/Redis channels + CLI mode (recommended)**
   - Extend runtime to support a `HeadlessChannel` (CLI) and `RedisChannel` (queue) that plug into `TaskRunner` with their own system prompt and result sinks. Add CLI args/env to pick mode.
   - Pros: Reuses TaskRunner/Sandbox/Decider, single source of truth for execution, predictable logging. Additive to existing modes.
   - Cons: Introduces redis dependency and more runtime branches in `__main__`.

2) **Standalone headless runner bypassing TaskRunner**
   - Directly call `Decider.handle_message` with a stub channel and skip Sandbox/TaskRunner plumbing.
   - Pros: Minimal code for single-shot CLI.
   - Cons: Duplicates container setup/state handling, diverges from queue path, higher risk of drift from main runtime.

3) **External worker script (out-of-repo) that shells into matrix-tui**
   - Keep repo unchanged; use external script to enqueue/dequeue Redis jobs and invoke matrix-tui via Matrix/GitHub flows.
   - Pros: Zero code change here.
   - Cons: Fails the goal (requires Matrix/GitHub presence), adds ops drift, no shared tests.

## Proposed Design (Option 1)

### Settings / CLI
- Add argparse to `__main__.py` (or a small launcher) with `--mode` choices: `matrix` (default), `headless`, `redis-worker`.
- New Settings fields (env-backed):
  - `headless_mode: bool = False` (true for CLI single-run)
  - `headless_workflow: str = ""`
  - `headless_payload_path: str = ""` (path to JSON/YAML)
  - `headless_payload_inline: str = ""` (raw JSON string)
  - `headless_correlation_id: str = ""` (default: uuid4)
  - `redis_url: str = "redis://localhost:6379/0"`
  - `redis_jobs_key: str = "matrix-tui:jobs"`
  - `redis_results_key: str = "matrix-tui:results"`
  - `redis_mode: str = "list"` (enum: list|stream)
  - `redis_stream_group: str = "matrix-tui"` (for stream)
  - `redis_stream_consumer: str = host-pid default` (for stream)
  - `redis_result_ttl_seconds: int = 0` (0 = no TTL)
  - `headless_update_channel: str = ""` (optional Redis key for progress)
- CLI flags override env values; `--payload` may accept inline JSON or `@path` shorthand.

### Job Schema (queue and CLI)
```json
{
  "workflow": "<string>",
  "payload": {"any": "json"},
  "correlation_id": "<string>",
  "result_key": "matrix-tui:results",   // optional override
  "update_key": "matrix-tui:results:<id>:updates", // optional
  "artifacts": ["logs", "summary"]      // optional hints
}
```
- If `correlation_id` missing, generate uuid4. Task ids will be `headless-<correlation_id>` to keep sandbox isolation.
- CLI mode builds the same schema from flags.

### Prompt Shaping
- Add `HEADLESS_SYSTEM_PROMPT` emphasizing non-interactive execution: consume `workflow` + `payload`, do not ask questions, return concise summary + key outputs. Include payload JSON in the user message for context.
- Message template for Decider:
  - User content: `Workflow: <workflow>\nPayload (JSON):\n<pretty json>\nRequirements: non-interactive, return result summary + any artifacts requested.`
  - System prompt: `HEADLESS_SYSTEM_PROMPT` (via channel.system_prompt).

### Channels / Runners
- **HeadlessCliChannel** (`ChannelAdapter`):
  - `send_update`: buffer updates for optional verbose stdout.
  - `deliver_result/error`: emit structured JSON to stdout `{correlation_id, status, result, elapsed, updates}`; set exit code !=0 on error.
  - `is_valid`: always True (single-shot).
- **RedisChannel** (`ChannelAdapter`):
  - ctor takes redis client + result/update keys + ttl.
  - `send_update`: LPUSH/RPUSH (list) or XADD (stream) to `update_key` when provided.
  - `deliver_result/error`: publish JSON to `result_key` or `result_key:<id>`; set EX when ttl>0.
  - `is_valid`: always True for queued jobs (queue owns lifecycle).
- **RedisWorker** (new module):
  - Connect via `redis.asyncio.from_url`.
  - Loop: read job (BLPOP for list; XREADGROUP for stream), parse/validate schema, build task_id, select channel, and `await task_runner.enqueue(task_id, message, channel)`.
  - Ack (stream) after completion; for list mode, job is consumed on BLPOP. On parse error, publish error result and ack/trim accordingly.
  - Honor SIGINT/SIGTERM to stop intake, wait for queues to drain, call `task_runner.shutdown()`.

### Execution Flows
- **CLI headless**: parse args → load payload → instantiate Settings/Sandbox/Decider/TaskRunner → create HeadlessCliChannel → enqueue single message → wait for completion → shutdown sandbox → exit.
- **Redis worker**: start RedisWorker + TaskRunner + optional GitHubChannel? (disabled in this mode) → continuously dispatch jobs → on shutdown, stop accepting new jobs and call `task_runner.shutdown()` and close Redis connections.

### Logging / Metrics
- Log correlation_id, workflow, and queue offset per job.
- Emit durations in result payload and logs. Keep existing logging format.

### Backward Compatibility
- Default behavior (no flags) remains Matrix + GitHub startup exactly as today.
- Headless/Redis code paths are opt-in and do not affect Matrix/GitHub settings.

### Testing Strategy
- Add unit tests with `fakeredis` for RedisWorker/RedisChannel (list + stream modes) covering job parsing, result emission, ack behavior, and error handling for malformed jobs.
- Test HeadlessCliChannel end-to-end with a stub Decider/TaskRunner (or small integration using a fake Decider) to ensure stdout payload structure.
- Regression: ensure Matrix/GitHub startup unaffected when no headless flags are set (e.g., `test_main_default_mode`).
- Validate prompt shaping: assert the headless message template includes workflow + payload and uses the headless system prompt.

### Documentation
- Add README section: headless CLI usage, job schema, Redis configuration, example `redis-cli` enqueue/dequeue, and expected result payloads.
- Include sample commands for list and stream modes, and guidance on TTL/cleanup.
