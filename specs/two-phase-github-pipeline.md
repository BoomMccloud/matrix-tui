# Feature: Two-Phase GitHub Issue Pipeline

## Problem

The current GitHub pipeline runs a single Gemini CLI session that plans, implements, tests, and creates a PR in one shot. The LLM self-declares which files it will modify (`changed-files.txt`) and then frequently violates its own declaration — modifying lock files, config files, or unrelated code. Scope creep is only detected *after* the damage is done. Retries inherit dirty filesystem state with no LLM context.

## Solution

Split `_process_github` into two sequential phases with a host-controlled gate between them.

### Phase 1: Plan (Gemini CLI)

- Gemini analyzes the issue and codebase
- Writes `/workspace/.ipc/plan.md` — freeform reasoning, approach, steps (for Phase 2 to read)
- Writes `/workspace/.ipc/changed-files.txt` — one file per line (for host to validate)
- **No code changes allowed** — plan prompt explicitly forbids implementation
- Session ends after writing both files

### Host Gate

- Host reads `changed-files.txt` (simple line-per-file format, no markdown parsing)
- Host checks file list against forbidden patterns using `check_forbidden()` (shared constants with `validate_work` and `hook-before-tool.sh`)
- **If forbidden files found:** one retry with feedback, then fail fast
- **If clean:** proceed to Phase 2

### Phase 2: Implement (Gemini CLI, swappable to Qwen later)

- Phase 2 Gemini automatically sees the plan via `@.ipc/plan.md` in `GEMINI.md` (see Context Loading below)
- Implements, tests, creates PR
- On completion, `validate_work()` runs as today (tests, scope check against `changed-files.txt`, PR URL, acceptance criteria)

### Context Loading via GEMINI.md

Gemini CLI's `@file` import in `GEMINI.md` auto-loads referenced files at session start. Add one line:

```markdown
## Implementation plan (loaded automatically when present)
@.ipc/plan.md
```

- **Phase 1:** `plan.md` doesn't exist yet → Gemini logs `[ERROR] ENOENT` and continues. Cosmetic noise, no impact.
- **Phase 2:** `plan.md` exists → auto-loaded into context. Zero prompt engineering needed.
- **Qwen swap:** Qwen doesn't read `GEMINI.md`, so Phase 2 prompt would include "read `/workspace/.ipc/plan.md`". One line in the implement template.

This reuses the same pattern as `@status.md` which already works today.

### Retry Logic

- **Phase 1 retry:** If plan includes forbidden files, retry once with feedback. No filesystem cleanup needed (Phase 1 is read-only).
- **Phase 2 retry:** `git checkout -- . && git clean -fd` the repo between attempts (`plan.md` and `changed-files.txt` are in `/workspace/.ipc/`, outside repo — they survive the reset). Max 2 retries (3 attempts total, same as today).

### CI Fix Flow

Keep single-phase for `cmd-fix-ci.toml` — scope is already constrained by the CI failure context and the changes are typically minimal.

---

## Architecture

### Data flow

```
Phase 1 (Gemini) writes:
  /workspace/.ipc/plan.md          → read by Phase 2 Gemini (via @file import)
  /workspace/.ipc/changed-files.txt → read by host gate + validate_work()

Host gate reads:
  changed-files.txt → check_forbidden() → pass/fail

Phase 2 (Gemini) reads:
  plan.md (auto-loaded via GEMINI.md @import)
  Original issue (via prompt)

Host validation reads:
  changed-files.txt (scope check, unchanged from today)
```

### Control flow

Keep `_process_github` as one function with two sequential blocks — don't split into separate methods (KISS: only called from one place, tightly coupled).

```
_process_github(task_id, message):
    clone repo
    if is_ci_fix:
        single-phase (unchanged)
        return

    # --- Phase 1: Plan ---
    for attempt in range(2):
        run_gemini_session(plan_prompt)
        file_list = read changed-files.txt lines
        forbidden = check_forbidden(file_list)
        if not forbidden:
            break
        feedback = f"Plan includes forbidden files: {forbidden}. Revise."
        clear changed-files.txt + plan.md for retry
    else:
        deliver_error("Plan includes forbidden files after 2 attempts")
        return

    # --- Phase 2: Implement ---
    for attempt in range(3):
        if attempt > 0:
            git_reset(repo)  # clean slate, plan.md survives in .ipc/
        run_gemini_session(impl_prompt)
        passed, failures = validate_work()
        if passed and pr_url:
            deliver_result(pr_url)
            return
        # retry with failure feedback
    deliver_error("Failed after 3 attempts")
```

### Forbidden file check (pure function, extracted from validate_work)

```python
FORBIDDEN_NAMES = {
    "pyproject.toml", "uv.lock", "package-lock.json", "Cargo.lock", "go.sum",
    ".gitignore", "CLAUDE.md", "AGENTS.md", "Containerfile", "Makefile",
    "pr-url.txt", "acceptance-criteria.md", "status.md", "GEMINI.md",
}
FORBIDDEN_PREFIXES = (".gemini/", ".claude/", ".github/", "scripts/", "src/matrix_agent/templates/")

def check_forbidden(file_list: list[str]) -> list[str]:
    """Return list of forbidden files. Empty = clean."""
    return [
        f for f in file_list
        if f in FORBIDDEN_NAMES or any(f.startswith(p) for p in FORBIDDEN_PREFIXES)
    ]
```

Shared by: host gate (new), `validate_work()` (refactored to use it), `hook-before-tool.sh` (already has its own copy — keep in sync via comments).

---

## Files to modify

| File | Change |
|---|---|
| `src/matrix_agent/core.py` | Add Phase 1 plan loop + host gate + Phase 2 implement loop inside `_process_github` |
| `src/matrix_agent/sandbox.py` | Extract `check_forbidden()` + `FORBIDDEN_NAMES`/`FORBIDDEN_PREFIXES` constants. Refactor `validate_work()` to use them. Add `write_ipc_file()` helper. |
| `src/matrix_agent/templates/cmd-fix-issue.toml` | Rename to `cmd-plan-issue.toml`. Strip implementation steps — plan + declare scope only. |
| `src/matrix_agent/templates/cmd-implement-plan.toml` | New. "Implement the plan. Run tests. Create PR." |
| `src/matrix_agent/templates/GEMINI.md` | Add `@.ipc/plan.md` import line |
| `src/matrix_agent/templates/cmd-fix-ci.toml` | No change |
| `tests/test_core_github.py` | Update existing tests, add tests for two-phase flow, plan gate, forbidden rejection, git reset between retries |
| `tests/test_sandbox_gemini.py` | Add tests for `check_forbidden()` |

## Swapping Phase 2 to Qwen

Phase 2 uses `code_stream(..., cli="gemini")`. To swap to Qwen:
1. Change `cli="gemini"` to `cli="qwen"` (one arg)
2. Add "Read `/workspace/.ipc/plan.md`" to `cmd-implement-plan.toml` prompt (Qwen doesn't read `GEMINI.md`)
3. No other changes — `code_stream` already handles `cli="qwen"`

---

## Acceptance Criteria

- [ ] New issues go through plan → gate → implement pipeline
- [ ] CI fix issues remain single-phase (unchanged)
- [ ] Plan with forbidden files is rejected with feedback, retried once, then fails
- [ ] Phase 2 Gemini auto-loads `plan.md` via `GEMINI.md` `@.ipc/plan.md` import
- [ ] `changed-files.txt` is written by Phase 1 LLM, validated by host (no markdown parsing)
- [ ] Repo is git-reset between Phase 2 retries; `plan.md` and `changed-files.txt` survive in `.ipc/`
- [ ] `check_forbidden()` extracted and shared between gate and `validate_work()`
- [ ] `check_forbidden()` has unit tests
- [ ] `_process_github` two-phase flow has unit tests (mock `run_gemini_session`)
- [ ] Existing integration tests still pass
- [ ] `uv run ruff check src tests` passes
