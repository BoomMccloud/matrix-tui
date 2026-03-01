# Spec Verification Report

**Spec**: docs/lifecycle-mgmt.md
**Verified**: 2026-03-01
**Overall Status**: ⚠️ WARNINGS

---

## Summary

- **Files**: 5 verified, 0 issues
- **Methods/Functions**: 8 verified, 1 issue
- **Libraries**: 0 issues
- **Data Models**: 2 verified, 0 issues
- **Naming**: 0 issues

---

## Blocking Issues

None.

---

## Warnings

### [WARN-001] `recover_tasks()` needs access to repo context

**Spec says**: `GitHubChannel.recover_tasks()` runs `gh issue list --label agent-task --state open --json number,title,body`

**Reality**: `gh issue list` operates on the current repo (or requires `--repo`). The `GitHubChannel` constructor receives `settings` which has `github_token` but no `github_repo` field. The webhook handler gets the repo from `payload["repository"]["full_name"]`, which won't be available during recovery.

**Suggested fix**: Either:
- Add a `github_repo` setting to `config.py` (e.g., `github_repo: str = ""`)
- Or run `gh issue list` without `--repo` and rely on the working directory being a git repo (fragile on VPS)

### [WARN-002] `recover_tasks()` comment posting doesn't match existing patterns

**Spec says**: Post "Bot restarted — resuming work on this issue." comment on each recovered issue

**Reality**: Existing code in `channels.py:130-133` uses `asyncio.create_subprocess_exec("gh", "issue", "comment", ...)` with `str(issue["number"])`. During recovery, we only have the issue number from `gh issue list` output, not from a webhook payload. The implementation must extract the number correctly from the JSON output. This is straightforward but worth noting — the existing `deliver_result`/`deliver_error` methods extract via `task_id.split("-", 1)[1]`, which will work for `gh-{n}` format.

### [WARN-003] Test file `test_channels.py` — verify existing test structure

**Spec says**: Add test for `GitHubChannel.recover_tasks()` to `test_channels.py`

**Reality**: `tests/test_channels.py` exists. Verify the existing test fixtures and mocking patterns are compatible with testing `recover_tasks()` (which shells out to `gh`). The existing channel tests mock `asyncio.create_subprocess_exec`, so this is consistent.

---

## Verified Items ✅

| Category | Reference | Status |
|---|---|---|
| File | `src/matrix_agent/core.py` | ✅ Exists |
| File | `src/matrix_agent/channels.py` | ✅ Exists |
| File | `src/matrix_agent/bot.py` | ✅ Exists |
| File | `src/matrix_agent/__main__.py` | ✅ Exists |
| File | `tests/test_core.py` | ✅ Exists |
| File | `tests/test_channels.py` | ✅ Exists |
| Method | `TaskRunner.enqueue(task_id, message, channel)` | ✅ Exists at `core.py:21`, signature matches |
| Method | `TaskRunner.destroy_orphans()` | ✅ Exists at `core.py:102`, checks `_processing` |
| Method | `TaskRunner._cleanup(task_id)` | ✅ Exists at `core.py:82` |
| Method | `sandbox.load_state()` | ✅ Exists at `sandbox.py:81`, returns `dict[str, list[dict]]` |
| Method | `decider.load_histories(histories)` | ✅ Exists at `decider.py:92` |
| Method | `Bot.run()` | ✅ Exists at `bot.py:204`, can be split into `setup()` + `run()` |
| Method | `ChannelAdapter.is_valid(task_id)` | ✅ Exists at `channels.py:35` |
| Data Model | `TaskRunner._processing: set[str]` | ✅ Exists at `core.py:19` |
| Data Model | `sandbox._containers: dict[str, str]` | ✅ Exists at `sandbox.py:38` |
| Data Model | `TaskRunner._queues`, `_workers`, `_channels` | ✅ Exist at `core.py:16-18` |
| Config | `settings.github_token` | ✅ Exists at `config.py:34` |
| Import | `GITHUB_SYSTEM_PROMPT` from `decider` | ✅ Exists at `decider.py:63` |
| Naming | `ChannelAdapter` ABC with abstract methods | ✅ Consistent pattern |
| Naming | `task_id` format `gh-{number}` for GitHub | ✅ Used at `channels.py:123` |
| Test | `test_destroy_orphans` in `test_core.py` | ✅ Exists at line 172 |
| Startup | `__main__.py` sequence: `load_state → load_histories → destroy_orphans` | ✅ Matches lines 26-28 |

---

## Recommendations

1. **Before implementing**: Resolve WARN-001 — decide how `recover_tasks()` will target the correct GitHub repo. Add `github_repo` to `config.py` Settings.
2. **Minor**: WARN-002 is informational — the existing `task_id.split("-", 1)[1]` pattern works for posting comments during recovery.
3. **All other references check out** — file paths, method signatures, data models, and naming conventions are accurate.
