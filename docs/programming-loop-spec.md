# Programming Loop Spec

> **Status: Planned / Not yet implemented.**
> The current bot accepts free-form task messages and delegates to Gemini directly. This spec describes a more structured autonomous workflow for future implementation.

## Overview

A structured autonomous coding workflow with a human approval phase followed by an autonomous implementation loop. The user drives spec creation and review, then hands off to the agent to plan, implement, test, and review until done.

## Workflow

```
Human Phase                          Autonomous Phase
──────────                           ────────────────
/spec  ──→  /analyze  ──→  /verify  ──→  /go
  │            │              │             │
  You         Gemini         Gemini        Loop
  write       first          verify        (runs to completion)
  spec        principles     against
              + KISS         codebase
```

### Human Phase (steps 1–3)

| Step | Command | What happens |
|------|---------|-------------|
| 1 | `/spec` | You paste the problem statement. Saved to `PRD.md` in the sandbox. |
| 2 | `/analyze` | Gemini reads `PRD.md` + codebase. Is this the simplest solution? Reports back for approval. |
| 3 | `/verify` | Gemini checks the spec fits existing patterns. Any conflicts? Reports back for approval. |

### Autonomous Phase (step 4)

`/go` starts the loop. The agent runs to completion using a checklist in `PROGRESS.md`:

1. Read `PRD.md` (spec — never changes)
2. Read `PROGRESS.md` (what's done, what's next)
3. Read `AGENTS.md` (accumulated learnings)
4. Execute next unchecked item
5. Check it off, write learnings
6. Repeat until all items checked

## Checklist

```markdown
# Progress

## Tasks
- [ ] Write implementation plan (→ PLAN.md)
- [ ] Write integration tests (fail at this point — red)
- [ ] Implement code + unit tests (make all tests pass — green)
- [ ] Run full test suite
- [ ] Review for debuggability and documentation
```

## Resilience

- **Context overflow** — each iteration reads from disk, not memory. A context-filling task doesn't lose progress.
- **Crashes** — `PROGRESS.md` is on disk. `/go` resumes from last unchecked item.
- **Stuck loops** — `max_retries` per task (default: 5). Reports to user if stuck.

## Why Not Implemented Yet

The current free-form workflow (just message the bot) covers most use cases. This loop adds value for large, multi-step tasks where you want strict spec-first discipline. The main prerequisite is non-blocking tool calls so the bot can respond to `/analyze` and `/verify` commands while a previous task is running.
