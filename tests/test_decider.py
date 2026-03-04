"""Unit tests for Decider.handle_message() max_turns limit."""
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from matrix_agent.decider import Decider

@pytest.mark.asyncio
async def test_handle_message_max_turns():
    # Create a Decider with a mock SandboxManager and Settings
    settings = MagicMock()
    settings.max_agent_turns = 3
    settings.llm_model = "gpt-4"
    settings.llm_api_key = "fake-key"
    settings.llm_api_base = None

    sandbox = MagicMock()
    sandbox.save_state = MagicMock()
    # Decider.__init__ sets self.sandbox._histories = self._histories
    
    decider = Decider(settings, sandbox)

    # Mock litellm.acompletion to always return tool calls
    mock_tool_call = MagicMock()
    mock_tool_call.id = "call_123"
    mock_tool_call.function.name = "run_command"
    mock_tool_call.function.arguments = '{"command": "ls"}'

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "Thinking..."
    mock_response.choices[0].message.tool_calls = [mock_tool_call]

    # Mock execute_tool to return a dummy result without an image
    with (
        patch("matrix_agent.decider.litellm.acompletion", new_callable=AsyncMock) as mock_acompletion,
        patch("matrix_agent.decider.execute_tool", new_callable=AsyncMock) as mock_execute_tool
    ):
        mock_acompletion.return_value = mock_response
        mock_execute_tool.return_value = ("Success", None)

        # Call handle_message() and collect yielded results
        results = []
        async for res in decider.handle_message("chat-1", "Hello"):
            results.append(res)

        # Verify the generator yields exactly once with status "max_turns"
        # Since execute_tool returns no image, only the final max_turns yield occurs.
        # This confirms that the loop terminates correctly.
        assert len(results) == 1
        text, image, status = results[0]
        assert status == "max_turns"
        assert "Reached maximum turns" in text
        assert f"({settings.max_agent_turns})" in text
        assert image is None

        # Verify save_state() is called
        sandbox.save_state.assert_called_once()
        
        # Verify litellm was called exactly max_turns times
        assert mock_acompletion.call_count == settings.max_agent_turns
        
        # Verify execute_tool was called for each tool call in each turn
        assert mock_execute_tool.call_count == settings.max_agent_turns

@pytest.mark.asyncio
async def test_handle_message_history_accumulation():
    # Setup Decider with mock Settings and SandboxManager
    settings = MagicMock()
    settings.max_agent_turns = 5
    settings.llm_model = "gpt-4"
    settings.llm_api_key = "fake-key"
    settings.llm_api_base = None

    sandbox = MagicMock()
    sandbox.save_state = MagicMock()
    
    decider = Decider(settings, sandbox)

    # Response 1: Tool Call
    mock_tool_call = MagicMock()
    mock_tool_call.id = "call_1"
    mock_tool_call.function.name = "run_command"
    mock_tool_call.function.arguments = '{"command": "ls"}'

    resp1 = MagicMock()
    resp1.choices = [MagicMock()]
    resp1.choices[0].message.content = "Checking files..."
    resp1.choices[0].message.tool_calls = [mock_tool_call]

    # Response 2: Final Text
    resp2 = MagicMock()
    resp2.choices = [MagicMock()]
    resp2.choices[0].message.content = "Found files."
    resp2.choices[0].message.tool_calls = None

    with (
        patch("matrix_agent.decider.litellm.acompletion", new_callable=AsyncMock) as mock_acompletion,
        patch("matrix_agent.decider.execute_tool", new_callable=AsyncMock) as mock_execute_tool
    ):
        # Capture messages at the time of each call
        captured_messages = []
        responses = [resp1, resp2]
        
        async def side_effect(*args, **kwargs):
            captured_messages.append(list(kwargs["messages"]))
            return responses.pop(0)
            
        mock_acompletion.side_effect = side_effect
        mock_execute_tool.return_value = ("file1.txt", None)

        # Call handle_message
        results = []
        async for res in decider.handle_message("chat-history", "list files"):
            results.append(res)

        # Verify acompletion was called twice
        assert len(captured_messages) == 2
        
        # Verify first call messages: system + user
        first_call = captured_messages[0]
        assert len(first_call) == 2
        assert first_call[0]["role"] == "system"
        assert first_call[1]["role"] == "user"
        assert first_call[1]["content"] == "list files"

        # Verify second call messages: system + user + assistant(tool_call) + tool(result)
        second_call = captured_messages[1]
        assert len(second_call) == 4
        
        # Assistant message with tool call
        assert second_call[2]["role"] == "assistant"
        assert second_call[2]["content"] == "Checking files..."
        assert "tool_calls" in second_call[2]
        assert second_call[2]["tool_calls"][0]["id"] == "call_1"
        assert second_call[2]["tool_calls"][0]["function"]["name"] == "run_command"

        # Tool result message
        assert second_call[3]["role"] == "tool"
        assert second_call[3]["tool_call_id"] == "call_1"
        assert second_call[3]["content"] == "file1.txt"

        # Verify final result
        assert "Found files." in results[0][0]
        assert results[0][2] == "completed"
