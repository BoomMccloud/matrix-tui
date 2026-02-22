# Programming Loop Spec

## Overview

A Ralph Loop-based autonomous coding workflow. The user drives spec creation and analysis (human-in-the-loop), then hands off to an autonomous agent loop that plans, tests, implements, and reviews until done.

## Workflow

```
Human Phase                          Autonomous Phase
──────────                           ────────────────
/spec  ──→  /analyze  ──→  /verify  ──→  /go
  │            │              │             │
  You         Gemini         Gemini        Ralph Loop
  write       first          verify        (runs to completion)
  spec        principles     against
              + KISS         codebase
```

### Human Phase (steps 1–3)

You stay in the loop. Each step requires your explicit approval before proceeding.

| Step | Command | Tool | What happens |
|------|---------|------|-------------|
| 1 | `/spec` | You | Paste the problem statement and proposed approach. Saved to `PRD.md` in the sandbox. |
| 2 | `/analyze` | Gemini CLI | Reads `PRD.md` + entire codebase. Answers: does this solve the root problem? Is it the simplest solution? Reports back for your approval. |
| 3 | `/verify` | Gemini CLI | Reads simplified spec + codebase. Answers: does this fit existing patterns? Any conflicts? Reports back for your approval. |

### Autonomous Phase (step 4)

You say `/go`. The agent runs to completion. You come back when it's done.

The loop executes a checklist written to `PROGRESS.md`. Each iteration, the agent:
1. Reads `PRD.md` (the spec — never changes)
2. Reads `PROGRESS.md` (what's done, what's next)
3. Reads `AGENTS.md` (accumulated learnings from prior iterations)
4. Executes the next unchecked item
5. Checks it off in `PROGRESS.md`
6. Writes any learnings to `AGENTS.md`
7. Repeats until all items are checked

## Checklist Template

When `/go` is triggered, the bot writes this to `PROGRESS.md`:

```markdown
# Progress

## Tasks
- [ ] Write implementation plan
- [ ] Write integration tests (from spec, test behavior not implementation)
- [ ] Implement code + unit tests (make all tests pass)
- [ ] Run all tests
- [ ] Review for debuggability and documentation
```

### Task details

**Write implementation plan**
- Agent: Gemini CLI (needs full codebase context)
- Output: `PLAN.md` — every file to create/modify, exact changes, order of operations
- Checks off when `PLAN.md` is written

**Write integration tests**
- Agent: Gemini CLI (reads spec + public interfaces only, NOT the plan)
- Output: test files in the appropriate test directory
- Tests define the acceptance criteria — they should all fail at this point (red)
- Checks off when integration test files exist and fail as expected

**Implement code + unit tests**
- Agent: Qwen Code (primary) / Aider (multi-file refactors)
- Reads `PLAN.md`, implements each change, writes unit tests alongside
- Inner retry loop: run tests → if fail → fix → run tests → repeat (max 5 retries)
- Checks off when all unit tests AND integration tests pass (green)

**Run all tests**
- Agent: direct sandbox exec (`npm test` / `pytest` / etc.)
- Runs the full test suite as a final gate
- If any test fails, unchecks "Implement code + unit tests" to trigger a re-implementation loop
- Checks off when all tests pass

**Review for debuggability and documentation**
- Agent: Gemini CLI (reads all changed files + tests)
- Checks: missing error handling, unclear names, missing logs, undocumented behavior, test coverage gaps
- Output: appended to `REVIEW.md`
- If critical issues found, unchecks "Implement code + unit tests" with notes on what to fix
- Checks off when review passes

## Tool Routing

The loop picks the right tool per task:

| Task | Tool | Why |
|------|------|-----|
| Plan | Gemini CLI | Needs 1M context to see full codebase |
| Integration tests | Gemini CLI | Independent agent, reads spec not implementation |
| Implement | Qwen Code | Highest benchmark accuracy (70.6%) |
| Implement (multi-file) | Aider | Best git workflow, auto-commits |
| Run tests | Sandbox exec | Direct command, no LLM needed |
| Review | Gemini CLI | Reads everything at once |

## File Layout (inside sandbox container)

```
/workspace/
├── PRD.md              # Spec (written by human, never modified by agent)
├── PROGRESS.md         # Checklist (updated by agent each iteration)
├── PLAN.md             # Implementation plan (written by agent)
├── REVIEW.md           # Review notes (written by agent)
├── AGENTS.md           # Accumulated learnings (appended by agent)
└── <project files>     # The actual codebase
```

## Resilience

**Context overflow:** Each loop iteration starts fresh. The agent reads from disk files, not from conversation memory. A task that fills the context window doesn't lose progress — the next iteration picks up from `PROGRESS.md`.

**Crashes:** `PROGRESS.md` is on disk. If the bot restarts, `/go` resumes from the last unchecked item.

**Stuck loops:** `max_retries` per task (default: 5). If a task fails 5 times, the loop stops and reports to the user: "Stuck on X after 5 attempts. Here's the last error."

**Learnings persist:** `AGENTS.md` accumulates patterns, gotchas, and conventions discovered during implementation. Future iterations (and future `/go` runs on the same repo) benefit from prior knowledge.

## Configuration

```
# .env
MAX_LOOP_RETRIES=5              # Max retries per checklist item
MAX_IMPLEMENT_RETRIES=5         # Max test-fail → re-implement cycles
GEMINI_API_KEY=                 # For analysis, planning, integration tests, review
QWEN_API_KEY=                   # For implementation (primary)
OPENROUTER_API_KEY=             # For orchestrator (Sonnet 4)
```

## Example Session

```
You:    /spec
        Problem: Users can't reset their password. The reset endpoint
        returns 500 because the token table has no expiry column.
        Approach: Add expiry column, validate on reset, add migration.

Bot:    Spec saved to PRD.md.

You:    /analyze

Bot:    First principles: Yes, the root cause is missing token expiry.
        The approach addresses it directly.
        KISS check: Migration + validation is minimal. No overengineering.
        Approve? (ok / feedback)

You:    ok

You:    /verify

Bot:    Codebase uses Knex for migrations (src/db/migrations/).
        Token model is at src/models/token.js.
        Reset endpoint is at src/routes/auth.js:142.
        No conflicts. Pattern matches existing migration style.
        Approve? (ok / feedback)

You:    ok

You:    /go

Bot:    Starting autonomous loop...

Bot:    [1/5] Writing implementation plan...
        → PLAN.md written (3 files to modify, 1 migration to create)

Bot:    [2/5] Writing integration tests...
        → 6 integration tests written (all failing — red)

Bot:    [3/5] Implementing + unit tests...
        → Migration created, model updated, endpoint fixed
        → 8 unit tests written
        → Running tests... 13/14 passed
        → Fixing: test_expired_token_returns_400
        → Running tests... 14/14 passed (green)

Bot:    [4/5] Running full test suite...
        → 14/14 passed

Bot:    [5/5] Reviewing...
        → No critical issues. Added suggestion: log token expiry
          validation failures for monitoring.

Bot:    ✓ All tasks complete.
        Summary: 1 migration, 3 files modified, 14 tests passing.
        Review notes in REVIEW.md.
```
