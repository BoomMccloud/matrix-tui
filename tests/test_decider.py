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
