# Acceptance Criteria - Fix unawaited subprocess in issue_comment webhook handler

## Changes
- **src/matrix_agent/channels.py**: Fixed unawaited `asyncio.create_subprocess_exec` call in `issue_comment` handler. Now properly uses `await proc.communicate()` and captures `stdout`/`stderr`.
- **pyproject.toml**: Added missing dependencies `aiohttp` and `pydantic`.
- **scripts/validate_podman.py**: Fixed linting errors (F401 unused import `asyncio`, F841 unused variable `body`).
- **scripts/validate_screenshot.py**: Fixed linting errors (F541 extraneous `f`-prefix).
- **tests/test_channels_ci_fix.py**: Added new test cases to verify the fix.

## Verification Results
### Automated Tests
- Ran `uv run pytest tests/test_channels_ci_fix.py`:
    - `test_webhook_issue_comment_posts_working_if_not_processing`: PASSED
    - `test_webhook_issue_comment_skips_working_if_already_processing`: PASSED
    - Total: 8/8 tests passed in `tests/test_channels_ci_fix.py`.

### Linting
- Ran `uv run ruff check scripts/validate_podman.py scripts/validate_screenshot.py`:
    - All checks passed.

## PR Details
- All changes committed and ready for review.
