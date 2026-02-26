# Multi-Agent Routing — Gemini for Planning, Qwen for Implementation

> **Status: Implemented.** See commit `cb4af59`.

## Context

The orchestrator previously routed all coding tasks to Gemini CLI via a single `code` tool. This change split it into three tools with two agents:

- **Gemini CLI** (1M context) — planning, analysis, code review, explaining codebases
- **Qwen Code** — implementation, bug fixes, refactoring, writing code

The orchestrator decides which agent to call based on the task. This is a routing change only — no tmux, no new IPC, no structural changes to the agent loop.

## Changes

### 1. Containerfile — install Qwen Code

Add after the Gemini CLI install line:

```dockerfile
RUN npm install -g @qwen/qwen@latest
```

### 2. sandbox.py — parameterize CLI binary

Replace the hardcoded `"gemini"` binary in `code_stream()` and `code()` with a parameter.

**Current:**
```python
async def code_stream(self, chat_id, task, on_chunk, chunk_size=800):
    ...
    proc = await asyncio.create_subprocess_exec(
        self.podman, "exec", "--workdir", "/workspace", name,
        "gemini", "-p", task,
        ...
    )
```

**New:**
```python
async def code_stream(self, chat_id, task, on_chunk, cli="gemini", chunk_size=800):
    ...
    proc = await asyncio.create_subprocess_exec(
        self.podman, "exec", "--workdir", "/workspace", name,
        cli, "-p", task,
        ...
    )
```

Same change for `code()`. The `cli` parameter defaults to `"gemini"` for backward compatibility.

**Note:** Verify that Qwen Code accepts `-p` for prompt mode. If it uses a different flag, add a lookup:
```python
CLI_FLAGS = {"gemini": ["-p"], "qwen": ["-p"]}  # adjust as needed
```

### 3. tools.py — split into three tools

Remove the `code` tool. Add three new tools:

**`plan`** — Gemini CLI for planning and analysis:
```python
{
    "name": "plan",
    "description": (
        "Ask Gemini CLI to plan, analyze, or explain. Use for: writing implementation plans, "
        "analyzing codebases, first-principles thinking, checking if a solution is the simplest approach. "
        "Gemini has 1M token context and can read entire repos."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "What to plan or analyze. Be specific about goals and constraints."
            }
        },
        "required": ["task"]
    }
}
```

**`implement`** — Qwen Code for writing code:
```python
{
    "name": "implement",
    "description": (
        "Ask Qwen Code to write or modify code. Use for: implementing features, fixing bugs, "
        "refactoring, writing tests. Pass the plan or requirements in the task description."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "What to implement. Include the plan, specific files, and acceptance criteria."
            }
        },
        "required": ["task"]
    }
}
```

**`review`** — Gemini CLI for code review:
```python
{
    "name": "review",
    "description": (
        "Ask Gemini CLI to review code changes. Use after implementation to check for bugs, "
        "security issues, missed edge cases, and adherence to project conventions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "What to review. Reference specific files or describe what changed."
            }
        },
        "required": ["task"]
    }
}
```

**Dispatch in `execute_tool()`:**
```python
if name == "plan":
    rc, stdout, stderr = await sandbox.code_stream(chat_id, args["task"], send_update, cli="gemini")
    ...

if name == "implement":
    rc, stdout, stderr = await sandbox.code_stream(chat_id, args["task"], send_update, cli="qwen")
    ...

if name == "review":
    rc, stdout, stderr = await sandbox.code_stream(chat_id, args["task"], send_update, cli="gemini")
    ...
```

### 4. agent.py — update system prompt

Replace the current `code` tool instructions with:

```
You have three coding agents available:

- plan(task) — Gemini CLI (1M context). Use for planning, analysis, and explaining codebases.
- implement(task) — Qwen Code. Use for writing code, fixing bugs, and refactoring.
- review(task) — Gemini CLI. Use after implementation to review changes.

Typical workflow:
1. plan() — understand the codebase and design the approach
2. implement() — write the code, passing the plan as context
3. run_tests() — verify lint and tests pass
4. review() — check for bugs, security issues, missed edge cases
5. If review finds issues, implement() again with the feedback

Always pass enough context between agents. The orchestrator carries conversation history,
but each agent invocation is independent — include the plan in the implement() task,
and describe what changed in the review() task.
```

### 5. Qwen Code auth config

Qwen Code requires `~/.qwen/settings.json` for auth. Written by `_init_workspace()` in sandbox.py. Uses DashScope international endpoint with `DASHSCOPE_API_KEY` env var:

```json
{
  "modelProviders": {
    "openai": [{
      "id": "qwen3-coder-next",
      "name": "qwen3-coder-next",
      "baseUrl": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
      "envKey": "DASHSCOPE_API_KEY"
    }]
  },
  "security": { "auth": { "selectedType": "openai" } },
  "model": { "name": "qwen3-coder-next" }
}
```

## What Does NOT Change

- **Agent loop** (agent.py) — same tool-calling loop, same max turns
- **Bot** (bot.py) — same message routing, same IPC watcher, same streaming
- **State persistence** — same state.json, same history format
- **Container lifecycle** — same create/destroy, same one-per-room model
- **Notification hook** — still works, only applies to Gemini sessions

## Testing

```bash
# Unit + scenario tests
uv run pytest tests/

# Integration test (requires API keys in .env)
podman build -t matrix-agent-sandbox:latest -f Containerfile .
bash scripts/test-multi-agent.sh
```

The integration test verifies both CLIs are installed, auth is configured, and both respond to `-p` mode. It exits non-zero on failure — suitable for gating deploys.

## Future

Once this routing works:
1. **tmux persistent sessions** — both agents run in their own tmux session for context persistence and bidirectional comms (see `tmux_gemini_sessions.md`)
2. **Programming loop** — structured `/spec → /analyze → /verify → /go` workflow using the plan/implement/review tools (see `programming-loop-spec.md`)
