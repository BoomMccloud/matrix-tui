# Spec: Shift GitHub Issue Orchestration into Gemini CLI

## Context

The bot currently uses a Python LLM routing loop (Gemini Flash via LiteLLM) to decide what tool to call each turn. This results in the router bypassing `plan`/`implement`/`review` tools in favor of `run_command`, weak context passing between agent invocations (each is a separate subprocess), and no enforcement of the review feedback loop.

**Goal:** For GitHub issues, replace the multi-turn Python router with a single long-running Gemini CLI session that handles the full workflow internally. The Python host becomes a launcher + monitor that provides complementary guardrails.

## What Changes

### Layer 1: Inside the Container (Gemini CLI orchestrates)

**New files written by `_init_workspace()` in `sandbox.py`:**

#### Custom Command: `/fix-issue`
**Path:** `/workspace/.gemini/commands/fix-issue.toml`

A templated slash command that Gemini CLI executes as a structured prompt. Encodes the deterministic workflow:
1. Run `/init` in the cloned repo to generate project GEMINI.md
2. Read `.baseline-tests.txt` for test baseline
3. Plan: analyze codebase, identify files, design approach
4. **Generate acceptance criteria**: write testable criteria to `/workspace/.ipc/acceptance-criteria.md` based on the issue requirements and codebase understanding
5. Implement: use the `delegate-qwen` skill for code changes
6. Test: `ruff check . && pytest -v` (or project-appropriate commands)
7. If tests fail, fix and re-test (max 3 attempts)
8. Review: `git diff`, verify correctness/security/scope, check each acceptance criterion is met
9. If review finds issues, fix and re-test
10. Create PR: branch, commit, push, `gh pr create`, write PR URL to `/workspace/.ipc/pr-url.txt`

Uses `{{args}}` for issue context injection.

PR creation steps are inlined in the command (not a separate skill) since they execute exactly once at the end of the workflow.

#### Custom Command: `/fix-ci`
**Path:** `/workspace/.gemini/commands/fix-ci.toml`

For reopened issues with CI failures:
1. Checkout existing PR branch
2. Read failure context
3. Fix, test, force-push

#### Skill: `delegate-qwen`
**Path:** `/workspace/.gemini/skills/delegate-qwen/SKILL.md`

Instructions for Gemini to invoke Qwen Code via `/workspace/.qwen-wrapper.sh "<task>"`. Tells Gemini when to use it (writing/modifying code) vs when not to (reading/planning). Emphasizes passing full plan context in the prompt.

#### Hook: `BeforeTool` safety guard
**Path:** `/workspace/.gemini/hooks/before-tool.sh`

Blocks `git push` without `--force` (exit code 2 = Gemini CLI blocks the tool). This moves the safety guard from Python into the container.

#### `AfterAgent` hook (unchanged from current)
**Path:** `/workspace/.gemini/hooks/after-agent.sh`

Keeps existing behavior: write IPC `event-result.json`, append timestamp to `status.md`. No quality gate — the host handles validation independently after Gemini exits. This avoids running `pytest` on every agent turn (latency cost) when the `/fix-issue` command already instructs Gemini to test, and the host validates anyway.

#### Updated `settings.json`
Add `BeforeTool` hook entry to existing hooks config.

#### Updated `.qwen-wrapper.sh`
Add inner timeout: `timeout ${QWEN_TIMEOUT:-1800} qwen -y -p "$1"` to prevent a single Qwen call from consuming the entire session budget.

### Layer 1.5: Template extraction

Move all container file contents (GEMINI.md, settings.json, hook scripts, skills, commands) out of `sandbox.py` into a flat `src/matrix_agent/templates/` directory as individual files. `_init_workspace()` reads from templates and writes to the container via a mapping dict:

```python
TEMPLATES = {
    "GEMINI.md": "/workspace/GEMINI.md",
    "status.md": "/workspace/status.md",
    "settings.json": "/workspace/.gemini/settings.json",
    "hook-session-start.sh": "/workspace/.gemini/hooks/session-start.sh",
    "hook-after-agent.sh": "/workspace/.gemini/hooks/after-agent.sh",
    "hook-after-tool.sh": "/workspace/.gemini/hooks/after-tool.sh",
    "hook-notification.sh": "/workspace/.gemini/hooks/notification.sh",
    "hook-before-tool.sh": "/workspace/.gemini/hooks/before-tool.sh",
    "cmd-fix-issue.toml": "/workspace/.gemini/commands/fix-issue.toml",
    "cmd-fix-ci.toml": "/workspace/.gemini/commands/fix-ci.toml",
    "skill-delegate-qwen.md": "/workspace/.gemini/skills/delegate-qwen/SKILL.md",
    "qwen-wrapper.sh": "/workspace/.qwen-wrapper.sh",
    "qwen-settings.json": "/root/.qwen/settings.json",
}
```

This keeps `sandbox.py` focused on infrastructure, makes templates easy to edit/review independently, and avoids deep nesting (all templates in one flat directory with descriptive prefixed names).

### Layer 2: Host (Python monitors + validates)

The host does what Gemini CLI **cannot** do for itself, and independently validates Gemini's work ("measure twice, cut once").

#### `sandbox.py` — Add `workdir` parameter to `code_stream()`

The existing `code_stream()` method hardcodes `--workdir /workspace`. Add an optional `workdir` parameter (default `/workspace`) so the GitHub pipeline can pass `--workdir /workspace/<repo>`. This is needed because Gemini CLI loads GEMINI.md from cwd — it must run from the repo directory, not `/workspace`.

```python
async def code_stream(self, chat_id, task, on_chunk, cli="gemini",
                      chunk_size=800, auto_accept=False, workdir="/workspace"):
```

#### `sandbox.py` — New method: `run_gemini_session()`

Thin wrapper around `code_stream()` — does NOT duplicate subprocess/streaming/timeout logic:
1. Calls `code_stream(chat_id, prompt, on_chunk, cli="gemini", auto_accept=True, workdir=f"/workspace/{repo_name}")`
2. After `code_stream()` returns, reads IPC files from host-side IPC dir:
   - `pr-url.txt` — source of truth for success
   - `acceptance-criteria.md` — for retry feedback
3. Returns `(exit_code, stdout, pr_url: str | None)`

#### `sandbox.py` — New method: `validate_work()`
After Gemini exits, the host independently checks the work:
1. `sandbox.exec()` → `cd <repo> && ruff check . && pytest -v` — tests must pass
2. `sandbox.exec()` → `git diff --stat HEAD~1` — check scope (files changed, lines added/removed)
3. `sandbox.exec()` → check `pr-url.txt` exists
4. `sandbox.exec()` → check `acceptance-criteria.md` exists and is non-empty
5. Returns `(passed: bool, failures: list[str])` — list of specific failures

If validation fails, the host re-launches Gemini with targeted feedback:
```
"Host validation failed after your previous attempt:
- Tests failing: test_foo.py::test_bar FAILED (assert 1 == 2)
- Scope concern: 8 files changed but issue only asked for 2
- Acceptance criteria not met: You wrote criteria X but tests show it doesn't pass
Fix these issues, then create the PR."
```

Max 2 retry rounds before reporting failure to the channel.

**Source of truth:** The host treats **process exit code + pr-url.txt existence** as the single authority on "is the task done?". The `event-result.json` from the AfterAgent hook is telemetry, not the authority.

#### `core.py` — Route GitHub tasks to new pipeline

All GitHub pipeline logic lives in `core.py._process()` — not in `decider.py`. The Decider stays focused on the Matrix/LiteLLM path. `core.py` already has `task_id`, `message` (containing repo URL), and `channel`, so it can construct the prompt and call sandbox methods directly.

In `_process()`: if `task_id.startswith("gh-")`, use new pipeline instead of `decider.handle_message()`:
1. Ensure container exists
2. Parse repo URL and issue number from `message` (first line is `Repository: owner/repo`)
3. Clone repo via `sandbox.exec()`
4. Detect CI fix vs new issue (see channels.py change below)
5. Construct prompt: `/fix-issue <issue context>` or `/fix-ci <failure context>`
6. Call `sandbox.run_gemini_session()` with the prompt
7. **Host validation:** call `sandbox.validate_work()` — runs tests, checks scope, verifies PR, checks acceptance criteria
8. If validation fails, re-launch Gemini with feedback (up to 2 retries)
9. Deliver result to channel (success with PR URL, or failure with details)

#### `decider.py` — No changes
The Decider is not involved in the GitHub pipeline. `handle_message()` continues to serve Matrix chat only.

#### Acceptance Criteria: Responsibility Split

| Who | Does what |
|---|---|
| **Gemini CLI** (step 4 of `/fix-issue`) | Generates testable acceptance criteria from the issue + codebase context, writes to `/workspace/.ipc/acceptance-criteria.md` |
| **Host** (`validate_work()`) | Checks the file exists and is non-empty. On retry, feeds criteria back: "You said X should pass, but tests show it doesn't." |

Gemini generates criteria because it requires understanding both the issue requirements and the codebase. A shell script or the host cannot do this — only Gemini has the full context window with code + issue text + baseline test results.

#### Two-layer validation model ("measure twice, cut once")

| Layer | Who | When | What it catches |
|---|---|---|---|
| **Inner (Gemini instructions)** | Gemini CLI | During `/fix-issue` execution | Gemini tests its own work (step 6), reviews (step 8), iterates on failures (steps 7, 9) — all within a single session with full context |
| **Outer (host validation)** | Python host | After Gemini exits | Scope creep, tests Gemini "rationalized" away, missing PR, missing acceptance criteria, subtle failures |

The inner layer is the `/fix-issue` prompt itself — Gemini is told to test and review before creating the PR. The outer layer is the host independently running the same checks. Two layers, not three.

| Host responsibility | Why Gemini CLI can't do it |
|---|---|
| Timeout enforcement | Gemini can't kill itself |
| Independent test verification | Gemini marks its own homework — host double-checks |
| Scope creep detection | Gemini added it, so it thinks it belongs |
| PR existence check | Gemini might claim success without creating PR |
| Acceptance criteria file check | Gemini might skip generating criteria |
| Container lifecycle | External to Gemini's scope |
| Progress forwarding to Matrix/GitHub | External channels |
| Retry with feedback | Process-level re-launch with specific failure details |
| CI fix flow detection | Webhook parsing |

### Layer 3: What stays the same

- **Matrix chat:** Keeps the existing LiteLLM router in `decider.py` — no changes
- **channels.py:** `GitHubChannel.system_prompt` becomes unused for the new path but stays for backwards compat. One new change: CI fix detection (see below)
- **tools.py:** Stays for Matrix chat path. GitHub path no longer uses it.

## Files to Create

| File | Contents |
|---|---|
| `src/matrix_agent/templates/GEMINI.md` | Workspace context (currently inline in sandbox.py) |
| `src/matrix_agent/templates/status.md` | Initial status log template |
| `src/matrix_agent/templates/settings.json` | Gemini CLI hooks config (with BeforeTool added) |
| `src/matrix_agent/templates/hook-session-start.sh` | SessionStart hook |
| `src/matrix_agent/templates/hook-after-agent.sh` | AfterAgent hook (IPC + status.md, no quality gate) |
| `src/matrix_agent/templates/hook-after-tool.sh` | AfterTool IPC hook |
| `src/matrix_agent/templates/hook-notification.sh` | Notification hook |
| `src/matrix_agent/templates/hook-before-tool.sh` | BeforeTool safety guard hook |
| `src/matrix_agent/templates/cmd-fix-issue.toml` | /fix-issue slash command |
| `src/matrix_agent/templates/cmd-fix-ci.toml` | /fix-ci slash command |
| `src/matrix_agent/templates/skill-delegate-qwen.md` | Qwen delegation skill |
| `src/matrix_agent/templates/qwen-wrapper.sh` | Qwen wrapper with timeout |
| `src/matrix_agent/templates/qwen-settings.json` | Qwen Code DashScope config |

## Files to Modify

| File | Change |
|---|---|
| `src/matrix_agent/sandbox.py` | Refactor `_init_workspace()` to read from flat templates/. Add `workdir` param to `code_stream()`. Add `run_gemini_session()` (thin wrapper around `code_stream()` + IPC read) and `validate_work()` methods. |
| `src/matrix_agent/core.py` | In `_process()`, detect `gh-*` tasks and route to new pipeline with post-validation. All GitHub pipeline logic lives here. |
| `src/matrix_agent/channels.py` | In `_handle_webhook`, when `action == "reopened"`, detect CI fix context and include it in the enqueued message. |

**Note:** `decider.py` is not modified. The GitHub pipeline bypasses the Decider entirely.

### CI Fix Detection in `channels.py`

When a GitHub issue is reopened (action `"reopened"`), the webhook handler must distinguish "new issue" from "CI failure on existing PR". The mechanism:

**Important ordering constraint:** The comment fetch must happen **before** the first `enqueue()` call, so `core.py._process()` sees the `CI_FIX:` prefix in the first message. This is a reorder of the existing backfill logic (lines 226-246), not a new mechanism.

For `action == "reopened"`:
1. Fetch recent comments via `gh api repos/{repo}/issues/{number}/comments --jq '[.[] | .body]'` (moved before enqueue)
2. Scan for comments with the `⚠️` prefix (written by `ci-feedback.yml`)
3. If `⚠️` comment found:
   - Extract the CI failure context (the `⚠️` comment body)
   - Build message as `CI_FIX: <ci_failure_context>\n\nRepository: {repo}\n\n# {title}\n\n{body}`
   - Enqueue this single message (skip separate backfill — CI context is already included)
4. If no `⚠️` comment found:
   - Normal reopened flow: enqueue title+body, then backfill other comments as before
5. `core.py._process()` checks `message.startswith("CI_FIX:")` to choose `/fix-ci` vs `/fix-issue`

The existing backfill filter (excludes `🤖`/`✅`/`❌`) already passes `⚠️` comments through, so no filter changes needed.

## Flow: GitHub Issue -> PR

```
1. Webhook -> channels.py -> core.py enqueue("gh-42", message)
2. core._process() detects gh- prefix
3. sandbox.create("gh-42") -> _init_workspace writes templates (skills/commands/hooks)
4. sandbox.exec("gh-42", "git clone <repo> /workspace/<name>")
5. sandbox.run_gemini_session("gh-42", "/fix-issue <issue context>")
   (wraps code_stream() with workdir=/workspace/<repo>, then reads IPC)
   +-- Inside container: single Gemini CLI session
       |-- SessionStart hook: install deps, baseline tests
       |-- /fix-issue command: structured workflow
       |-- Gemini plans (in its own context window, with full workspace state)
       |-- Gemini generates acceptance criteria -> /workspace/.ipc/acceptance-criteria.md
       |-- Gemini delegates to Qwen via delegate-qwen skill
       |-- Gemini tests, reviews, iterates (same session = full context)
       |-- Gemini checks acceptance criteria are met during review
       |-- BeforeTool hook blocks bare git push
       |-- Gemini creates PR (branch, commit, push, gh pr create)
       |-- AfterTool hooks write IPC progress
       +-- AfterAgent hook writes result IPC + status.md
6. Host validation (second measure):
   |-- sandbox.exec: ruff check . && pytest -v
   |-- sandbox.exec: git diff --stat (scope check)
   |-- Check pr-url.txt exists
   |-- Check acceptance-criteria.md exists and is non-empty
   +-- If fails -> re-launch Gemini with feedback (up to 2 retries)
7. channel.deliver_result("gh-42", "Completed -- PR: <url>")
```

## First-Principle Analysis

### 1. Source of Truth Topology

**Current state:** Split brain. The Python Decider (Gemini Flash) is the "brain" but Gemini CLI inside the container also has its own context window and decision-making. They share no state — the Decider passes a one-shot prompt, gets text back, and decides what to do next. Two decision-makers, no shared truth.

**Proposed state:** Single brain (Gemini CLI), host is just a "sensor" (IPC) + "limb" (container lifecycle). The split-brain problem is eliminated.

- **Source of truth for "is the task done?":** Process exit code + `pr-url.txt` existence. The `event-result.json` from the AfterAgent hook is telemetry, not the authority.
- **Crash recovery:** On crash, the host re-launches Gemini with the same prompt + a preamble: "Previous attempt may have partially completed — check git status and status.md before starting." Gemini reads status.md automatically via the `@status.md` import in GEMINI.md and can pick up where it left off. Existing hooks (AfterAgent appends to status.md, AfterTool writes event-progress.json) plus git state provide the recovery context.

### 2. Side-Effect Control Flow

**Current state: Reactive chain.** LLM response -> parse tool calls -> execute tool -> append result -> loop. The LLM reacts to tool results to decide next action, with no guarantee it'll iterate on failures.

**Proposed state: Intent-based.**

- The `/fix-issue` command is an explicit intent: "do this workflow." Gemini CLI receives a structured prompt with clear steps. This is command-driven, not reactive.
- Hooks are event-driven side-effects with clear triggers — `BeforeTool` blocks dangerous ops, `AfterTool` writes IPC. They don't influence the main flow, they guard it.
- The `/fix-issue` command is a prompt, not a state machine — there's no hard enforcement that step 6 (test) happens before step 10 (create PR). The host validation layer catches this: if tests don't pass after Gemini exits, the host re-launches with feedback.
- Qwen timeout: `.qwen-wrapper.sh` uses `timeout ${QWEN_TIMEOUT:-1800}` to prevent a single Qwen call from consuming the entire session budget.

### 3. Hardware Driver Pattern (Infrastructure Abstraction)

**Proposed state: IMPROVED separation.**

- `SandboxManager` becomes a dumber driver — it launches a process (`run_gemini_session`), monitors IPC files, reports status. It doesn't decide what Gemini should do.
- Business logic (the workflow) moves into `.gemini/commands/fix-issue.toml` — a declarative artifact, not procedural Python.
- Template extraction separates "what files to write" (templates, easy to edit/review) from "how to write them" (sandbox.py, infrastructure).

### 4. Lifecycle & Concurrency

- The single Gemini CLI process per task is cleaner than the current multi-turn loop. One process to monitor, one process to kill on timeout.
- `code_stream` already handles timeout + kill + cleanup. `run_gemini_session` reuses this pattern.
- Follow-up comments during an active session queue behind it and process after — acceptable for Phase 1.
- If the host crashes, `destroy_orphans()` kills the container on restart. Same as today — no regression.

### 5. Testability

- Host-side code is testable: `run_gemini_session` is a thin wrapper around subprocess launch + IPC file reading. `validate_work()` is 4 shell commands + file existence checks.
- Pipeline logic in `core.py` is deterministic: create -> clone -> run session -> validate -> deliver. Easy to test each step.
- The workflow (fix-issue command, skills, hooks) is untestable from Python — it's an LLM prompt. Test it by running it. What you CAN test: (a) templates are syntactically valid TOML, (b) hook scripts execute without error, (c) the host correctly validates IPC output.

### Litmus Test

**"If I delete the Matrix UI and replace the GitHub API with a mock, can I still run the entire business process in a console script?"**

**Current design: No.** You need the LiteLLM API, the tool dispatch loop, and the conversation history management.

**Proposed design: Yes.** `podman exec sandbox gemini -y -p "/fix-issue ..."` is a single command you can run from a terminal. The host-side validation (check pr-url.txt) is a file read.

**Verdict: Passes the litmus test.**

## KISS Simplifications Applied

The following simplifications were applied based on KISS evaluation:

1. **Removed `BeforeAgent` hook** — GEMINI.md already imports `@status.md` for session continuity. Gemini has shell access and can run `git status` itself. One mechanism instead of two.

2. **Removed `AfterAgent` quality gate** — The `/fix-issue` command instructs Gemini to test (step 6) and review (step 8). The host runs the same checks independently after Gemini exits. Two validation layers is enough — adding a third in the AfterAgent hook would run `pytest` on every agent turn (latency cost) for no additional safety.

3. **Inlined `create-pr` skill into `/fix-issue` command** — PR creation executes exactly once at step 10. A separate skill adds a file and an indirection for a single use. The steps (branch, commit, push, `gh pr create`, write pr-url.txt) are part of the command directly.

4. **Flattened template directory** — All 13 templates in a single `src/matrix_agent/templates/` directory with descriptive prefixed names (`hook-before-tool.sh`, `cmd-fix-issue.toml`, `skill-delegate-qwen.md`) instead of nested subdirectories. Easier to scan at a glance.

## Summary of Architectural Decisions

| Decision | Rationale |
|---|---|
| Single Gemini CLI session per task | Eliminates split-brain, preserves context across plan/implement/review |
| Structured `/fix-issue` command over free-form prompt | Intent-based, not reactive. Deterministic workflow encoded as artifact |
| Gemini generates acceptance criteria | Requires codebase + issue context that only Gemini has |
| Host validates criteria file exists | "Measure twice" — host can't judge quality but can enforce the step happened |
| Host validates PR creation, doesn't orchestrate | Clear separation: Gemini decides, host verifies |
| BeforeTool hook for safety, not Python code | Guard lives at point of execution, not in routing layer |
| Two-layer validation (instructions + host) | `/fix-issue` instructions (inner) + host post-validation (outer) provide defense-in-depth without over-engineering |
| Crash recovery via existing hooks | `status.md` (via AfterAgent hook) + `event-progress.json` (via AfterTool hook) + git state provide recovery context; GEMINI.md auto-imports status.md |
| Template extraction from sandbox.py | Separates "what to write" from "how to write it" — easier to review and edit |
| Flat template directory | 13 files in one directory with prefixed names — no unnecessary nesting |
| PR creation inlined in /fix-issue | Single use at step 10 — doesn't warrant a separate skill |
| No BeforeAgent hook | GEMINI.md `@status.md` import already provides continuity; Gemini has shell access for git status |
| No AfterAgent quality gate | Two validation layers (Gemini instructions + host) is enough; avoids pytest on every agent turn |
| GitHub pipeline in core.py, not decider.py | core.py already has task_id, message, channel — no need to route through Decider. Keeps Decider focused on Matrix/LiteLLM |
| run_gemini_session() wraps code_stream() | Avoids duplicating subprocess/streaming/timeout logic. Only adds IPC file reading after exit |
| CI fix detected via `⚠️` comment prefix | Reuses existing comment-fetching pattern in _handle_webhook; `CI_FIX:` message prefix for core.py routing |

## Verification

1. Create a test GitHub issue with `agent-task` label
2. Verify bot picks it up and launches a single Gemini CLI session (not multi-turn LiteLLM loop)
3. Check IPC files are written during execution (progress reporting works)
4. Check `acceptance-criteria.md` is generated with testable criteria
5. Verify PR is created and URL is reported
6. Verify timeout enforcement by setting a short timeout
7. Test CI fix flow: fail CI, verify reopened issue triggers `/fix-ci` path
8. Verify Matrix chat still works via existing LiteLLM router (no regression)
9. Run existing test suite: `uv run pytest tests/`

## Migration Strategy

**Phase 1 (this spec):** GitHub issues only — both paths coexist
**Phase 2 (future):** Optionally migrate Matrix chat to persistent Gemini CLI sessions
**Phase 3 (future):** Remove LiteLLM router, TOOL_SCHEMAS, simplify decider
