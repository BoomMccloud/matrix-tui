# Implementation Guide: Shift GitHub Issue Orchestration into Gemini CLI

**Based on Spec**: `specs/gemini-orchestration.md`
**Verification Report**: `specs/gemini-orchestration-verification-report.md`
**Generated**: 2026-03-02

---

## Overview

### What You're Building

Replace the multi-turn Python LLM routing loop for GitHub issues with a single long-running Gemini CLI session inside the container. The Python host becomes a launcher that kicks off Gemini CLI with a `/fix-issue` command, then independently validates the output (tests pass, PR created, acceptance criteria generated).

### Deliverables

After completing this guide, you will have:

- [ ] 13 template files in `src/matrix_agent/templates/`
- [ ] Refactored `_init_workspace()` reading from templates instead of inline strings
- [ ] `workdir` parameter on `code_stream()`
- [ ] `run_gemini_session()` and `validate_work()` methods on `SandboxManager`
- [ ] GitHub pipeline routing in `core.py._process()`
- [ ] CI fix detection in `channels.py._handle_webhook()`

### Files You Will Create

| File | Summary |
|---|---|
| `src/matrix_agent/templates/GEMINI.md` | Workspace context |
| `src/matrix_agent/templates/status.md` | Initial status log |
| `src/matrix_agent/templates/settings.json` | Gemini CLI hooks config |
| `src/matrix_agent/templates/hook-session-start.sh` | SessionStart hook |
| `src/matrix_agent/templates/hook-after-agent.sh` | AfterAgent hook |
| `src/matrix_agent/templates/hook-after-tool.sh` | AfterTool IPC hook |
| `src/matrix_agent/templates/hook-notification.sh` | Notification hook |
| `src/matrix_agent/templates/hook-before-tool.sh` | BeforeTool safety guard |
| `src/matrix_agent/templates/cmd-fix-issue.toml` | /fix-issue slash command |
| `src/matrix_agent/templates/cmd-fix-ci.toml` | /fix-ci slash command |
| `src/matrix_agent/templates/skill-delegate-qwen.md` | Qwen delegation skill |
| `src/matrix_agent/templates/qwen-wrapper.sh` | Qwen wrapper with timeout |
| `src/matrix_agent/templates/qwen-settings.json` | Qwen DashScope config |

### Files You Will Modify

| File | Summary |
|---|---|
| `src/matrix_agent/sandbox.py` | Refactor `_init_workspace()`, add `workdir` to `code_stream()`, add `run_gemini_session()`, `validate_work()` |
| `src/matrix_agent/core.py` | Route `gh-*` tasks to new pipeline in `_process()` |
| `src/matrix_agent/channels.py` | CI fix detection in `_handle_webhook()` for reopened issues |

### Out of Scope — DO NOT MODIFY

- `src/matrix_agent/decider.py` — GitHub pipeline bypasses the Decider entirely
- `src/matrix_agent/tools.py` — stays for Matrix chat path
- `src/matrix_agent/bot.py` — no changes
- `src/matrix_agent/config.py` — no changes

---

## Prerequisites

```bash
# Verify tests pass before starting
uv run pytest tests/ -v

# Verify lint is clean
uv run ruff check src tests
```

---

## Phase 1: Template Extraction (sandbox.py)

Extract inline strings from `_init_workspace()` into template files, then rewrite the method to read from them.

### Step 1.1: Create the templates directory

```bash
mkdir -p src/matrix_agent/templates
```

### Step 1.2: Extract existing templates

Create each template file with the exact content currently inline in `sandbox.py` lines 162-378. The content for each existing template comes directly from the current inline strings:

**`src/matrix_agent/templates/status.md`** — copy from sandbox.py lines 162-173 (the string passed to `write("/workspace/status.md", ...)`).

**`src/matrix_agent/templates/GEMINI.md`** — copy from sandbox.py lines 178-202.

**`src/matrix_agent/templates/settings.json`** — copy from sandbox.py lines 205-258, BUT add the new `BeforeTool` hook entry. The final JSON should have these hook sections: `SessionStart`, `AfterAgent`, `AfterTool`, `Notification`, and the new `BeforeTool`:

```json
"BeforeTool": [
  {
    "hooks": [
      {
        "name": "safety-guard",
        "type": "command",
        "command": "/workspace/.gemini/hooks/before-tool.sh",
        "timeout": 5000
      }
    ]
  }
]
```

**`src/matrix_agent/templates/hook-session-start.sh`** — copy from sandbox.py lines 260-316.

**`src/matrix_agent/templates/hook-after-agent.sh`** — copy from sandbox.py lines 318-327. Keep existing behavior (no quality gate).

**`src/matrix_agent/templates/hook-after-tool.sh`** — copy from sandbox.py lines 329-334.

**`src/matrix_agent/templates/hook-notification.sh`** — copy from sandbox.py lines 336-342.

**`src/matrix_agent/templates/qwen-wrapper.sh`** — based on sandbox.py lines 345-360, BUT add the timeout:

```sh
#!/bin/sh
# Wrapper for qwen CLI — writes event-result.json on completion.
# Usage: .qwen-wrapper.sh "prompt text"
output=$(timeout ${QWEN_TIMEOUT:-1800} qwen -y -p "$1" 2>&1) || true
rc=$?
timestamp=$(date '+%Y-%m-%dT%H:%M:%S')
cat > /workspace/.ipc/event-result.json <<IPCEOF
{"cli": "qwen", "exit_code": $rc, "timestamp": "$timestamp"}
IPCEOF
if [ $rc -ne 0 ]; then
  echo "wrapper error: qwen exited $rc" >> /workspace/.ipc/hook-errors.log
fi
echo "$output"
exit $rc
```

**`src/matrix_agent/templates/qwen-settings.json`** — copy from sandbox.py lines 363-378.

### Step 1.3: Create new template files

These are new files that don't exist in the current codebase.

**`src/matrix_agent/templates/hook-before-tool.sh`:**

```sh
#!/bin/sh
# BeforeTool hook — blocks bare git push (must use --force for CI fix flow).
# Exit code 2 = Gemini CLI blocks the tool execution.
# Reads JSON from stdin with tool invocation details.
INPUT=$(cat)

# Check if this is a shell command containing "git push" without "--force"
COMMAND=$(echo "$INPUT" | grep -o '"command"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1)
if echo "$COMMAND" | grep -q 'git push' && ! echo "$COMMAND" | grep -q '\-\-force'; then
  echo '{"error": "git push without --force is blocked. Use create-pr workflow or --force for CI fixes."}'
  exit 2
fi

echo '{}'
```

**`src/matrix_agent/templates/cmd-fix-issue.toml`:**

```toml
[command]
name = "fix-issue"
description = "Fix a GitHub issue: plan, implement, test, review, create PR"

[command.steps]
prompt = """
You are fixing a GitHub issue. Follow this workflow exactly:

1. Run `/init` to generate a project-specific GEMINI.md for this repo.
2. Read `/workspace/.baseline-tests.txt` to understand the test baseline before your changes.
3. **Plan**: Analyze the codebase, identify the files to change, and design your approach.
4. **Acceptance Criteria**: Write testable acceptance criteria to `/workspace/.ipc/acceptance-criteria.md`. Each criterion should be specific and verifiable (e.g., "Function X returns Y when given Z", "Test test_foo passes").
5. **Implement**: Use the `delegate-qwen` skill to write/modify code. Pass the full plan as context.
6. **Test**: Run the project's test suite. For Python: `ruff check . && pytest -v`. For Node: `npm run lint && npm test`.
7. If tests fail, fix and re-test (max 3 attempts).
8. **Review**: Run `git diff` and review your changes for correctness, security issues, scope creep, and whether each acceptance criterion is met.
9. If review finds issues, fix and re-test.
10. **Create PR**:
    - Create a branch: `git checkout -b agent/<slug>`
    - Stage and commit: `git add -A && git commit -m "<title>"`
    - Push: `git push -u origin agent/<slug>`
    - Open PR: `gh pr create --title "<title>" --body "Closes #<number>\n\n<summary>"`
    - Write the PR URL to `/workspace/.ipc/pr-url.txt`

Issue context:
{{args}}
"""
```

**`src/matrix_agent/templates/cmd-fix-ci.toml`:**

```toml
[command]
name = "fix-ci"
description = "Fix CI failures on an existing PR branch"

[command.steps]
prompt = """
CI has failed on an existing PR. Fix it:

1. The PR branch should already be checked out. Verify with `git branch`.
2. Read the CI failure context below to understand what broke.
3. Fix the failing tests or lint issues.
4. Run the test suite to verify: `ruff check . && pytest -v` (or project-appropriate).
5. If tests pass, force-push: `git add -A && git commit -m "Fix CI failures" && git push --force`
6. Write the PR URL to `/workspace/.ipc/pr-url.txt` (read it from `gh pr list --head <branch> --json url`).

CI failure context:
{{args}}
"""
```

**`src/matrix_agent/templates/skill-delegate-qwen.md`:**

```markdown
# Delegate to Qwen Code

Use Qwen Code for writing and modifying code. Invoke it via:

```sh
/workspace/.qwen-wrapper.sh "<detailed task description>"
```

## When to use Qwen
- Writing new code or modifying existing files
- Implementing features, fixing bugs, refactoring
- Writing tests

## When NOT to use Qwen
- Reading or analyzing code (you can do this directly)
- Planning or designing approaches (do this yourself)
- Running shell commands (use your shell tool)

## Important
- Pass the FULL plan context in the prompt — Qwen has no memory of previous calls
- Include specific file paths, function names, and acceptance criteria
- Qwen has a timeout (default 30 minutes) — break large tasks into smaller calls
```

### Step 1.4: Rewrite `_init_workspace()` in sandbox.py

**File**: `src/matrix_agent/sandbox.py`

Add the `TEMPLATES` mapping and a `_templates_dir` helper near the top of the file (after the imports, around **line 14**). Add `from pathlib import Path` to imports.

**Add import at line 7** (after `import os`):

```python
from pathlib import Path
```

**Add after line 15** (`log = logging.getLogger(__name__)`):

```python
_TEMPLATES_DIR = Path(__file__).parent / "templates"

# Maps template filename -> container destination path
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

# Templates that need chmod +x
_EXECUTABLE_TEMPLATES = {
    "hook-session-start.sh",
    "hook-after-agent.sh",
    "hook-after-tool.sh",
    "hook-notification.sh",
    "hook-before-tool.sh",
    "qwen-wrapper.sh",
}
```

**Replace `_init_workspace()`** (lines 153-396) with:

```python
async def _init_workspace(self, container_name: str) -> None:
    """Initialize workspace coordination files on container creation."""
    async def write(path: str, content: str) -> None:
        await self._run(
            "exec", "-i", container_name, "sh", "-c",
            f"mkdir -p $(dirname {path}) && cat > {path}",
            stdin_data=content.encode(),
        )

    # Write all templates to the container
    for template_name, container_path in TEMPLATES.items():
        content = (_TEMPLATES_DIR / template_name).read_text()
        await write(container_path, content)

    # Make hook/wrapper scripts executable
    exec_paths = [
        TEMPLATES[name] for name in _EXECUTABLE_TEMPLATES
        if name in TEMPLATES
    ]
    if exec_paths:
        await self._run("exec", container_name, "chmod", "+x", *exec_paths)

    # Git identity for commits and PRs
    await self._run("exec", container_name, "git", "config", "--global",
                    "user.email", "bot@matrix-agent")
    await self._run("exec", container_name, "git", "config", "--global",
                    "user.name", "Matrix Agent")
    # gh CLI auth (uses GITHUB_TOKEN env var already injected)
    if self.settings.github_token:
        await self._run("exec", container_name, "gh", "auth", "setup-git")
```

### Step 1.5: Verify template extraction

```bash
uv run pytest tests/ -v
uv run ruff check src tests
```

All existing tests should still pass. The behavior is identical — only the source of the string content changed.

### Common Mistakes

- **Forgetting to copy the trailing newline** from inline strings. Check each template file ends with a newline.
- **Breaking the JSON in settings.json** when adding the BeforeTool entry. Validate with `python -m json.tool src/matrix_agent/templates/settings.json`.
- **Not including `from pathlib import Path`** in the imports.

---

## Phase 2: Add `workdir` to `code_stream()` (sandbox.py)

### Step 2.1: Add `workdir` parameter

**File**: `src/matrix_agent/sandbox.py`

**Find** the `code_stream` method signature at **line 477**:

```python
    async def code_stream(
        self,
        chat_id: str,
        task: str,
        on_chunk: Callable[[str], Awaitable[Any]],
        cli: str = "gemini",
        chunk_size: int = 800,
        auto_accept: bool = False,
    ) -> tuple[int, str, str]:
```

**Replace with:**

```python
    async def code_stream(
        self,
        chat_id: str,
        task: str,
        on_chunk: Callable[[str], Awaitable[Any]],
        cli: str = "gemini",
        chunk_size: int = 800,
        auto_accept: bool = False,
        workdir: str = "/workspace",
    ) -> tuple[int, str, str]:
```

### Step 2.2: Use `workdir` in subprocess call

**Find line 504** (inside `code_stream`):

```python
        proc = await asyncio.create_subprocess_exec(
            self.podman, "exec", "--workdir", "/workspace", name,
```

**Replace with:**

```python
        proc = await asyncio.create_subprocess_exec(
            self.podman, "exec", "--workdir", workdir, name,
```

### Step 2.3: Verify

```bash
uv run pytest tests/ -v
```

No behavior change — default is still `/workspace`.

---

## Phase 3: Add `run_gemini_session()` and `validate_work()` (sandbox.py)

### Step 3.1: Add `run_gemini_session()`

**File**: `src/matrix_agent/sandbox.py`

**Add after `code_stream()`** (after line 553, before `code()`):

```python
    async def run_gemini_session(
        self,
        chat_id: str,
        prompt: str,
        on_chunk: Callable[[str], Awaitable[Any]],
        repo_name: str,
    ) -> tuple[int, str, str | None]:
        """Run a single Gemini CLI session for a GitHub issue.

        Wraps code_stream() with repo-specific workdir, then reads IPC files.
        Returns (exit_code, stdout, pr_url_or_none).
        """
        workdir = f"/workspace/{repo_name}"
        rc, stdout, stderr = await self.code_stream(
            chat_id, prompt, on_chunk,
            cli="gemini", auto_accept=True, workdir=workdir,
        )

        # Read PR URL from IPC (source of truth for success)
        pr_url = None
        name = self._containers.get(chat_id)
        if name:
            ipc_host = os.path.join(self.settings.ipc_base_dir, name)
            pr_url_path = os.path.join(ipc_host, "pr-url.txt")
            if os.path.exists(pr_url_path):
                with open(pr_url_path) as f:
                    pr_url = f.read().strip() or None

        return rc, stdout, pr_url
```

### Step 3.2: Add `validate_work()`

**Add after `run_gemini_session()`:**

```python
    async def validate_work(
        self, chat_id: str, repo_name: str,
    ) -> tuple[bool, list[str]]:
        """Host-side validation after Gemini exits.

        Returns (passed, list_of_failure_descriptions).
        """
        failures: list[str] = []
        repo_path = f"/workspace/{repo_name}"

        # 1. Run tests
        rc, stdout, stderr = await self.exec(
            chat_id, f"cd {repo_path} && ruff check . 2>&1 && pytest -v 2>&1",
        )
        if rc != 0:
            # Truncate long output
            test_output = (stdout + stderr)[:3000]
            failures.append(f"Tests/lint failing:\n{test_output}")

        # 2. Check scope
        rc, stdout, _ = await self.exec(
            chat_id, f"cd {repo_path} && git diff --stat HEAD~1 2>/dev/null || echo 'no commits'",
        )
        if stdout.strip():
            # Just log scope info — not a hard failure, but include for context
            log.info("[%s] Scope: %s", chat_id[:20], stdout.strip()[:500])

        # 3. Check pr-url.txt exists
        name = self._containers.get(chat_id)
        if name:
            ipc_host = os.path.join(self.settings.ipc_base_dir, name)
            pr_url_path = os.path.join(ipc_host, "pr-url.txt")
            if not os.path.exists(pr_url_path):
                failures.append("No PR created (pr-url.txt missing)")

            # 4. Check acceptance criteria
            ac_path = os.path.join(ipc_host, "acceptance-criteria.md")
            if not os.path.exists(ac_path) or os.path.getsize(ac_path) == 0:
                failures.append("Acceptance criteria not generated (acceptance-criteria.md missing or empty)")

        passed = len(failures) == 0
        return passed, failures
```

### Step 3.3: Verify

```bash
uv run pytest tests/ -v
uv run ruff check src tests
```

---

## Phase 4: Route GitHub tasks in `core.py`

### Step 4.1: Modify `_process()` to detect `gh-*` prefix

**File**: `src/matrix_agent/core.py`

**Find `_process()`** at **line 65**. The current method is:

```python
    async def _process(self, task_id: str, message: str, channel: ChannelAdapter) -> None:
        """Run the decider loop for one message."""
        logger.info("[%s] Task started (message: %d chars)", task_id[:20], len(message))
        t0 = time.monotonic()

        # Ensure container exists
        if task_id not in self.sandbox._containers:
            await self.sandbox.create(task_id)

        # Define send_update callback for streaming
        async def send_update(chunk: str) -> None:
            await channel.send_update(task_id, chunk)

        # Run decider
        try:
            final_text = None
            final_status = "completed"
            async for text, image, status in self.decider.handle_message(
                task_id, message,
                send_update=send_update,
                system_prompt=channel.system_prompt,
            ):
                if text:
                    final_text = text
                    final_status = status
            if final_text:
                await channel.deliver_result(task_id, final_text, status=final_status)
            elapsed = time.monotonic() - t0
            logger.info("[%s] Task completed in %.1fs", task_id[:20], elapsed)
        except Exception as e:
            elapsed = time.monotonic() - t0
            logger.error("[%s] Task failed after %.1fs: %s", task_id[:20], elapsed, e)
            await channel.deliver_error(task_id, str(e))
            raise
```

**Replace the entire method with:**

```python
    async def _process(self, task_id: str, message: str, channel: ChannelAdapter) -> None:
        """Run the decider loop for one message."""
        logger.info("[%s] Task started (message: %d chars)", task_id[:20], len(message))
        t0 = time.monotonic()

        # Ensure container exists
        if task_id not in self.sandbox._containers:
            await self.sandbox.create(task_id)

        # Define send_update callback for streaming
        async def send_update(chunk: str) -> None:
            await channel.send_update(task_id, chunk)

        try:
            if task_id.startswith("gh-"):
                await self._process_github(task_id, message, channel, send_update)
            else:
                await self._process_matrix(task_id, message, channel, send_update)
            elapsed = time.monotonic() - t0
            logger.info("[%s] Task completed in %.1fs", task_id[:20], elapsed)
        except Exception as e:
            elapsed = time.monotonic() - t0
            logger.error("[%s] Task failed after %.1fs: %s", task_id[:20], elapsed, e)
            await channel.deliver_error(task_id, str(e))
            raise

    async def _process_matrix(self, task_id: str, message: str, channel: ChannelAdapter, send_update) -> None:
        """Existing LiteLLM decider path for Matrix chat."""
        final_text = None
        final_status = "completed"
        async for text, image, status in self.decider.handle_message(
            task_id, message,
            send_update=send_update,
            system_prompt=channel.system_prompt,
        ):
            if text:
                final_text = text
                final_status = status
        if final_text:
            await channel.deliver_result(task_id, final_text, status=final_status)

    async def _process_github(self, task_id: str, message: str, channel: ChannelAdapter, send_update) -> None:
        """Single Gemini CLI session for GitHub issues."""
        import re as _re

        # Parse repo from message (first line: "Repository: owner/repo" or "CI_FIX: ...")
        is_ci_fix = message.startswith("CI_FIX:")
        if is_ci_fix:
            # CI_FIX: <context>\n\nRepository: owner/repo\n\n...
            repo_match = _re.search(r"Repository:\s*(\S+)", message)
        else:
            repo_match = _re.search(r"Repository:\s*(\S+)", message)

        if not repo_match:
            await channel.deliver_error(task_id, "Could not parse repository from message")
            return

        repo_full = repo_match.group(1)  # e.g. "owner/repo"
        repo_name = repo_full.split("/")[-1]  # e.g. "repo"
        issue_number = task_id.split("-", 1)[1]

        # Clone repo (idempotent — skip if dir exists)
        clone_rc, _, clone_err = await self.sandbox.exec(
            task_id,
            f"test -d /workspace/{repo_name}/.git || git clone https://github.com/{repo_full} /workspace/{repo_name}",
        )
        if clone_rc != 0:
            await channel.deliver_error(task_id, f"Clone failed: {clone_err}")
            return

        # Build prompt
        if is_ci_fix:
            prompt = f"/fix-ci {message}"
        else:
            prompt = f"/fix-issue {message}"

        # Run Gemini with retries
        max_retries = 2
        for attempt in range(max_retries + 1):
            rc, stdout, pr_url = await self.sandbox.run_gemini_session(
                task_id, prompt, send_update, repo_name,
            )

            # Validate
            passed, failures = await self.sandbox.validate_work(task_id, repo_name)

            if passed and pr_url:
                await channel.deliver_result(task_id, f"PR created: {pr_url}")
                return

            if attempt < max_retries:
                # Re-launch with feedback
                failure_text = "\n".join(f"- {f}" for f in failures)
                if not pr_url:
                    failure_text += "\n- No PR URL found"
                prompt = (
                    f"Host validation failed after your previous attempt:\n"
                    f"{failure_text}\n\n"
                    f"Fix these issues, then create the PR.\n\n"
                    f"Original issue:\n{message}"
                )
                logger.warning("[%s] Validation failed (attempt %d/%d): %s",
                               task_id[:20], attempt + 1, max_retries + 1,
                               "; ".join(failures))
            else:
                # Final failure
                failure_text = "\n".join(f"- {f}" for f in failures)
                await channel.deliver_error(
                    task_id,
                    f"Failed after {max_retries + 1} attempts. Issues:\n{failure_text}",
                )
```

### Step 4.2: Add `re` import if needed

`core.py` doesn't currently import `re`. The `_process_github` method imports it locally as `_re` to avoid polluting the module namespace. This is fine as-is.

### Step 4.3: Verify

```bash
uv run pytest tests/ -v
```

Existing tests should still pass — the `gh-*` prefix routing only activates for GitHub task IDs, and the test mocks use `task-1`, `task-2`, etc.

### Common Mistakes

- **Forgetting to handle the case where `repo_match` is None** — the method returns early with `deliver_error`.
- **Using `message.startswith("CI_FIX:")` before the message is parsed** — this is correct because `channels.py` prepends `CI_FIX:` before enqueuing.

---

## Phase 5: CI Fix Detection in `channels.py`

### Step 5.1: Modify `_handle_webhook` for reopened issues

**File**: `src/matrix_agent/channels.py`

**Find the reopened handler** at **line 197** (inside the `if event_type == "issues" and action in ("labeled", "reopened"):` block). Currently after the idempotency check (line 207-208), the code posts a "Working" comment, then enqueues, then backfills.

**Replace lines 197-246** (the `else:` branch for reopened, through the backfill section) with the following. The key change is: for `action == "reopened"`, fetch comments BEFORE enqueuing to detect CI fix context.

The full `if event_type == "issues" and action in ("labeled", "reopened"):` block should become:

```python
        if event_type == "issues" and action in ("labeled", "reopened"):
            # For "labeled", only react to the agent-task label
            if action == "labeled":
                label = payload.get("label", {}).get("name", "")
                if label != "agent-task":
                    return web.Response(text="ignored label")
            else:
                # For "reopened", verify agent-task label is present
                issue_labels = [lb["name"] for lb in payload["issue"].get("labels", [])]
                if "agent-task" not in issue_labels:
                    return web.Response(text="reopened but not an agent-task issue")

            issue = payload["issue"]
            task_id = f"gh-{issue['number']}"

            # Idempotency: skip if already processing
            if task_id in self.task_runner._processing:
                return web.Response(text="already processing")

            # Post "Working" comment
            proc = await asyncio.create_subprocess_exec(
                "gh", "issue", "comment", str(issue["number"]),
                "--body", "\U0001f916 Working on this issue...",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                log.error("gh issue comment (working) failed for #%s: %s", issue["number"], stderr.decode())

            repo_full_name = payload.get("repository", {}).get("full_name", "")

            # For reopened issues, check for CI failure context BEFORE enqueuing
            ci_context = None
            if action == "reopened" and repo_full_name:
                proc = await asyncio.create_subprocess_exec(
                    "gh", "api", f"repos/{repo_full_name}/issues/{issue['number']}/comments",
                    "--jq", "[.[] | .body]",
                    stdout=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                if proc.returncode == 0 and stdout:
                    try:
                        bodies = json.loads(stdout.decode())
                    except (ValueError, TypeError):
                        bodies = []
                    # Look for CI failure comments (⚠️ prefix from ci-feedback.yml)
                    ci_comments = [b for b in bodies if b.strip().startswith("\u26a0\ufe0f")]
                    if ci_comments:
                        ci_context = ci_comments[-1]  # most recent CI failure

            # Build and enqueue the message
            if ci_context:
                message = f"CI_FIX: {ci_context}\n\nRepository: {repo_full_name}\n\n# {issue['title']}\n\n{issue.get('body', '')}"
                await self.task_runner.enqueue(task_id, message, self)
                # Skip backfill — CI context is already included
            else:
                message = f"Repository: {repo_full_name}\n\n# {issue['title']}\n\n{issue.get('body', '')}"
                await self.task_runner.enqueue(task_id, message, self)

                # Backfill existing comments as bundled context
                if repo_full_name:
                    proc = await asyncio.create_subprocess_exec(
                        "gh", "api", f"repos/{repo_full_name}/issues/{issue['number']}/comments",
                        "--jq", "[.[] | .body]",
                        stdout=asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await proc.communicate()
                    if proc.returncode == 0 and stdout:
                        try:
                            bodies = json.loads(stdout.decode())
                        except (ValueError, TypeError):
                            bodies = []
                        comments = [
                            b for b in bodies
                            if b.strip() and not b.strip().startswith(("\U0001f916", "\u2705", "\u274c"))
                        ]
                        if comments:
                            context = "Previous comments on this issue:\n\n" + "\n---\n".join(comments)
                            await self.task_runner.enqueue(task_id, context, self)
```

**Note**: The emoji characters are: `\U0001f916` = `🤖`, `\u2705` = `✅`, `\u274c` = `❌`, `\u26a0\ufe0f` = `⚠️`.

### Step 5.2: Verify

```bash
uv run pytest tests/ -v
uv run ruff check src tests
```

Existing webhook tests should pass — they test `action == "labeled"` which doesn't hit the new CI fix path.

### Common Mistakes

- **Double-fetching comments** for non-CI reopened issues. The code above only fetches once for `action == "reopened"` to check for CI context, then again for backfill if it's NOT a CI fix. This is 2 API calls for non-CI reopened issues. Acceptable — could optimize later by reusing the first fetch, but not worth the complexity now.
- **Forgetting `\u26a0\ufe0f`** (the `⚠️` unicode). The CI feedback workflow uses this exact prefix.

---

## Final Verification

### Run all tests

```bash
uv run pytest tests/ -v
```

All tests must pass.

### Run lint

```bash
uv run ruff check src tests
```

No errors.

### Validate templates

```bash
# Check settings.json is valid JSON
python -m json.tool src/matrix_agent/templates/settings.json > /dev/null

# Check TOML files parse (requires tomli or Python 3.11+)
python -c "import tomllib; tomllib.load(open('src/matrix_agent/templates/cmd-fix-issue.toml', 'rb'))"
python -c "import tomllib; tomllib.load(open('src/matrix_agent/templates/cmd-fix-ci.toml', 'rb'))"
```

### Smoke test (manual)

If you have a running instance:
1. Create a GitHub issue with `agent-task` label
2. Watch logs for `_process_github` being called instead of `handle_message`
3. Verify Gemini CLI launches with `/fix-issue` prompt
4. Check that Matrix chat still works (uses old decider path)

---

## Pre-Submission Checklist

- [ ] All 13 template files created in `src/matrix_agent/templates/`
- [ ] `_init_workspace()` reads from templates (no inline strings)
- [ ] `code_stream()` has `workdir` parameter (default `/workspace`)
- [ ] `run_gemini_session()` wraps `code_stream()` + reads IPC
- [ ] `validate_work()` checks tests, PR, and acceptance criteria
- [ ] `core.py._process()` routes `gh-*` to `_process_github()`
- [ ] Matrix chat path unchanged (`_process_matrix()`)
- [ ] `channels.py` detects CI fix on reopened issues with `CI_FIX:` prefix
- [ ] `uv run pytest tests/ -v` passes
- [ ] `uv run ruff check src tests` clean
- [ ] `settings.json` template is valid JSON with BeforeTool hook
- [ ] TOML templates parse correctly

---

## Troubleshooting

### Error: `FileNotFoundError` on template read

**Cause**: Template file missing from `src/matrix_agent/templates/`
**Fix**: Check all 13 files exist. Run `ls src/matrix_agent/templates/` and compare against the Files to Create table.

### Error: Existing tests fail after `_init_workspace` refactor

**Cause**: Tests mock `sandbox._run` but the template read happens before the mock is applied.
**Fix**: The template read is a local file read (not a container operation), so it shouldn't be affected by mocks. Check that the template directory is included in the package — you may need to verify `_TEMPLATES_DIR` resolves correctly in the test environment.

### Error: `gh-*` task goes to Matrix decider instead of GitHub pipeline

**Cause**: The `task_id.startswith("gh-")` check is not reached.
**Fix**: Verify the `_process()` method was replaced correctly and the `if` branch comes before the `else`.

### Error: CI fix not detected on reopened issue

**Cause**: No `⚠️` comment found, or the comment fetch failed.
**Fix**: Check that `ci-feedback.yml` ran and posted the `⚠️` comment. Verify `gh api` has permission to read comments.
