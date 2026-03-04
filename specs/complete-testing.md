# Feature: Complete Test Coverage

## Goal

Close critical test gaps, remove low-value tests, and ensure every important code path has coverage. After this work, a regression in any core subsystem should break at least one test.

---

## Phase 1: Remove Low-Value Tests

Delete or consolidate tests that don't guard real behavior:

- [ ] **Delete `tests/test_state.py`** — 2 tests that only verify `json.dump`/`json.load` + `os.replace`. Does not test `SandboxManager.save_state()` or `load_state()`.
- [ ] **Delete `_strip_ansi` tests from `tests/test_sandbox_utils.py`** — 7 tests for a one-liner regex. Keep only the `_container_name` tests.
- [ ] **Delete `tests/test_sandbox_auto_accept.py`** — 4 tests that verify `-y` appears in subprocess args. Tests a trivial conditional.
- [ ] **Delete template constant tests from `tests/test_sandbox_gemini.py`** — `test_templates_dir_constant_points_to_correct_path`, `test_templates_dict_has_expected_keys`, `test_all_template_files_exist_on_disk`. Replace with a single "all template files referenced in TEMPLATES exist on disk" assertion if desired.
- [ ] **Delete `test_channel_adapter_has_required_abstract_methods` from `tests/test_channels.py`** — tests Python ABC machinery, not application behavior.

---

## Phase 2: Add Critical Missing Tests

### 2a. SandboxManager crash recovery (`sandbox.py`)

- [ ] **`load_state()` stale container pruning** — Mock `_run("inspect", ...)` to return non-running status for one container and running for another. Verify the stale entry is removed from `_containers` and its history is dropped. Verify the running entry is kept.
- [ ] **`create()` "already in use" retry** — Mock first `podman run` to return `rc=125, stderr="already in use"`. Mock second call to succeed. Verify `podman rm -f` was called between attempts.
- [ ] **`create()` env var injection** — Mock `_run` and verify the podman command includes `-e GEMINI_API_KEY=...`, `-e GEMINI_MODEL=...`, `-e GITHUB_TOKEN=...` when settings have those values. Verify they're absent when settings values are empty.

### 2b. Decider edge cases (`decider.py`)

- [ ] **`handle_message()` max_turns** — Mock LiteLLM to always return tool calls (never a final text response). Verify the generator yields exactly once with status `"max_turns"` and the response includes the turn count. Verify `save_state()` is called.
- [ ] **History persistence round-trip** — Call `handle_message()` to completion. Verify `_histories[chat_id]` contains the user message, assistant response, and any tool calls. Call `load_histories()` with that data and verify the next `handle_message()` call includes prior context.

### 2c. TaskRunner timeout (`core.py`)

- [ ] **`_process()` timeout** — Create a `TaskRunner` with a mock decider whose `handle_message()` sleeps longer than `coding_timeout_seconds + 300`. Verify `channel.deliver_error()` is called with a message containing "timed out". Verify the task is cleaned up.

### 2d. GitHubChannel (`channels.py`)

- [ ] **`is_valid()` — open issue with label** — Mock `asyncio.create_subprocess_exec` for `gh issue view` to return JSON with `state: "OPEN"` and `labels: [{name: "agent-task"}]`. Verify returns `True`.
- [ ] **`is_valid()` — closed issue** — Mock to return `state: "CLOSED"`. Verify returns `False`.
- [ ] **`is_valid()` — open but no label** — Mock to return `state: "OPEN"` with no `agent-task` label. Verify returns `False`.
- [ ] **`_handle_webhook()` new issue_comment on unprocessed task** — Verify the "Working" comment is posted and the task is enqueued (lines 278–290).

### 2e. SandboxManager utilities (`sandbox.py`)

- [ ] **`get_host_port()`** — Mock `podman port` output (e.g., `0.0.0.0:12345`). Verify correct port is parsed. Test with no mapping (returns `None`).
- [ ] **`screenshot()`** — Mock `exec()` for the node script call, mock `_run("cp", ...)` and file read. Verify bytes are returned. Test failure case (non-zero rc).

---

## Phase 3: bot.py Coverage (stretch)

`bot.py` has zero test coverage. These are lower priority since they involve Matrix SDK mocking, but are important for completeness:

- [ ] **`MatrixChannel.send_update`** — Verify it calls `room_send` with correct room_id and chunked messages.
- [ ] **`MatrixChannel.deliver_result`** — Verify final message is sent to the correct room.
- [ ] **`MatrixChannel.is_valid`** — Verify it checks room membership.
- [ ] **Event deduplication** — Verify duplicate `m.room.message` events are ignored.
- [ ] **Room join logic** — Verify bot auto-joins on invite.

---

## Phase 4: config.py Validation (stretch)

- [ ] **Default values** — Instantiate `Settings` with minimal env vars. Verify defaults for `gemini_model`, `sandbox_image`, timeouts, etc.
- [ ] **Missing required fields** — Verify `ValidationError` when required fields (e.g., `matrix_homeserver`) are absent.

---

## Acceptance Criteria

- All Phase 1 deletions complete, no test count regression in meaningful coverage
- All Phase 2 tests written and passing
- `uv run pytest tests/` passes with 0 failures
- `uv run ruff check src tests` passes with 0 errors
- No integration tests added (all new tests use mocks/fakes, run without podman or API keys)
