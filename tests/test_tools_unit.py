"""Unit tests for matrix_agent.tools."""

from unittest.mock import AsyncMock

from matrix_agent.tools import (
    _shell_quote,
    execute_tool,
    _create_pull_request,
    _is_git_push_blocked,
)


# ------------------------------------------------------------------ #
# _shell_quote tests
# ------------------------------------------------------------------ #


class TestShellQuote:
    """Tests for _shell_quote function."""

    def test_basic_strings(self):
        """Basic strings without special characters."""
        assert _shell_quote("hello") == "'hello'"
        assert _shell_quote("world") == "'world'"

    def test_strings_with_spaces(self):
        """Strings containing spaces."""
        assert _shell_quote("hello world") == "'hello world'"
        assert _shell_quote("  leading and trailing  ") == "'  leading and trailing  '"

    def test_strings_with_single_quotes(self):
        """Strings containing single quotes."""
        assert _shell_quote("it's") == "'it'\\''s'"
        assert _shell_quote("don't") == "'don'\\''t'"
        assert _shell_quote("'quoted'") == "''\\''quoted'\\'''"

    def test_empty_string(self):
        """Empty string."""
        assert _shell_quote("") == "''"

    def test_shell_specials(self):
        """Shell special characters."""
        assert _shell_quote("$HOME") == "'$HOME'"
        assert _shell_quote("`cmd`") == "'`cmd`'"
        assert _shell_quote("$(cmd)") == "'$(cmd)'"
        assert _shell_quote("a & b") == "'a & b'"
        assert _shell_quote("a | b") == "'a | b'"
        assert _shell_quote("a > b") == "'a > b'"
        assert _shell_quote("a < b") == "'a < b'"
        assert _shell_quote("a * b") == "'a * b'"
        assert _shell_quote("a ? b") == "'a ? b'"


# ------------------------------------------------------------------ #
# _is_git_push_blocked tests
# ------------------------------------------------------------------ #


class TestIsGitPushBlocked:
    """Tests for _is_git_push_blocked function."""

    def test_block_git_push(self):
        """Standard git push should be blocked."""
        assert _is_git_push_blocked("git push") is True
        assert _is_git_push_blocked("git push origin main") is True

    def test_allow_force(self):
        """git push --force should be allowed."""
        assert _is_git_push_blocked("git push --force") is False
        assert _is_git_push_blocked("git push origin main --force") is False

    def test_allow_force_with_lease(self):
        """git push --force-with-lease should be allowed."""
        assert _is_git_push_blocked("git push --force-with-lease") is False
        assert _is_git_push_blocked("git push --force-with-lease origin feat-branch") is False

    def test_allow_other_commands(self):
        """Other commands should not be blocked."""
        assert _is_git_push_blocked("git status") is False
        assert _is_git_push_blocked("git pull") is False
        assert _is_git_push_blocked("ls -la") is False


# ------------------------------------------------------------------ #
# execute_tool tests
# ------------------------------------------------------------------ #


class TestExecuteTool:
    """Tests for execute_tool function."""

    def _make_sandbox(self):
        """Create a mock SandboxManager."""
        sandbox = AsyncMock()
        sandbox._containers = {}
        return sandbox

    async def test_run_command_basic(self):
        """run_command: verify sandbox exec call."""
        sandbox = self._make_sandbox()
        sandbox.exec.return_value = (0, "output", "")

        result, image = await execute_tool(sandbox, "chat-1", "run_command", '{"command": "echo hello"}')

        sandbox.exec.assert_called_once_with("chat-1", "echo hello")
        assert result == "output"

    async def test_run_command_with_stderr(self):
        """run_command: includes stderr in output."""
        sandbox = self._make_sandbox()
        sandbox.exec.return_value = (0, "stdout", "stderr msg")

        result, image = await execute_tool(sandbox, "chat-1", "run_command", '{"command": "echo hello"}')

        assert "STDERR:" in result
        assert "stderr msg" in result

    async def test_run_command_with_exit_code(self):
        """run_command: includes exit code on non-zero."""
        sandbox = self._make_sandbox()
        sandbox.exec.return_value = (1, "output", "")

        result, image = await execute_tool(sandbox, "chat-1", "run_command", '{"command": "exit 1"}')

        assert "[exit code: 1]" in result

    async def test_run_command_git_push_blocked(self):
        """run_command: block git push without --force."""
        sandbox = self._make_sandbox()

        result, image = await execute_tool(sandbox, "chat-1", "run_command", '{"command": "git push origin main"}')

        assert "Error: git push is not allowed" in result
        sandbox.exec.assert_not_called()

    async def test_run_command_git_push_force_allowed(self):
        """run_command: allow git push --force."""
        sandbox = self._make_sandbox()
        sandbox.exec.return_value = (0, "pushed", "")

        result, image = await execute_tool(sandbox, "chat-1", "run_command", '{"command": "git push --force origin feature"}')

        sandbox.exec.assert_called_once()
        assert "pushed" in result

    async def test_run_command_git_status_allowed(self):
        """run_command: allow other git commands."""
        sandbox = self._make_sandbox()
        sandbox.exec.return_value = (0, "On branch main", "")

        result, image = await execute_tool(sandbox, "chat-1", "run_command", '{"command": "git status"}')

        sandbox.exec.assert_called_once()
        assert "On branch main" in result

    async def test_write_file(self):
        """write_file: verify sandbox write_file call."""
        sandbox = self._make_sandbox()
        sandbox.write_file.return_value = "Wrote 100 bytes to /test.txt"

        result, image = await execute_tool(
            sandbox, "chat-1", "write_file", '{"path": "/test.txt", "content": "hello"}'
        )

        sandbox.write_file.assert_called_once_with("chat-1", "/test.txt", "hello")
        assert "Wrote 100 bytes" in result

    async def test_read_file(self):
        """read_file: verify sandbox read_file call."""
        sandbox = self._make_sandbox()
        sandbox.read_file.return_value = "file content"

        result, image = await execute_tool(sandbox, "chat-1", "read_file", '{"path": "/test.txt"}')

        sandbox.read_file.assert_called_once_with("chat-1", "/test.txt")
        assert result == "file content"

    async def test_plan_routing(self):
        """plan: verify routing to gemini CLI."""
        sandbox = self._make_sandbox()
        sandbox.code.return_value = (0, "plan output", "")

        result, image = await execute_tool(sandbox, "chat-1", "plan", '{"task": "plan this"}')

        sandbox.code.assert_called_once()
        call_args = sandbox.code.call_args
        assert call_args[1]["cli"] == "gemini"

    async def test_implement_routing(self):
        """implement: verify routing to qwen CLI."""
        sandbox = self._make_sandbox()
        sandbox.code.return_value = (0, "implementation done", "")

        result, image = await execute_tool(sandbox, "chat-1", "implement", '{"task": "implement this"}')

        sandbox.code.assert_called_once()
        call_args = sandbox.code.call_args
        assert call_args[1]["cli"] == "qwen"

    async def test_review_routing(self):
        """review: verify routing to gemini CLI."""
        sandbox = self._make_sandbox()
        sandbox.code.return_value = (0, "reviewed", "")

        result, image = await execute_tool(sandbox, "chat-1", "review", '{"task": "review this"}')

        sandbox.code.assert_called_once()
        call_args = sandbox.code.call_args
        assert call_args[1]["cli"] == "gemini"

    async def test_plan_with_send_update(self):
        """plan: verify send_update is passed through."""
        sandbox = self._make_sandbox()
        sandbox.code_stream.return_value = (0, "streaming plan", "")

        async def send_update(chunk):
            pass

        result, image = await execute_tool(
            sandbox, "chat-1", "plan", '{"task": "plan this"}', send_update=send_update
        )

        sandbox.code_stream.assert_called_once()
        call_args = sandbox.code_stream.call_args
        assert call_args[1]["cli"] == "gemini"
        assert call_args[1]["auto_accept"] is True

    async def test_run_tests_lint_and_test(self):
        """run_tests: verify it runs both ruff and pytest."""
        sandbox = self._make_sandbox()
        sandbox.exec.side_effect = [
            (0, "No issues.", ""),  # ruff
            (0, "passed", ""),       # pytest
        ]

        result, image = await execute_tool(sandbox, "chat-1", "run_tests", '{"path": "/workspace"}')

        assert sandbox.exec.call_count == 2
        calls = [c[0][1] for c in sandbox.exec.call_args_list]
        assert "ruff check ." in calls[0]
        assert "pytest -v" in calls[1]
        assert "[PASS]" in result
        assert "=== Lint (ruff) ===" in result
        assert "=== Tests (pytest) ===" in result

    async def test_run_tests_fail_on_lint(self):
        """run_tests: FAIL status when lint fails."""
        sandbox = self._make_sandbox()
        sandbox.exec.side_effect = [
            (1, "lint error", ""),  # ruff fails
            (0, "passed", ""),      # pytest passes
        ]

        result, image = await execute_tool(sandbox, "chat-1", "run_tests", '{}')

        assert "[FAIL]" in result

    async def test_run_tests_fail_on_test(self):
        """run_tests: FAIL status when tests fail."""
        sandbox = self._make_sandbox()
        sandbox.exec.side_effect = [
            (0, "No issues.", ""),  # ruff passes
            (1, "test failed", ""), # pytest fails
        ]

        result, image = await execute_tool(sandbox, "chat-1", "run_tests", '{}')

        assert "[FAIL]" in result

    async def test_take_screenshot_success(self):
        """take_screenshot: verify sandbox screenshot call."""
        sandbox = self._make_sandbox()
        sandbox.screenshot.return_value = b"png data"

        result, image = await execute_tool(sandbox, "chat-1", "take_screenshot", '{"url": "http://localhost:3000"}')

        sandbox.screenshot.assert_called_once_with("chat-1", "http://localhost:3000")
        assert result == "Screenshot taken successfully."
        assert image == b"png data"

    async def test_take_screenshot_failure(self):
        """take_screenshot: handle failure."""
        sandbox = self._make_sandbox()
        sandbox.screenshot.return_value = None

        result, image = await execute_tool(sandbox, "chat-1", "take_screenshot", '{"url": "http://localhost:3000"}')

        assert result == "Screenshot failed."
        assert image is None

    async def test_unknown_tool(self):
        """unknown tool: verify error message."""
        sandbox = self._make_sandbox()

        result, image = await execute_tool(sandbox, "chat-1", "unknown_tool", '{}')

        assert result == "Unknown tool: unknown_tool"

    async def test_run_command_output_truncation(self):
        """run_command: output > 10000 chars is truncated."""
        sandbox = self._make_sandbox()
        long_output = "x" * 11000
        sandbox.exec.return_value = (0, long_output, "")

        result, image = await execute_tool(sandbox, "chat-1", "run_command", '{"command": "echo long"}')

        assert len(result) == 10000 + len("\n... (truncated)")
        assert result.endswith("\n... (truncated)")

    async def test_read_file_output_truncation(self):
        """read_file: output > 10000 chars is truncated."""
        sandbox = self._make_sandbox()
        long_content = "y" * 11000
        sandbox.read_file.return_value = long_content

        result, image = await execute_tool(sandbox, "chat-1", "read_file", '{"path": "/test.txt"}')

        assert len(result) == 10000 + len("\n... (truncated)")
        assert result.endswith("\n... (truncated)")

    async def test_run_tests_custom_command(self):
        """run_tests: verify it uses the custom command when provided."""
        sandbox = self._make_sandbox()
        sandbox.exec.side_effect = [
            (0, "No issues.", ""),  # ruff
            (0, "custom output", ""), # custom command
        ]

        result, image = await execute_tool(
            sandbox, "chat-1", "run_tests", '{"command": "pytest tests/test_foo.py"}'
        )

        assert sandbox.exec.call_count == 2
        calls = [c[0][1] for c in sandbox.exec.call_args_list]
        assert "pytest tests/test_foo.py" in calls[1]
        assert "custom output" in result

    async def test_run_tests_custom_command_failure(self):
        """run_tests: FAIL status when custom command fails."""
        sandbox = self._make_sandbox()
        sandbox.exec.side_effect = [
            (0, "No issues.", ""),  # ruff passes
            (1, "custom test failed", "stderr message"),  # custom command fails
        ]

        result, image = await execute_tool(
            sandbox, "chat-1", "run_tests", '{"command": "pytest tests/test_foo.py"}'
        )

        assert sandbox.exec.call_count == 2
        calls = [c[0][1] for c in sandbox.exec.call_args_list]
        assert "pytest tests/test_foo.py" in calls[1]
        assert "[FAIL]" in result
        assert "custom test failed" in result
        assert "STDERR:" in result
        assert "stderr message" in result

    async def test_run_tests_path_quoting(self):
        """run_tests: path is shell-quoted."""
        sandbox = self._make_sandbox()
        sandbox.exec.return_value = (0, "ok", "")

        await execute_tool(sandbox, "chat-1", "run_tests", '{"path": "/path with spaces"}')

        calls = [c[0][1] for c in sandbox.exec.call_args_list]
        assert "cd '/path with spaces'" in calls[0]
        assert "cd '/path with spaces'" in calls[1]

    async def test_run_tests_blocks_git_push(self):
        """run_tests: blocks git push without --force."""
        sandbox = self._make_sandbox()

        result, image = await execute_tool(
            sandbox, "chat-1", "run_tests", '{"command": "git push origin main"}'
        )

        assert "Error: git push is not allowed" in result
        sandbox.exec.assert_not_called()

    async def test_run_tests_allows_git_push_force(self):
        """run_tests: allows git push --force."""
        sandbox = self._make_sandbox()
        sandbox.exec.return_value = (0, "pushed", "")

        result, image = await execute_tool(
            sandbox, "chat-1", "run_tests", '{"command": "git push --force origin feat"}'
        )

        assert sandbox.exec.call_count == 2
        assert "pushed" in result

    async def test_run_tests_output_truncation(self):
        """run_tests: output > 10000 chars is truncated."""
        sandbox = self._make_sandbox()
        long_output = "z" * 6000
        sandbox.exec.side_effect = [
            (0, long_output, ""),  # ruff
            (0, long_output, ""),  # pytest
        ]

        result, image = await execute_tool(sandbox, "chat-1", "run_tests", '{}')

        assert len(result) == 10000 + len("\n... (truncated)")
        assert result.endswith("\n... (truncated)")


# ------------------------------------------------------------------ #
# Git Push Block Guard tests
# ------------------------------------------------------------------ #


class TestGitPushBlockGuard:
    """Tests for git push blocking logic in execute_tool."""

    def _make_sandbox(self):
        sandbox = AsyncMock()
        sandbox._containers = {}
        return sandbox

    async def test_block_git_push(self):
        """Block 'git push'."""
        sandbox = self._make_sandbox()

        result, _ = await execute_tool(sandbox, "chat-1", "run_command", '{"command": "git push"}')

        assert "Error: git push is not allowed" in result
        sandbox.exec.assert_not_called()

    async def test_block_git_push_origin_main(self):
        """Block 'git push origin main'."""
        sandbox = self._make_sandbox()

        result, _ = await execute_tool(sandbox, "chat-1", "run_command", '{"command": "git push origin main"}')

        assert "Error: git push is not allowed" in result

    async def test_allow_git_push_force(self):
        """Allow 'git push --force'."""
        sandbox = self._make_sandbox()
        sandbox.exec.return_value = (0, "pushed", "")

        result, _ = await execute_tool(sandbox, "chat-1", "run_command", '{"command": "git push --force origin main"}')

        sandbox.exec.assert_called_once()
        assert "pushed" in result

    async def test_allow_other_git_commands(self):
        """Allow other git commands like 'git status'."""
        sandbox = self._make_sandbox()
        sandbox.exec.return_value = (0, "On branch main", "")

        result, _ = await execute_tool(sandbox, "chat-1", "run_command", '{"command": "git status"}')

        sandbox.exec.assert_called_once()
        assert "On branch main" in result

    async def test_allow_git_push_with_other_flags(self):
        """Allow 'git push' with other flags."""
        sandbox = self._make_sandbox()
        sandbox.exec.return_value = (0, "pushed", "")

        result, _ = await execute_tool(sandbox, "chat-1", "run_command", '{"command": "git push --force-with-lease origin feature"}')

        sandbox.exec.assert_called_once()

    async def test_allow_git_pull(self):
        """Allow 'git pull' (not blocked)."""
        sandbox = self._make_sandbox()
        sandbox.exec.return_value = (0, "Already up to date", "")

        result, _ = await execute_tool(sandbox, "chat-1", "run_command", '{"command": "git pull"}')

        sandbox.exec.assert_called_once()


# ------------------------------------------------------------------ #
# Output Truncation tests
# ------------------------------------------------------------------ #


class TestOutputTruncation:
    """Tests for output truncation (>10000 chars)."""

    def _make_sandbox(self):
        sandbox = AsyncMock()
        sandbox._containers = {}
        return sandbox

    async def test_run_command_truncation_boundary(self):
        """run_command: truncation at exactly 10000 chars boundary."""
        sandbox = self._make_sandbox()
        sandbox.exec.return_value = (0, "x" * 10000, "")

        result, _ = await execute_tool(sandbox, "chat-1", "run_command", '{"command": "echo"}')

        # Should not be truncated since exactly 10000
        assert len(result) == 10000
        assert not result.endswith("(truncated)")

    async def test_run_command_truncation_over_boundary(self):
        """run_command: truncation when over 10000 chars."""
        sandbox = self._make_sandbox()
        sandbox.exec.return_value = (0, "x" * 10001, "")

        result, _ = await execute_tool(sandbox, "chat-1", "run_command", '{"command": "echo"}')

        assert len(result) == 10000 + len("\n... (truncated)")
        assert result.endswith("\n... (truncated)")

    async def test_read_file_truncation(self):
        """read_file: truncation over 10000 chars."""
        sandbox = self._make_sandbox()
        sandbox.read_file.return_value = "y" * 15000

        result, _ = await execute_tool(sandbox, "chat-1", "read_file", '{"path": "/test.txt"}')

        assert len(result) == 10000 + len("\n... (truncated)")
        assert result.endswith("\n... (truncated)")

    async def test_plan_implement_review_truncation(self):
        """plan/implement/review: truncation over 10000 chars."""
        sandbox = self._make_sandbox()
        sandbox.code.return_value = (0, "z" * 15000, "")

        for tool in ["plan", "implement", "review"]:
            result, _ = await execute_tool(sandbox, "chat-1", tool, '{"task": "long task"}')

            assert len(result) == 10000 + len("\n... (truncated)")
            assert result.endswith("\n... (truncated)")

    async def test_run_tests_truncation(self):
        """run_tests: truncation over 10000 chars."""
        sandbox = self._make_sandbox()
        long_part = "t" * 5500
        sandbox.exec.side_effect = [
            (0, long_part, ""),  # ruff
            (0, long_part, ""),  # pytest
        ]

        result, _ = await execute_tool(sandbox, "chat-1", "run_tests", '{}')

        assert len(result) == 10000 + len("\n... (truncated)")
        assert result.endswith("\n... (truncated)")


# ------------------------------------------------------------------ #
# create_pull_request tests
# ------------------------------------------------------------------ #


class TestCreatePullRequest:
    """Tests for _create_pull_request function."""

    def _make_sandbox(self):
        sandbox = AsyncMock()
        sandbox._containers = {}
        return sandbox

    async def test_branch_slug_generation(self):
        """Branch name generation: lowercase, alphanumeric, dash-separated, max 50 chars."""
        sandbox = self._make_sandbox()
        # Simulate finding a repo
        sandbox.exec.side_effect = [
            (0, "/workspace/.git", ""),  # find .git
            (0, "", ""),                  # checkout -b
            (0, "", ""),                  # git add
            (0, "", ""),                  # git commit
            (0, "", ""),                  # git push
            (0, "https://github.com/owner/repo/pull/123", ""),  # gh pr create
        ]

        await _create_pull_request(sandbox, "chat-1", "Fix Bug", "Fixes issue")

        # Check branch name is lowercase, dash-separated
        calls = [c[0][1] for c in sandbox.exec.call_args_list]
        # Find the checkout command
        checkout_call = next(c for c in calls if "git checkout -b" in c)
        assert "agent/fix-bug" in checkout_call

    async def test_branch_slug_max_length(self):
        """Branch name generation: slug truncated to 50 chars, branch name includes agent/ prefix."""
        sandbox = self._make_sandbox()
        # Title that generates a slug longer than 50 chars
        long_title = "This is a very long title that exceeds the fifty character limit"
        sandbox.exec.side_effect = [
            (0, "/workspace/.git", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "https://github.com/owner/repo/pull/123", ""),
        ]

        await _create_pull_request(sandbox, "chat-1", long_title, "Body")

        calls = [c[0][1] for c in sandbox.exec.call_args_list]
        checkout_call = next(c for c in calls if "git checkout -b" in c)
        # Extract branch name (format: agent/<slug>)
        branch_name = checkout_call.split()[-1]
        # The slug is truncated to 50 chars, but branch name includes "agent/" prefix (7 chars)
        # So branch name can be up to 57 chars (7 + 50)
        assert len(branch_name) <= 57
        # Verify slug is properly truncated
        slug = branch_name.replace("agent/", "")
        assert len(slug) <= 50

    async def test_branch_slug_special_chars_replaced(self):
        """Branch name generation: special chars replaced with dashes."""
        sandbox = self._make_sandbox()
        sandbox.exec.side_effect = [
            (0, "/workspace/.git", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "https://github.com/owner/repo/pull/123", ""),
        ]

        await _create_pull_request(sandbox, "chat-1", "Fix: Bug #123!", "Body")

        calls = [c[0][1] for c in sandbox.exec.call_args_list]
        checkout_call = next(c for c in calls if "git checkout -b" in c)
        assert "agent/fix-bug-123" in checkout_call

    async def test_gh_chat_id_prefix_closes_issue(self):
        """gh- chat_id prefix: adds 'Closes #issue' to PR body."""
        sandbox = self._make_sandbox()
        sandbox.exec.side_effect = [
            (0, "/workspace/.git", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "https://github.com/owner/repo/pull/123", ""),
        ]

        await _create_pull_request(sandbox, "gh-42", "Fix bug", "Original body")

        calls = [c[0][1] for c in sandbox.exec.call_args_list]
        # Find the gh pr create command
        pr_create_call = next(c for c in calls if "gh pr create" in c)
        # The body should include Closes #42
        assert "Closes #42" in pr_create_call

    async def test_non_gh_chat_id_no_closes_prefix(self):
        """Non-gh chat_id: does not add 'Closes #issue' to PR body."""
        sandbox = self._make_sandbox()
        sandbox.exec.side_effect = [
            (0, "/workspace/.git", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "https://github.com/owner/repo/pull/123", ""),
        ]

        await _create_pull_request(sandbox, "matrix-123", "Fix bug", "Original body")

        calls = [c[0][1] for c in sandbox.exec.call_args_list]
        pr_create_call = next(c for c in calls if "gh pr create" in c)
        assert "Closes #" not in pr_create_call

    async def test_no_git_repo_found(self):
        """No git repo: returns error message."""
        sandbox = self._make_sandbox()
        sandbox.exec.return_value = (1, "", "No .git found")

        result = await _create_pull_request(sandbox, "chat-1", "Fix bug", "Body")

        assert "No git repository found" in result

    async def test_checkout_fails(self):
        """Checkout failure: returns error."""
        sandbox = self._make_sandbox()
        sandbox.exec.side_effect = [
            (0, "/workspace/.git", ""),
            (1, "", "checkout failed"),  # checkout -b fails
        ]

        result = await _create_pull_request(sandbox, "chat-1", "Fix bug", "Body")

        assert "Failed at" in result
        assert "git checkout -b" in result

    async def test_commit_fails(self):
        """Commit failure: returns error."""
        sandbox = self._make_sandbox()
        sandbox.exec.side_effect = [
            (0, "/workspace/.git", ""),
            (0, "", ""),
            (0, "", ""),
            (1, "", "commit failed"),  # git commit fails
        ]

        result = await _create_pull_request(sandbox, "chat-1", "Fix bug", "Body")

        assert "Failed at" in result
        assert "git commit" in result

    async def test_push_fails(self):
        """Push failure: returns error."""
        sandbox = self._make_sandbox()
        sandbox.exec.side_effect = [
            (0, "/workspace/.git", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (1, "", "push failed"),  # git push fails
        ]

        result = await _create_pull_request(sandbox, "chat-1", "Fix bug", "Body")

        assert "Failed at" in result
        assert "git push" in result

    async def test_pr_create_fails(self):
        """PR create failure: returns error."""
        sandbox = self._make_sandbox()
        sandbox.exec.side_effect = [
            (0, "/workspace/.git", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (1, "", "pr create failed"),  # gh pr create fails
        ]

        result = await _create_pull_request(sandbox, "chat-1", "Fix bug", "Body")

        assert "Failed at" in result
        assert "gh pr create" in result

    async def test_returns_pr_url_on_success(self):
        """Success: returns PR URL."""
        sandbox = self._make_sandbox()
        sandbox.exec.side_effect = [
            (0, "/workspace/.git", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "https://github.com/owner/repo/pull/123", ""),
        ]

        result = await _create_pull_request(sandbox, "chat-1", "Fix bug", "Body")

        assert result == "https://github.com/owner/repo/pull/123"

    async def test_repo_in_subdirectory(self):
        """Repo found in subdirectory: uses correct path."""
        sandbox = self._make_sandbox()
        sandbox.exec.side_effect = [
            (0, "/workspace/subdir/.git", ""),  # Found in subdir
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "https://github.com/owner/repo/pull/123", ""),
        ]

        await _create_pull_request(sandbox, "chat-1", "Fix bug", "Body")

        calls = [c[0][1] for c in sandbox.exec.call_args_list]
        # All commands after the find command should be prefixed with cd /workspace/subdir
        # The first call is the find command, skip it
        for call in calls[1:]:
            assert call.startswith("cd /workspace/subdir &&")
