# Architecture

## Terminology

| Name | What it is | Where it runs |
|---|---|---|
| **Channel adapter** | Thin I/O skin. Receives events from an external service, delivers output back. Matrix bot (`bot.py`), GitHub webhooks (`channels.py`). No business logic — just I/O translation. | Host (bot process) |
| **Task runner** | Pure execution lifecycle layer. No LLM. Creates container, manages queue, clones repo, runs git/PR steps, posts comments, handles cleanup. Deterministic code paths only. Also runs the PR evaluator as a post-task step. One task runner per room/issue. Currently mixed into `bot.py` — will be separated when GitHub support is added. | Host (bot process) |
| **Decider** | LLM-enabled routing loop (`decider.py`, was `agent.py`). Haiku decides which tool to call next (plan, implement, review, run_command, etc.). Maintains per-chat conversation history. Loops until done or max turns. This is the only component that loops with LLM judgment. | Host (bot process) |
| **Coding agent** | Gemini CLI or Qwen CLI running inside the container. Does the actual code reading and writing. Invoked by the decider via the `code`/`plan`/`implement`/`review` tools. Each invocation is independent — no persistent state between calls. | Container |
| **PR evaluator** | A one-shot cheap LLM call (Haiku) that runs after the decider finishes a GitHub task. Given the diff and issue description, it decides whether to create a PR and generates the commit message, PR title, and PR body. Called by the task runner. | Host (bot process) |

## Component Interaction

```
User Message (Matrix or GitHub)
  │
  ▼
┌─────────────────────────────────────────────────┐
│  Channel Adapter (bot.py / channels.py)         │
│  - Receives events, translates to internal form │
│  - Delivers output back to the channel          │
│                                                 │
│  ▼                                              │
│  Task Runner (currently in bot.py)              │
│  - One per room (Matrix) or issue (GitHub)      │
│  - asyncio.Queue for incoming messages          │
│  - Processes messages serially                  │
│  - Creates container on first message (lazy)    │
│  - Owns lifecycle: clone, git, PR, cleanup      │
│                                                 │
│  Calls:                                         │
│  ┌─────────────────────────────────────────┐    │
│  │  Decider (decider.py)                   │    │
│  │  - LLM tool-calling loop (Haiku)        │    │
│  │  - Maintains conversation history       │    │
│  │  - Decides which tools to call          │    │
│  │  - Yields (text, image) replies         │    │
│  │                                         │    │
│  │  Calls tools via execute_tool():        │    │
│  │  ┌─────────────────────────────┐        │    │
│  │  │  Tools (tools.py)           │        │    │
│  │  │  - run_command              │        │    │
│  │  │  - read_file / write_file   │        │    │
│  │  │  - plan (→ Gemini CLI)      │        │    │
│  │  │  - implement (→ Qwen CLI)   │        │    │
│  │  │  - review (→ Gemini CLI)    │        │    │
│  │  │  - run_tests                │        │    │
│  │  │  - take_screenshot          │        │    │
│  │  │  - self_update              │        │    │
│  │  └──────────┬──────────────────┘        │    │
│  │             │                           │    │
│  └─────────────┼───────────────────────────┘    │
│                │                                │
│  ┌─────────────▼───────────────────────────┐    │
│  │  Container (sandbox.py)                 │    │
│  │  - Podman container per room/issue      │    │
│  │  - /workspace mount point               │    │
│  │  - Node.js 20, Python 3, git, gh CLI    │    │
│  │  - Gemini CLI + Qwen CLI installed      │    │
│  │  - GITHUB_TOKEN, API keys in env        │    │
│  │  - IPC via /workspace/.ipc/ files       │    │
│  └─────────────────────────────────────────┘    │
│                                                 │
│  (GitHub only, after decider finishes):         │
│  ┌─────────────────────────────────────────┐    │
│  │  PR Evaluator                           │    │
│  │  - One-shot Haiku call                  │    │
│  │  - Input: git diff + issue description  │    │
│  │  - Output: create_pr bool, commit msg,  │    │
│  │    PR title, PR body                    │    │
│  │  - Task runner then runs git commands   │    │
│  │    via sandbox.exec() deterministically │    │
│  └─────────────────────────────────────────┘    │
└─────────────────────────────────────────────────┘
```

## The Hierarchy

```
Channel Adapter (I/O)
  → Task Runner (pure execution, lifecycle)
    → Decider (LLM loop, decides what to do)
      → Coding Agents (Gemini/Qwen, do the work)
    → PR Evaluator (one-shot LLM, post-task)
```

## How the Pieces Connect

### 1. Task Runner → Decider

The task runner calls `decider.handle_message(chat_id, text, send_update)` which is an async generator. The decider runs its LLM loop internally and yields `(text, image)` tuples back to the task runner. The task runner passes these to the channel adapter for delivery (Matrix message or GitHub comment).

```python
# bot.py — Matrix task runner (channel adapter + task runner currently mixed)
async for reply_text, image in self.decider.handle_message(room_id, text, send_update):
    if reply_text:
        await self.client.room_send(room_id, ..., reply_text)
    if image:
        await self._send_image(room_id, image)
```

The `send_update` callback lets the decider stream intermediate output (coding agent progress) back through the task runner to the channel adapter. For Matrix, this sends messages. For GitHub, this is a no-op (only final status is posted).

### 2. Decider → Tools → Container

The decider calls `execute_tool(sandbox, chat_id, tool_name, arguments)` for each tool the LLM requests. Most tools delegate to `sandbox.exec(chat_id, command)` which runs a command inside the container via `podman exec`.

The coding agent tools (`plan`, `implement`, `review`) invoke Gemini CLI or Qwen CLI inside the container. These are long-running processes that produce IPC events.

### 3. Container → Host (IPC)

Gemini CLI has hooks configured in `/workspace/.gemini/settings.json`:

| Hook | Fires when | IPC file written | Purpose |
|---|---|---|---|
| **AfterAgent** | Gemini CLI exits | `event-result.json` | Signals completion with exit code |
| **AfterTool** | Each Gemini tool finishes | `event-progress.json` | Tracks tool-by-tool progress |
| **Notification** | Gemini needs approval | `notification.json` | Surfaces permission requests to user |

IPC files are written to `/workspace/.ipc/` inside the container (host-accessible via volume mount). The task runner polls this directory every 1 second via `_watch_ipc()`, reads + deletes each file, and formats the event as a Matrix message.

Qwen CLI has no native hook support — a wrapper script (`/workspace/.qwen-wrapper.sh`) captures output and writes `event-result.json` on completion.

### 4. Task Runner Lifecycle

**Matrix task runners** live as long as the room exists. They block on `queue.get()` when idle. Cleanup happens when the bot is kicked, the last user leaves, or the reconcile loop finds an orphaned container.

**GitHub task runners** live as long as the issue is open and labeled `agent-task`. Termination triggers:
- Issue closed → `issues` webhook with `action: "closed"` → sentinel pushed to queue → task runner breaks
- Label removed → `issues` webhook with `action: "unlabeled"` → same
- Reconcile job catches anything webhooks missed

### 5. GitHub Task Runner Flow

```
Issue labeled "agent-task"
  → Webhook arrives (channel adapter)
  → Check _processing set (idempotency)
  → Create queue + task runner
  → Task runner posts "🤖 Working on this issue..." comment
  → Create container, clone repo
  → Build initial context (issue title + body + comments)
  → Push to queue, decider processes it
  → Decider calls plan/implement/review/run_tests
  → handle_message() finishes
  → PR Evaluator: one-shot Haiku call with diff + issue description
    → If changes exist and are valid: generate commit msg, PR title, PR body
    → Task runner runs git branch/add/commit/push/PR via sandbox.exec()
    → Post "✅ Completed — PR: {url}" comment
    → If no changes or invalid: post "⚠️ No PR created: {reason}"
  → On exception: post "❌ Failed: {error}" comment
  → Task runner blocks on queue.get() waiting for follow-up comments
  → On sentinel (issue closed/unlabeled): task runner exits, cleanup
```

### 6. Conversation History

The decider maintains per-chat conversation history in memory (`decider._histories`). This is a list of message dicts (system, user, assistant, tool). History persists across messages within a session — the decider sees full context of prior exchanges.

Histories are saved to `state.json` (via `sandbox.save_state()`) after every decider reply and restored on startup via `sandbox.load_state()` + `decider.load_histories()`.

### 7. Multi-Agent Routing

The decider (Haiku) acts as a router. It doesn't write code itself — it decides which coding agent to invoke:

| Tool | Routes to | Use case |
|---|---|---|
| `plan(task)` | Gemini CLI (1M context) | Planning, analysis, codebase understanding |
| `implement(task)` | Qwen CLI | Writing code, fixing bugs, refactoring |
| `review(task)` | Gemini CLI | Code review, finding bugs and edge cases |

Each coding agent invocation is independent. The decider passes context between them via the task description (e.g., including the plan output in the implement task).

## File Mapping

| File | Role |
|---|---|
| `bot.py` | Channel adapter (Matrix) + task runner (currently mixed, will be separated) |
| `decider.py` (was `agent.py`) | Decider — LLM routing loop |
| `channels.py` | Channel adapter (GitHub) |
| `tools.py` | Tool schemas and dispatch |
| `sandbox.py` | Container management |
