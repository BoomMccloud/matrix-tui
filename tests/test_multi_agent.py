"""Tests for multi-agent routing via decider: plan/review → Gemini, implement → Qwen."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _make_llm_response(tool_calls=None, content=None):
    """Build a mock LiteLLM response."""
    msg = SimpleNamespace(
        content=content or "",
        tool_calls=tool_calls,
    )
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(choices=[choice])


def _make_tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


@pytest.mark.asyncio
async def test_plan_implement_review_scenario():
    """Simulate decider calling plan → implement → run_tests → review → final response.

    Verifies:
    - All four tools are called in order
    - plan and review route to gemini (cli='gemini')
    - implement routes to qwen (cli='qwen')
    - The final text response is yielded
    - Context flows through conversation history (each tool result is in messages)
    """
    from matrix_agent.decider import Decider

    # Mock settings — only need fields the decider reads
    settings = SimpleNamespace(
        llm_model="test-model",
        llm_api_key="test-key",
        llm_api_base="",
        max_agent_turns=10,
    )

    # Mock sandbox — track calls to code_stream with cli param
    sandbox = AsyncMock()
    sandbox.save_state = lambda: None
    sandbox._histories = None

    call_log = []

    async def fake_code_stream(chat_id, task, on_chunk, cli="gemini", chunk_size=800):
        call_log.append({"tool": "code_stream", "cli": cli, "task": task})
        return (0, f"output from {cli}: done", "")

    sandbox.code_stream = fake_code_stream
    sandbox.code = AsyncMock(return_value=(0, "output", ""))
    sandbox.exec = AsyncMock(return_value=(0, "all tests pass", ""))

    decider = Decider(settings, sandbox)

    # Simulate 5 LLM turns:
    # Turn 1: call plan
    # Turn 2: call implement (with plan output as context)
    # Turn 3: call run_tests
    # Turn 4: call review
    # Turn 5: final text response
    llm_responses = [
        _make_llm_response(tool_calls=[
            _make_tool_call("call-1", "plan", {"task": "design auth system for this app"}),
        ]),
        _make_llm_response(tool_calls=[
            _make_tool_call("call-2", "implement", {"task": "implement JWT auth per plan: use middleware in auth.py"}),
        ]),
        _make_llm_response(tool_calls=[
            _make_tool_call("call-3", "run_tests", {"path": "/workspace"}),
        ]),
        _make_llm_response(tool_calls=[
            _make_tool_call("call-4", "review", {"task": "review the auth implementation in auth.py"}),
        ]),
        _make_llm_response(content="Done! I've planned, implemented, tested, and reviewed the auth system."),
    ]

    response_iter = iter(llm_responses)

    async def mock_acompletion(**kwargs):
        return next(response_iter)

    send_update = AsyncMock()

    with patch("matrix_agent.decider.litellm") as mock_litellm:
        mock_litellm.acompletion = mock_acompletion

        results = []
        async for text, image in decider.handle_message("!test:room", "add auth to the app", send_update=send_update):
            results.append((text, image))

    # Verify final response
    assert len(results) == 1
    assert "Done!" in results[0][0]

    # Verify tool call order and routing
    assert len(call_log) == 3  # plan, implement, review (run_tests goes through sandbox.exec)
    assert call_log[0]["cli"] == "gemini"
    assert call_log[0]["task"] == "design auth system for this app"
    assert call_log[1]["cli"] == "qwen"
    assert call_log[1]["task"] == "implement JWT auth per plan: use middleware in auth.py"
    assert call_log[2]["cli"] == "gemini"
    assert call_log[2]["task"] == "review the auth implementation in auth.py"

    # Verify run_tests went through sandbox.exec
    assert sandbox.exec.call_count == 2  # ruff + pytest

    # Verify conversation history has all tool results
    history = decider._histories["!test:room"]
    tool_results = [m for m in history if m.get("role") == "tool"]
    assert len(tool_results) == 4  # plan, implement, run_tests, review
