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
    async def test_wake_word_fires_callback(self, conversation):
        conversation.on_wake_word = AsyncMock()
        await conversation.process_text("hey homie do something")
        conversation.on_wake_word.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_wake_word_no_callback_if_in_followup(self, conversation):
        conversation.on_wake_word = AsyncMock()
        conversation._awaiting_followup = True  # LLM asked a question
        await conversation.process_text("hey homie do something")
        # In follow-up, text is accumulated directly — no wake word trigger
        conversation.on_wake_word.assert_not_awaited()

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
        assert conversation._command_buffer == ["I said do something"]


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
    async def test_followup_when_question(self, conversation):
        conversation._awaiting_followup = True
        assert conversation.in_conversation is True

    @pytest.mark.asyncio
    async def test_no_followup_when_not_question(self, conversation):
        conversation._awaiting_followup = False
        assert conversation.in_conversation is False

    @pytest.mark.asyncio
    async def test_followup_accumulates_without_wake_word(self, conversation):
        # LLM asked a question — follow-up active
        conversation._awaiting_followup = True
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
    async def test_silence_enables_followup_on_question(self, conversation):
        await conversation.process_text("hey homie hello")

        with patch.object(conversation, "_chat_completion", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = {"message": {"content": "What would you like?", "role": "assistant"}}
            await conversation.on_silence()

        assert conversation._awaiting_followup is True
        assert conversation.in_conversation is True

    @pytest.mark.asyncio
    async def test_silence_no_followup_on_statement(self, conversation):
        await conversation.process_text("hey homie hello")

        with patch.object(conversation, "_chat_completion", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = {"message": {"content": "Done, light is on.", "role": "assistant"}}
            await conversation.on_silence()

        assert conversation._awaiting_followup is False
        assert conversation.in_conversation is False


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
        """Ensure the tool-calling loop stops after 8 rounds."""
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
        assert conversation.mcp.call_tool.await_count == 8

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


class TestCloudOverride:
    """Cloud LLM override: transport swap only — router/OpenClaw skipped,
    per-turn local fallback, summarization stays local, switch never flipped."""

    def _ollama_resp(self, content="local says hi"):
        r = MagicMock()
        r.status_code = 200
        r.raise_for_status = MagicMock()
        r.json.return_value = {"message": {"content": content}}
        return r

    def _enable_cloud(self, conversation, chat=None):
        cloud = MagicMock()
        cloud.chat = chat or AsyncMock(return_value={"message": {"content": "cloud says hi"}})
        conversation.cloud_llm = cloud
        conversation.cloud_llm_model = "openai/gpt-test"
        return cloud

    @pytest.mark.asyncio
    async def test_chat_completion_delegates_to_cloud(self, conversation):
        cloud = self._enable_cloud(conversation)
        with patch.object(conversation._http, "post", new_callable=AsyncMock) as mock_post:
            out = await conversation._chat_completion([{"role": "user", "content": "x"}])
        assert out["message"]["content"] == "cloud says hi"
        cloud.chat.assert_awaited_once()
        mock_post.assert_not_awaited()                  # Ollama HTTP untouched
        assert conversation._last_ollama_metrics["model"] == "openai/gpt-test"

    @pytest.mark.asyncio
    async def test_with_tools_false_stays_local(self, conversation):
        cloud = self._enable_cloud(conversation)
        with patch.object(conversation._http, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = self._ollama_resp("summary")
            out = await conversation._chat_completion(
                [{"role": "user", "content": "summarize"}], with_tools=False)
        assert out["message"]["content"] == "summary"
        cloud.chat.assert_not_awaited()                 # compaction never burns cloud tokens

    @pytest.mark.asyncio
    async def test_cloud_skips_router_and_openclaw(self, conversation):
        self._enable_cloud(conversation)
        conversation.openclaw_client = MagicMock()
        conversation.router_enabled = True
        conversation.router_model = "tiny"
        conversation._route_decision = AsyncMock(return_value="openclaw")
        conversation._run_openclaw = AsyncMock()
        await conversation.process_text("hey homie do the thing")
        await conversation.on_silence()
        conversation._route_decision.assert_not_awaited()
        conversation._run_openclaw.assert_not_awaited()
        args = conversation.on_response.call_args
        assert args[0][0] == "cloud says hi"

    @pytest.mark.asyncio
    async def test_cloud_failure_falls_back_to_local_for_the_turn(self, conversation):
        cloud = self._enable_cloud(conversation, chat=AsyncMock(side_effect=RuntimeError("503")))
        with patch.object(conversation._http, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = self._ollama_resp("local fallback answer")
            await conversation.process_text("hey homie hello")
            await conversation.on_silence()
        args = conversation.on_response.call_args
        assert args[0][0] == "local fallback answer"
        assert conversation.cloud_llm is cloud           # switch NOT flipped
        assert conversation._force_local_turn is False   # flag cleared
        # the failed cloud attempt must not double-append the user message
        user_msgs = [m for m in conversation.history if m["role"] == "user"]
        assert len(user_msgs) == 1

    @pytest.mark.asyncio
    async def test_cloud_tool_loop_round_trip(self, conversation):
        cloud = self._enable_cloud(conversation, chat=AsyncMock(side_effect=[
            {"message": {"content": "", "_openai_tool_call_ids": ["id1"],
                         "tool_calls": [{"function": {"name": "test_tool", "arguments": {"x": 1}}}]}},
            {"message": {"content": "tool done, Master"}},
        ]))
        conversation.mcp.call_tool = AsyncMock(return_value="ok")
        await conversation.process_text("hey homie use the tool")
        await conversation.on_silence()
        assert cloud.chat.await_count == 2
        conversation.mcp.call_tool.assert_awaited_once_with("test_tool", {"x": 1})
        # round 2 received the tool result paired after the stashed-id message
        round2_messages = cloud.chat.await_args_list[1].args[1]
        assert round2_messages[-1]["role"] == "tool"
        args = conversation.on_response.call_args
        assert args[0][0] == "tool done, Master"


class TestSystemPrompt:
    def test_returns_system_prompt(self, conversation):
        prompt = conversation._build_system_prompt()
        assert "PAL" in prompt

    def test_custom_prompt(self, mock_mcp_client, mock_tts):
        cm = ConversationManager(
            wake_word="hey homie",
            ollama_host="http://x",
            ollama_model="m",
            mcp_client=mock_mcp_client,
            tts_engine=mock_tts,
            system_prompt="You are Jarvis.",
        )
        prompt = cm._build_system_prompt()
        assert prompt.startswith("You are Jarvis.")
        assert "Current date and time:" in prompt

    def test_default_prompt_when_empty(self, mock_mcp_client, mock_tts):
        cm = ConversationManager(
            wake_word="hey homie",
            ollama_host="http://x",
            ollama_model="m",
            mcp_client=mock_mcp_client,
            tts_engine=mock_tts,
            system_prompt="",
        )
        prompt = cm._build_system_prompt()
        assert "PAL" in prompt

    def test_prompt_includes_context_summary(self, conversation):
        conversation._context_summary = "User prefers metric units; lives in Berlin."
        prompt = conversation._build_system_prompt()
        assert "summarized" in prompt
        assert "User prefers metric units; lives in Berlin." in prompt


class TestHistoryCompaction:
    def _mem(self):
        mem = MagicMock()
        mem.available = True
        mem.remember = AsyncMock(return_value=True)
        return mem

    def _fill(self, conversation, n):
        # n full messages alternating user/assistant
        for i in range(n):
            role = "user" if i % 2 == 0 else "assistant"
            conversation.history.append({"role": role, "content": f"msg {i}"})

    @pytest.mark.asyncio
    async def test_no_compaction_under_threshold(self, conversation):
        conversation.memory = self._mem()
        self._fill(conversation, conversation.max_history)  # exactly at limit, not over
        with patch.object(conversation, "_summarize_history", new_callable=AsyncMock) as s:
            await conversation._compact_history()
            s.assert_not_awaited()
        assert len(conversation.history) == conversation.max_history
        assert conversation._context_summary == ""

    @pytest.mark.asyncio
    async def test_compaction_summarizes_persists_and_trims(self, conversation):
        mem = self._mem()
        conversation.memory = mem
        self._fill(conversation, conversation.max_history + 4)
        with patch.object(conversation, "_summarize_history", new_callable=AsyncMock) as s:
            s.return_value = "A concise summary."
            await conversation._compact_history()

        # Older slice summarized, summary stored in Shodh, tail kept verbatim.
        assert conversation._context_summary == "A concise summary."
        mem.remember.assert_awaited_once()
        _, kwargs = mem.remember.call_args
        assert kwargs.get("memory_type") == "conversation_summary"
        assert len(conversation.history) == conversation.compact_keep
        # The kept tail is the most-recent messages.
        assert conversation.history[-1]["content"] == "msg " + str(conversation.max_history + 3)

    @pytest.mark.asyncio
    async def test_compaction_hard_trims_when_summary_empty(self, conversation):
        mem = self._mem()
        conversation.memory = mem
        self._fill(conversation, conversation.max_history + 4)
        with patch.object(conversation, "_summarize_history", new_callable=AsyncMock) as s:
            s.return_value = ""  # summarizer unavailable/failed
            await conversation._compact_history()
        mem.remember.assert_not_awaited()
        assert conversation._context_summary == ""
        # Falls back to a hard trim so the prompt can't grow unbounded.
        assert len(conversation.history) == conversation.max_history

    @pytest.mark.asyncio
    async def test_compaction_survives_no_memory(self, conversation):
        conversation.memory = None
        self._fill(conversation, conversation.max_history + 4)
        with patch.object(conversation, "_summarize_history", new_callable=AsyncMock) as s:
            s.return_value = "Summary without Shodh."
            await conversation._compact_history()
        assert conversation._context_summary == "Summary without Shodh."
        assert len(conversation.history) == conversation.compact_keep

    @pytest.mark.asyncio
    async def test_summarize_history_is_tool_free(self, conversation):
        with patch.object(conversation, "_chat_completion", new_callable=AsyncMock) as chat:
            chat.return_value = {"message": {"content": "  note.  "}}
            out = await conversation._summarize_history(
                [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
            )
        assert out == "note."
        _, kwargs = chat.call_args
        assert kwargs.get("with_tools") is False

    @pytest.mark.asyncio
    async def test_summarize_history_empty_input(self, conversation):
        out = await conversation._summarize_history([{"role": "user", "content": "   "}])
        assert out == ""
