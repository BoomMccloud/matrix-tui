# Fix Duplicate Completion Comments on Issue Reopen

## Problem

When a GitHub issue is reopened (e.g., by CI feedback), the backfill logic in `GitHubChannel._handle_webhook` re-enqueues the bot's own prior completion comments (`✅ Completed — ...`, `❌ Failed: ...`, `⚠️ Reached maximum turns...`) as new user messages. The decider processes each one, produces another completion, and posts yet another `✅ Completed` comment — creating a feedback loop that grows with every restart or reopen cycle.

**Root cause:** The backfill `gh api` call (`channels.py:230`) uses `--jq ".[].body"`, which discards comment authorship. The filter at line 237 only checks for the `🤖` prefix, letting `✅`, `❌`, and `⚠️` comments through. Meanwhile, the webhook path at line 248 correctly filters by both sender login and a broader set of prefixes — but this logic is not shared.

## Goals

1. Prevent the bot from re-ingesting its own comments during backfill
2. Use author identity (not just emoji prefixes) as the primary filter — this is the real source of truth
3. Unify the "is this a bot comment?" logic into a single shared predicate
4. Keep emoji prefix check as a secondary guard

## Non-Goals

- Changing the reconcile loop timing or restart behavior
- Modifying how `deliver_result` / `deliver_error` format their output
- Addressing the race between issue close and restart (separate concern)

## Changes

### 1. Add `_is_bot_comment()` helper to `GitHubChannel`

Location: `src/matrix_agent/channels.py`, new method on `GitHubChannel`

```python
_BOT_PREFIXES = ("🤖", "✅", "❌", "⚠️")

def _is_bot_comment(self, *, login: str = "", body: str = "") -> bool:
    """Return True if a comment was posted by the bot."""
    if login.endswith("[bot]") or login == self._bot_login:
        return True
    return body.startswith(self._BOT_PREFIXES)
```

`self._bot_login` should be derived from the GitHub token at init time (or passed via config). If unavailable, fall back to prefix-only matching.

### 2. Update backfill to fetch author login

Location: `src/matrix_agent/channels.py`, lines 229-238

Change the `gh api` call from:
```python
"--jq", ".[].body"
```
to:
```python
"--jq", '.[] | (.user.login + "\t" + .body)'
```

Then parse each line to extract `login` and `body`, and filter using `_is_bot_comment(login=login, body=body)`.

### 3. Update webhook comment filter to use shared helper

Location: `src/matrix_agent/channels.py`, line 248

Replace:
```python
if sender.endswith("[bot]") or payload["comment"]["body"].startswith(("✅", "❌", "🤖")):
```
with:
```python
if self._is_bot_comment(login=sender, body=payload["comment"]["body"]):
```

### 4. Update `recover_tasks()` backfill filter (if applicable)

`recover_tasks()` at line 129 does not currently backfill comments — it only re-enqueues the issue title+body. No change needed there, but the recovery comment it posts uses `🤖` which is already covered by the predicate.

## Test Plan

- Unit test `_is_bot_comment()` with bot logins, `[bot]` suffix, each prefix, and normal user comments
- Unit test backfill filtering: mock `gh api` returning a mix of bot and user comments, verify only user comments are enqueued
- Unit test webhook comment filtering: verify bot comments (by login and by prefix) are ignored
- Integration test: simulate reopen with prior `✅` comments in history, verify no duplicate completion is produced
