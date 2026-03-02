# Spec Verification Report (v2)

**Spec**: specs/gemini-orchestration.md
**Verified**: 2026-03-02
**Overall Status**: PASS — 1 minor warning, all previous blockers resolved

---

## Summary

- **Files**: 4 existing files to modify verified, 13 new files to create (no conflicts)
- **Methods/Functions**: 7 existing verified, 2 new to create
- **Libraries**: No new deps needed
- **Data Models**: N/A
- **Naming**: Consistent with codebase patterns

---

## Blocking Issues

None.

---

## Resolved Issues (from v1)

### [RESOLVED] WARN-001: GitHub pipeline ownership
**Was**: Ambiguity between `core.py` and `decider.py` owning the GitHub pipeline
**Fix applied**: Spec now says `decider.py` is not modified. All GitHub logic in `core.py._process()`.
**Verified**: `core.py:65` `_process()` already has `task_id`, `message`, `channel` — can route directly.

### [RESOLVED] WARN-002: `run_gemini_session()` duplicating `code_stream()`
**Was**: Risk of duplicating subprocess/streaming/timeout logic
**Fix applied**: Spec now explicitly says `run_gemini_session()` is a thin wrapper around `code_stream()`. Only adds IPC file reading after exit.
**Verified**: `code_stream()` at line 477 returns `(rc, stdout, stderr)` — wrapper can read IPC after it returns.

### [RESOLVED] WARN-003: `code_stream()` hardcoded workdir
**Was**: `code_stream()` hardcodes `--workdir /workspace` at line 504
**Fix applied**: Spec adds `workdir` parameter with default `/workspace`.
**Verified**: Line 504 is the only place `--workdir` appears in `code_stream()` — single change point.

### [RESOLVED] WARN-004: CI fix detection unspecified
**Was**: No concrete mechanism for detecting CI fix context
**Fix applied**: Spec now defines the mechanism: check for `⚠️` prefix in recent comments, prepend `CI_FIX:` to message.
**Verified**: `ci-feedback.yml:76` writes `⚠️ CI failed on PR #${PR_NUMBER}...` and reopens the issue. The backfill filter at `channels.py:240-243` excludes `🤖`/`✅`/`❌` but NOT `⚠️`, so CI failure comments pass through correctly.

---

## Warnings

### [WARN-001] `CI_FIX:` prefix detection needs to happen BEFORE the first enqueue

**Spec says** (CI Fix Detection section): In `_handle_webhook`, when `action == "reopened"`, fetch comments, look for `⚠️`, prepend `CI_FIX:` to the enqueued message.

**Reality**: The current code at `channels.py:221-224` enqueues the initial message first, then backfills comments after (lines 226-246). The CI fix detection needs to happen **before** the first `enqueue()` call so the message already has the `CI_FIX:` prefix when `core.py._process()` sees it.

**Current flow:**
```
1. message = f"Repository: {repo}\n\n# {title}\n\n{body}"  # line 223
2. enqueue(task_id, message, self)                           # line 224
3. fetch comments and backfill                               # lines 226-246
```

**Required flow for reopened + CI fix:**
```
1. fetch comments
2. check for ⚠️ prefix
3. if found: message = f"CI_FIX: {ci_context}\n\nRepository: {repo}..."
4. enqueue(task_id, message, self)
```

**Recommendation**: For `action == "reopened"`, move the comment fetch **before** the initial enqueue. The existing backfill after the enqueue can be skipped for reopened issues (the CI context is already in the first message). This is a straightforward reorder of existing logic within the same method.

---

## Verified Items

| Category | Reference | Status |
|---|---|---|
| File | `src/matrix_agent/sandbox.py` | Exists, `_init_workspace()` at line 153, `code_stream()` at line 477 |
| File | `src/matrix_agent/core.py` | Exists, `_process()` at line 65 |
| File | `src/matrix_agent/channels.py` | Exists, `_handle_webhook()` at line 173 |
| File | `src/matrix_agent/config.py` | Exists, `Settings` at line 5 |
| File | `src/matrix_agent/tools.py` | Exists, unchanged by spec |
| File | `src/matrix_agent/decider.py` | Exists, unchanged by spec |
| File | `.github/workflows/ci-feedback.yml` | Exists, writes `⚠️` prefix at line 76 |
| Method | `SandboxManager.code_stream()` | Line 477, `--workdir` at line 504 — single change point for workdir param |
| Method | `SandboxManager._init_workspace()` | Line 153, currently writes 7 files inline |
| Method | `SandboxManager.exec()` | Line 398, returns `(rc, stdout, stderr)` |
| Method | `TaskRunner._process()` | Line 65, has `task_id`, `message`, `channel` — can route GitHub directly |
| Method | `Decider.handle_message()` | Line 126, unchanged — Matrix chat only |
| Config | `coding_timeout_seconds` | Settings line 29, default 2400, used in `code_stream()` |
| Config | `ipc_base_dir` | Settings line 39, default `/tmp/sandbox-ipc` |
| Pattern | `task_id = f"gh-{number}"` | channels.py lines 154, 204, 259 |
| Pattern | `task_id.startswith("gh-")` | Already used in tools.py:309 for `create_pull_request` |
| Pattern | Message format `Repository: owner/repo` | channels.py:223 — confirmed as first line |
| Pattern | IPC files via hooks | sandbox.py writes hooks that produce `event-result.json`, `event-progress.json` |
| Pattern | Comment backfill | channels.py:226-246 — existing `gh api` pattern reusable for CI detection |
| Pattern | `⚠️` not filtered by backfill | channels.py:240-243 filters `🤖`/`✅`/`❌` only — `⚠️` passes through |
| Pattern | `code_stream()` timeout via `coding_timeout_seconds` | sandbox.py:538 — `run_gemini_session()` inherits this |
| Dir | `src/matrix_agent/templates/` | Does not exist yet — to be created (expected) |
| Method | `run_gemini_session()` | Does not exist yet — to be created (expected) |
| Method | `validate_work()` | Does not exist yet — to be created (expected) |

---

## Recommendations

1. **Before implementing**: Address WARN-001 by reordering comment fetch before initial enqueue for `action == "reopened"` in `_handle_webhook`. This is a straightforward reorder within the existing method, not a new mechanism.
