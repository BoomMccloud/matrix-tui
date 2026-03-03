# Acceptance Criteria - Fix unawaited subprocess in issue_comment webhook handler

## Bug Fix
- [x] In `src/matrix_agent/channels.py`, the `issue_comment` event handler now correctly awaits the "Working on this issue..." subprocess.
- [x] The subprocess is created with `stdout=asyncio.subprocess.PIPE` and `stderr=asyncio.subprocess.PIPE`.
- [x] `await proc.communicate()` is called to ensure the process completes and is cleaned up.
- [x] Failed `gh` calls are logged with their stderr output.

## Validation Scripts
- [x] Fixed linting error in `scripts/validate_podman.py`: Removed unused `asyncio` import.
- [x] Fixed linting error in `scripts/validate_podman.py`: Removed unused assignment to `body`.
- [x] Fixed linting errors in `scripts/validate_screenshot.py`: Removed extraneous `f` prefix from strings without placeholders.

## Templates
- [x] Restored missing `src/matrix_agent/templates/GEMINI.md`.
- [x] Restored missing `src/matrix_agent/templates/status.md`.

## Testing
- [x] Added `test_webhook_issue_comment_posts_working_if_new` to `tests/test_channels_ci_fix.py` to verify the fix.
- [x] Added `test_webhook_issue_comment_skips_working_if_processing` to verify idempotency.
- [x] Added `test_webhook_issue_comment_ignores_bot` to verify bot filtering.
- [x] All tests in `tests/test_channels_ci_fix.py` pass.
- [x] Full test suite passes.
