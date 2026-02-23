# In-Container Coding Agents Spec

## Context

The Matrix Agent bot currently has 4 basic tools (run_command, write_file, read_file, take_screenshot) with the orchestrator LLM (Sonnet 4 via OpenRouter) doing all coding directly. This works for simple tasks but struggles with real codebases — the orchestrator burns through turns writing files one at a time.

We need to install dedicated coding agent CLIs inside the sandbox container so the orchestrator can delegate coding tasks to specialized tools. The orchestrator becomes a router, not a coder.

## Decision: Three Agents

| Agent | Role | Model | Auth env var |
|-------|------|-------|-------------|
| **Qwen Code** | Primary coder (70.6% SWE-bench) | MiniMax M2.5 | `MINIMAX_API_KEY` (mapped from `QWEN_API_KEY`) |
| **Aider** | Multi-file refactors, git workflow | Sonnet 4 via OpenRouter | `OPENROUTER_API_KEY` (mapped from `AIDER_API_KEY`) |
| **Gemini CLI** | Analysis, review, large context (1M tokens) | Gemini 2.5 Pro | `GEMINI_API_KEY` |

All three use API key auth only — no OAuth, no browser, fully headless.

## Architecture

```
Matrix Room → Bot → Orchestrator (Sonnet 4, OpenRouter)
                        │
                   picks backend based on task
                        │
              ┌─────────┼─────────┐
              │         │         │
         Qwen Code   Aider    Gemini CLI
         (container) (container) (container)
              │         │         │
         via podman exec, each reads env vars from container env
```

## When to Use Each

| Scenario | Backend | Why |
|----------|---------|-----|
| Write new feature | qwen | Highest benchmark accuracy |
| Bug fix in a specific file | qwen | Surgical, fast |
| Refactor across many files | aider | Best git integration, auto-commits |
| Add tests for existing code | aider | Understands patterns, iterative |
| Explain how code works | gemini | 1M context, reads entire repo |
| Large-scale migration | gemini | Needs to see everything at once |
| Review code changes | gemini | Full context for consistency check |

## Private Repo Access

The user tells the agent to clone a repo in natural language (e.g. "clone https://github.com/user/private-repo and fix the login bug"). The orchestrator uses `run_command` to `git clone`.

A GitHub token with `repo` scope (read+write) is passed into the container via env var. Git is configured to use it automatically — the agent just runs `git clone` and it works.

```
Host .env                    Container environment
GITHUB_TOKEN=ghp_xxx  →     GITHUB_TOKEN=ghp_xxx
```

Git credential helper set on container create:
```bash
git config --global url."https://${GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"
```

## Env Var Flow

```
Host .env                    Container environment
─────────                    ─────────────────────
QWEN_API_KEY=xxx      →     MINIMAX_API_KEY=xxx      (Qwen Code reads this)
GEMINI_API_KEY=xxx    →     GEMINI_API_KEY=xxx       (Gemini CLI reads this)
AIDER_API_KEY=xxx     →     OPENROUTER_API_KEY=xxx   (Aider reads this via litellm)
GITHUB_TOKEN=ghp_xxx  →     GITHUB_TOKEN=ghp_xxx     (git clone/push auth)
```

Keys live in host `.env` only. Passed as `-e` flags on `podman run`. Never written to disk inside the container.

## .env.example additions

```
# In-container coding agents
QWEN_API_KEY=                   # MiniMax API key
GEMINI_API_KEY=                 # Google AI API key
AIDER_API_KEY=                  # Reuse OPENROUTER_API_KEY value

# GitHub private repo access (needs repo read+write scope)
GITHUB_TOKEN=

# Timeout for coding agent tasks (30 min)
CODING_TIMEOUT_SECONDS=1800
```

---

## Phases

### Phase A — Containerfile + validate each agent standalone

**Goal:** Confirm each agent installs correctly and runs non-interactively inside the container before touching any bot code.

**Changes:**
- Update `Containerfile` to install Qwen Code, Gemini CLI, Aider
- Add `scripts/validate_agents.py` — standalone validation script

**Validation script steps:**
1. Start a sandbox container manually
2. Test Qwen Code: `podman exec {cid} qwen-code --api-key $MINIMAX_API_KEY -p "print hello world in python" --no-input`
3. Test Gemini CLI: `podman exec {cid} gemini --api-key $GEMINI_API_KEY -p "say hello"`
4. Test Aider: `podman exec {cid} aider --model openrouter/anthropic/claude-sonnet-4 --message "create hello.py" --yes --no-git`
5. Verify each produces output and exits cleanly
6. Stop and remove container

**Pass criteria:** All three agents respond and exit without hanging.

**Fallback flags to try if default flags fail:**
- Qwen: `--headless`, `--yes`, `-y`
- Gemini: `--no-browser`, `--key`
- Aider: already well-documented, `--yes --no-git` should work

---

### Phase B — env var passthrough + git auth

**Goal:** Confirm API keys reach the container and git auth works.

**Changes:**
- Update `config.py` — add `qwen_api_key`, `gemini_api_key`, `aider_api_key`, `github_token`, `coding_timeout_seconds`
- Update `sandbox.py` — pass env vars as `-e` flags in `create()`, set git credential helper after container starts

**Validation:**
1. Start container via `sandbox.create()`
2. `podman exec {cid} printenv MINIMAX_API_KEY` — verify key is present
3. `podman exec {cid} printenv GEMINI_API_KEY` — verify key is present
4. `podman exec {cid} printenv OPENROUTER_API_KEY` — verify key is present
5. `podman exec {cid} git clone https://github.com/BoomMccloud/matrix-tui.git` — verify private repo clones

**Pass criteria:** All env vars present, repo clones successfully.

---

### Phase C — `code` tool + orchestrator routing

**Goal:** Wire the agents into the bot as a `code` tool the orchestrator can call.

**Changes:**
- Update `sandbox.py` — add `code(chat_id, task, backend)` method
- Update `tools.py` — add `code` tool schema + dispatch
- Update `agent.py` — update system prompt with routing guidance

**Validation (via Matrix chat):**
1. Invite bot to a new unencrypted room
2. Send: "Use qwen to write a Python function that checks if a number is prime"
3. Verify Qwen Code runs and returns code
4. Send: "Use gemini to explain what files are in /workspace"
5. Verify Gemini CLI runs and returns explanation
6. Send: "Use aider to add a docstring to the prime function"
7. Verify Aider runs and modifies the file
8. Send: "Fix the prime function" (no backend specified)
9. Verify orchestrator picks qwen automatically

**Pass criteria:** All four tests pass. Orchestrator routes correctly without explicit backend instruction.

---

### Phase D — private repo end-to-end

**Goal:** Confirm full workflow on a real private repo.

**Validation:**
1. Invite bot to a new room
2. Send: "Clone https://github.com/BoomMccloud/matrix-tui and show me the file structure"
3. Verify repo clones and gemini describes the structure
4. Send: "Add a comment to the top of sandbox.py explaining what it does"
5. Verify aider edits the file
6. Send: "Push the changes"
7. Verify git push succeeds

**Pass criteria:** Clone, edit, push all work without manual credential input.

---

## Workspace File Layout

All coordination files live in `/workspace` alongside the cloned repo:

```
/workspace/
├── task.md         # Current task (written by orchestrator, read by agents)
├── status.md       # Agent status log (each agent appends what it did)
├── context.md      # Persistent learnings and conventions across tasks
└── <repo files>    # Cloned repository
```

**task.md** — orchestrator writes the current task here before invoking any agent. Agents read it instead of receiving the task as a CLI argument. Solves shell escaping entirely.

**status.md** — append-only log. Each agent appends its name, what it did, and current state. Orchestrator reads this to summarize back to the Matrix room.

**context.md** — persists across tasks. Agents append discovered conventions, gotchas, and patterns. Future agents read it to avoid repeating mistakes and to match existing code style.

**Flow:**
```
Orchestrator writes task.md: "Add a login endpoint"

Gemini reads task.md + codebase, appends to status.md:
  [gemini] Auth is in src/middleware/auth.js. No conflicts. Ready.

Qwen reads task.md + status.md, implements, appends:
  [qwen] Created src/routes/login.js. Tests passing.

Orchestrator reads status.md → summarizes → sends to Matrix room.
```

**Agent invocation with task file:**
```bash
# Instead of: qwen-code -p "add a login endpoint --with 'quotes' that break"
# Write task to file, agent reads it:
podman exec {cid} sh -c 'qwen-code --task-file /workspace/task.md --no-input'
```

## Risks

- **Qwen Code non-interactive flags** — CLI is new, flags may differ. Validate in Phase A before assuming.
- **Shell escaping** — task strings with quotes break `sh -c`. Solution: write task to a temp file, pass file path instead.
- **Aider install size** — adds ~200MB Python deps. Acceptable.
- **Gemini CLI cold start** — first invocation may take 5-10s to initialize. Normal.
