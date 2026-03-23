"""Tests for the conversation manager."""

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.app.conversation import ConversationManager


@pytest.fixture
def conversation(mock_mcp_client, mock_tts):
    """A ConversationManager wired to mock MCP + TTS."""
    cm = ConversationManager(
        wake_word="hey homie",
        ollama_host="http://fake-ollama:11434",
        ollama_model="test-model",
        mcp_client=mock_mcp_client,
        tts_engine=mock_tts,
    )
    cm.on_response = AsyncMock()
    return cm


class TestWakeWordDetection:
    @pytest.mark.asyncio
    async def test_ignores_text_without_wake_word(self, conversation):
        await conversation.process_text("the weather is nice today")
        assert conversation.state == "idle"
        assert conversation._wake_detected is False
        assert conversation._command_buffer == []

    @pytest.mark.asyncio
    async def test_detects_wake_word(self, conversation):
        await conversation.process_text("hey homie turn on the lights")
        assert conversation._wake_detected is True
        assert conversation.state == "listening"

    @pytest.mark.asyncio
    async def test_extracts_text_after_wake_word(self, conversation):
        await conversation.process_text("hey homie turn on the lights")
        assert conversation._command_buffer == ["turn on the lights"]

    @pytest.mark.asyncio
    async def test_wake_word_case_insensitive(self, conversation):
        await conversation.process_text("Hey Homie what time is it")
        assert conversation._wake_detected is True
        assert conversation._command_buffer == ["what time is it"]

    @pytest.mark.asyncio
    async def test_wake_word_only_no_remainder(self, conversation):
        await conversation.process_text("hey homie")
        assert conversation._wake_detected is True
        assert conversation._command_buffer == []

    @pytest.mark.asyncio
    async def test_wake_word_embedded_in_sentence(self, conversation):
        await conversation.process_text("I said hey homie do something")
        assert conversation._wake_detected is True
        assert conversation._command_buffer == ["do something"]


class TestConversationState:
    @pytest.mark.asyncio
    async def test_in_conversation_after_wake_word(self, conversation):
        assert conversation.in_conversation is False
        await conversation.process_text("hey homie hello")
        assert conversation.in_conversation is True

    @pytest.mark.asyncio
    async def test_accumulates_during_conversation(self, conversation):
        await conversation.process_text("hey homie")
        await conversation.process_text("turn on the kitchen lights")
        await conversation.process_text("and the bedroom lights too")
        assert conversation._command_buffer == [
            "turn on the kitchen lights",
            "and the bedroom lights too",
        ]

    @pytest.mark.asyncio
    async def test_followup_window(self, conversation):
        conversation._last_response_time = time.monotonic()
        # Should be in conversation due to follow-up window
        assert conversation.in_conversation is True

    @pytest.mark.asyncio
    async def test_followup_window_expired(self, conversation):
        conversation._last_response_time = time.monotonic() - 20.0
        assert conversation.in_conversation is False

    @pytest.mark.asyncio
    async def test_followup_accumulates_without_wake_word(self, conversation):
        # Simulate just after a response
        conversation._last_response_time = time.monotonic()
        await conversation.process_text("also dim the lights")
        assert conversation._command_buffer == ["also dim the lights"]


class TestOnSilence:
    @pytest.mark.asyncio
    async def test_silence_does_nothing_when_not_in_conversation(self, conversation):
        await conversation.on_silence()
        assert conversation.state == "idle"
        conversation.on_response.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_silence_does_nothing_with_empty_buffer(self, conversation):
        conversation._wake_detected = True
        await conversation.on_silence()
        # No command buffer → nothing to process
        conversation.on_response.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_silence_triggers_llm_and_tts(self, conversation):
        # Set up the command
        await conversation.process_text("hey homie turn on the lights")

        # Mock Ollama response (no tool calls)
        with patch.object(conversation, "_chat_completion", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = {
                "message": {"content": "Done, I turned on the lights.", "role": "assistant"},
            }
            await conversation.on_silence()

        # Should have called TTS
        conversation.tts_engine.synthesize.assert_awaited_once_with("Done, I turned on the lights.")
        # Should have sent response
        conversation.on_response.assert_awaited_once()
        args = conversation.on_response.call_args
        assert args[0][0] == "Done, I turned on the lights."
        # State should return to idle
        assert conversation.state == "idle"

    @pytest.mark.asyncio
    async def test_silence_resets_wake_flag(self, conversation):
        await conversation.process_text("hey homie hello")
        assert conversation._wake_detected is True

        with patch.object(conversation, "_chat_completion", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = {"message": {"content": "Hi!", "role": "assistant"}}
            await conversation.on_silence()

        assert conversation._wake_detected is False

    @pytest.mark.asyncio
    async def test_silence_sets_followup_time(self, conversation):
        await conversation.process_text("hey homie hello")

        with patch.object(conversation, "_chat_completion", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = {"message": {"content": "Hi!", "role": "assistant"}}
            await conversation.on_silence()

        assert conversation._last_response_time > 0
        assert conversation.in_conversation is True  # within follow-up window


class TestLLMWithTools:
    @pytest.mark.asyncio
    async def test_simple_text_response(self, conversation):
        with patch.object(conversation, "_chat_completion", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = {
                "message": {"content": "The weather is nice.", "role": "assistant"},
            }
            result = await conversation._run_llm_with_tools("what's the weather?")

        assert result == "The weather is nice."
        assert conversation.history[-1]["content"] == "The weather is nice."

    @pytest.mark.asyncio
    async def test_tool_call_flow(self, conversation):
        call_count = 0

        async def fake_chat(messages):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: LLM wants to call a tool
                return {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "function": {
                                "name": "ha_call_service",
                                "arguments": {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"},
                            }
                        }],
                    }
                }
            else:
                # Second call: LLM gives final response after tool result
                return {
                    "message": {"content": "Kitchen light is on now.", "role": "assistant"},
                }

        conversation._chat_completion = fake_chat
        # Mock the MCP tool call
        conversation.mcp.call_tool = AsyncMock(return_value="Service called successfully")

        result = await conversation._run_llm_with_tools("turn on the kitchen light")

        assert result == "Kitchen light is on now."
        conversation.mcp.call_tool.assert_awaited_once_with(
            "ha_call_service",
            {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"},
        )

    @pytest.mark.asyncio
    async def test_tool_loop_limit(self, conversation):
        """Ensure the tool-calling loop stops after 5 rounds."""
        async def always_call_tool(messages):
            return {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "function": {"name": "ha_get_state", "arguments": {"entity_id": "light.test"}},
                    }],
                }
            }

        conversation._chat_completion = always_call_tool
        conversation.mcp.call_tool = AsyncMock(return_value="on")

        result = await conversation._run_llm_with_tools("infinite loop test")
        assert result == "I've completed the action."
        assert conversation.mcp.call_tool.await_count == 5

    @pytest.mark.asyncio
    async def test_history_trimming(self, conversation):
        conversation.max_history = 4
        # Add enough entries to exceed max
        for i in range(10):
            conversation.history.append({"role": "user", "content": f"msg {i}"})

        with patch.object(conversation, "_chat_completion", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = {"message": {"content": "ok", "role": "assistant"}}
            await conversation._run_llm_with_tools("latest")

        # History should be trimmed: the 10 old + 1 new user = 11, trimmed to 4, then +1 assistant = 5
        # Actually: history has 10 items. We append "latest" → 11. Trim to 4. Then append assistant → 5.
        assert len(conversation.history) <= 5


class TestChatCompletion:
    @pytest.mark.asyncio
    async def test_includes_tools_in_payload(self, conversation):
        with patch.object(conversation._http, "post", new_callable=AsyncMock) as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {"message": {"content": "hi"}}
            mock_post.return_value = mock_resp

            await conversation._chat_completion([{"role": "user", "content": "test"}])

            call_kwargs = mock_post.call_args
            payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            assert "tools" in payload
            assert payload["model"] == "test-model"
            assert payload["stream"] is False

    @pytest.mark.asyncio
    async def test_handles_ollama_error(self, conversation):
        with patch.object(conversation._http, "post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = Exception("connection refused")

            result = await conversation._chat_completion([{"role": "user", "content": "test"}])
            assert "trouble" in result["message"]["content"]


class TestSystemPrompt:
    def test_includes_tool_descriptions(self, conversation):
        prompt = conversation._build_system_prompt()
        assert "ha_call_service" in prompt
        assert "ha_get_state" in prompt
        assert "HAL" in prompt

    def test_no_tools(self, mock_tts):
        from server.app.mcp_client import MCPClient
        empty_mcp = MCPClient(server_url="http://x")
        cm = ConversationManager(
            wake_word="hey homie",
            ollama_host="http://x",
            ollama_model="m",
            mcp_client=empty_mcp,
            tts_engine=mock_tts,
        )
        prompt = cm._build_system_prompt()
        assert "(no tools available)" in prompt
