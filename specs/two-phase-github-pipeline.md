# Feature: Two-Phase GitHub Issue Pipeline

## Problem

The current GitHub pipeline runs a single Gemini CLI session that plans, implements, tests, and creates a PR in one shot. Two critical flaws:

1. **Scope creep goes undetected until after push.** Gemini commits and pushes forbidden files (pyproject.toml, uv.lock, etc.) inside its session. `validate_work()` runs *after* the session ends — by then the damage is on the remote branch.
2. **No plan validation.** The LLM self-declares scope (`changed-files.txt`) and then violates it. There's no host checkpoint before implementation begins.

## Solution — Two Stages

### Stage 1: Host-Controlled Push (immediate fix)

Move push and PR creation out of the Gemini session and into host control. Gemini commits but does NOT push. The host inspects the commit, strips forbidden files, then pushes and creates the PR.

### Stage 2: Two-Phase Plan/Implement Split (follow-up)

Split the single session into Phase 1 (plan + declare scope) and Phase 2 (implement), with a host gate between them.

---

## Stage 1: Host-Controlled Push

### Current flow (broken)

```
Gemini session:
  analyze → implement → test → git add → git commit → git push → gh pr create
                                                         ↑
                                              forbidden files pushed here
Host:
  validate_work()  ← too late, already pushed
```

### New flow

```
Gemini session:
  analyze → implement → test → git add → git commit → STOP (no push, no PR)

Host (after session ends):
  1. inspect commit: git diff --name-only origin/main...HEAD
  2. check_forbidden() on changed files
  3. if forbidden files found:
       git checkout origin/main -- <forbidden files>
       git commit --amend --no-edit
  4. git push -u origin <branch>
  5. gh pr create --title "..." --body "Closes #N ..."
  6. write pr-url.txt to IPC
  7. validate_work() (tests, scope, acceptance criteria)
```

### Changes required

**`cmd-fix-issue.toml`** — Remove push and PR steps. Gemini's job ends at commit:

```
...
11. **Create branch and commit**:
    - Create a branch: `git checkout -b agent/<slug>`
    - Stage ONLY the files you intentionally changed: `git add <file1> <file2> ...`
    - Commit: `git commit -m "<title>"`
    - DO NOT push. DO NOT run gh pr create. The host handles this.
```

**`cmd-fix-ci.toml`** — Also remove `git push --force` from Gemini. Host pushes after validation:

```
...
6. Stage and commit: `git add <file1> <file2> ... && git commit -m "Fix CI failures"`
   DO NOT push. The host handles the push.
```

**`hook-before-tool.sh`** — Tighten the git push guard. Block ALL git push (including `--force`), since the host now owns pushing:

```sh
if echo "$COMMAND" | grep -q 'git push'; then
  echo '{"error": "git push is blocked. The host handles pushing after validation."}'
  exit 2
fi
```

**`src/matrix_agent/core.py`** — Add host-side push logic after Gemini session, before `validate_work()`:

```python
# After Gemini session ends, before validate_work():

# 1. Find the branch Gemini created
rc, branch, _ = await self.sandbox.exec(task_id,
    f"cd {repo_path} && git rev-parse --abbrev-ref HEAD")
branch = branch.strip()

if branch == "main" or branch == "master":
    # Gemini didn't create a branch — validation will fail on missing PR
    pass
else:
    # 2. Check for forbidden files in the commit
    rc, stdout, _ = await self.sandbox.exec(task_id,
        f"cd {repo_path} && "
        "base=$(git merge-base HEAD origin/main 2>/dev/null || "
        "git merge-base HEAD origin/master 2>/dev/null || echo HEAD~1) && "
        "git diff --name-only $base HEAD")
    changed = [f.strip() for f in stdout.splitlines() if f.strip()]
    forbidden = check_forbidden(changed)

    # 3. Strip forbidden files if any
    if forbidden:
        logger.warning("[%s] Stripping forbidden files from commit: %s",
                       task_id[:20], ", ".join(forbidden))
        await self.sandbox.exec(task_id,
            f"cd {repo_path} && "
            f"git checkout origin/main -- {' '.join(forbidden)} && "
            f"git commit --amend --no-edit")

    # 4. Push
    await self.sandbox.exec(task_id,
        f"cd {repo_path} && git push -u origin {branch}")

    # 5. Create PR
    issue_num = task_id.replace("gh-", "")
    rc, pr_url_out, _ = await self.sandbox.exec(task_id,
        f"cd {repo_path} && "
        f"gh pr create --title 'Fix #{issue_num}' --body 'Closes #{issue_num}' "
        f"--head {branch} 2>/dev/null || "
        f"gh pr view {branch} --json url -q .url")
    pr_url = pr_url_out.strip()

    # 6. Write PR URL to IPC
    await self.sandbox.write_ipc_file(task_id, "pr-url.txt", pr_url)
```

**`src/matrix_agent/sandbox.py`** — Extract `check_forbidden()` and add `write_ipc_file()`:

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

Refactor `validate_work()` to use `check_forbidden()` instead of inline list.

Add `write_ipc_file()`:

```python
async def write_ipc_file(self, chat_id: str, filename: str, content: str) -> None:
    name = self._containers.get(chat_id)
    if not name:
        return
    ipc_host = os.path.join(self.settings.ipc_base_dir, name)
    path = os.path.join(ipc_host, filename)
    with open(path, "w") as f:
        f.write(content)
```

### CI fix flow adjustment

Same pattern — host pushes with `--force` after stripping forbidden files:

```python
# For CI fix: push with --force instead of -u
await self.sandbox.exec(task_id,
    f"cd {repo_path} && git push --force origin {branch}")
```

---

## Stage 2: Two-Phase Plan/Implement Split (follow-up)

After Stage 1 is deployed and validated, add a plan phase before implementation.

### Phase 1: Plan (Gemini CLI)

- Gemini analyzes the issue and codebase
- Writes `/workspace/.ipc/plan.md` — freeform reasoning, approach, steps
- Writes `/workspace/.ipc/changed-files.txt` — one file per line
- **No code changes allowed** — plan prompt explicitly forbids implementation
- Session ends after writing both files

### Host Gate

- Host reads `changed-files.txt`
- Host checks file list against forbidden patterns using `check_forbidden()`
- **If forbidden files found:** one retry with feedback, then fail fast
- **If clean:** proceed to Phase 2

### Phase 2: Implement (Gemini CLI, swappable to Qwen later)

- Host reads `plan.md` and injects it into the Phase 2 prompt
- Gemini implements, tests, commits (no push — Stage 1 handles that)
- Host strips forbidden files, pushes, creates PR, then runs `validate_work()`

### Plan Handoff: Host-Side Prompt Injection

```python
plan_content = await self.sandbox.read_ipc_file(task_id, "plan.md")
impl_prompt = (
    f"/implement-plan Follow this approved plan:\n\n"
    f"{plan_content}\n\n"
    f"Original issue:\n{message}"
)
await self.sandbox.run_gemini_session(task_id, impl_prompt, send_update, repo_name)
```

### Retry Logic

- **Phase 1 retry:** If plan includes forbidden files, retry once with feedback. Phase 1 is read-only, no cleanup needed.
- **Phase 2 retry:** `git checkout -- . && git clean -fd` the repo between attempts. `plan.md` and `changed-files.txt` survive in `.ipc/`.

### CI Fix Flow

Keep single-phase for CI fixes — scope is constrained by the CI failure context. Host still controls the push (Stage 1).

### Control flow (Stages 1+2 combined)

```
_process_github(task_id, message):
    clone repo

    if is_ci_fix:
        run_gemini_session(fix_ci_prompt)
        host_strip_and_push(force=True)
        validate_work()
        return

    # --- Phase 1: Plan ---
    for attempt in range(2):
        run_gemini_session(plan_prompt)
        file_list = read changed-files.txt lines
        forbidden = check_forbidden(file_list)
        if not forbidden:
            break
        feedback = f"Plan includes forbidden files: {forbidden}. Revise."
        clear IPC files for retry
    else:
        deliver_error("Plan includes forbidden files after 2 attempts")
        return

    # --- Phase 2: Implement ---
    plan_content = read_ipc("plan.md")
    for attempt in range(3):
        if attempt > 0:
            git_reset(repo)
        impl_prompt = f"/implement-plan {plan_content}\n\nOriginal issue:\n{message}"
        run_gemini_session(impl_prompt)
        host_strip_and_push()          # Stage 1: host controls push
        passed, failures = validate_work()
        if passed and pr_url:
            deliver_result(pr_url)
            return
        # retry with failure feedback
    deliver_error("Failed after 3 attempts")
```

---

## Files to modify

### Stage 1 (host-controlled push)

| File | Change |
|---|---|
| `src/matrix_agent/core.py` | Add post-session forbidden file stripping, host-side push + PR creation in `_process_github` |
| `src/matrix_agent/sandbox.py` | Extract `check_forbidden()` + constants. Refactor `validate_work()` to use them. Add `write_ipc_file()`. |
| `src/matrix_agent/templates/cmd-fix-issue.toml` | Remove `git push` and `gh pr create` steps. Gemini stops at commit. |
| `src/matrix_agent/templates/cmd-fix-ci.toml` | Remove `git push --force` step. Gemini stops at commit. |
| `src/matrix_agent/templates/hook-before-tool.sh` | Block ALL `git push` (remove `--force` exception). |
| `tests/test_core_github.py` | Add tests for forbidden file stripping, host-side push, PR creation |
| `tests/test_sandbox_gemini.py` | Add tests for `check_forbidden()` |

### Stage 2 (two-phase split, follow-up)

| File | Change |
|---|---|
| `src/matrix_agent/core.py` | Add Phase 1 plan loop + host gate before Phase 2 |
| `src/matrix_agent/templates/cmd-fix-issue.toml` | Rename to `cmd-plan-issue.toml`. Plan + declare scope only. |
| `src/matrix_agent/templates/cmd-implement-plan.toml` | New. "Implement the plan. Run tests. Commit." |
| `tests/test_core_github.py` | Add tests for plan gate, forbidden rejection, plan prompt injection |

## Swapping Phase 2 to Qwen

Phase 2 uses `code_stream(..., cli="gemini")`. To swap to Qwen:
1. Change `cli="gemini"` to `cli="qwen"` (or add a `cli` parameter)
2. No other changes — the plan is in the prompt, `code_stream` already handles `cli="qwen"`

---

## Acceptance Criteria

### Stage 1
- [ ] Gemini sessions never push or create PRs — host does it
- [ ] `hook-before-tool.sh` blocks ALL `git push` (no `--force` exception)
- [ ] Forbidden files are stripped from commits before push
- [ ] Host creates PR via `gh pr create` and writes `pr-url.txt`
- [ ] CI fix flow: host pushes with `--force` after stripping
- [ ] `check_forbidden()` extracted with shared constants
- [ ] `check_forbidden()` has unit tests
- [ ] Host push/PR logic has unit tests
- [ ] Existing integration tests still pass
- [ ] `uv run ruff check src tests` passes

### Stage 2
- [ ] New issues go through plan → gate → implement pipeline
- [ ] Plan with forbidden files is rejected with feedback, retried once, then fails
- [ ] Phase 2 receives plan via host-side prompt injection
- [ ] Repo is git-reset between Phase 2 retries
- [ ] `_process_github` two-phase flow has unit tests
