"""Regression: "show me the conversation log" must ALWAYS open the view.

Routed turns go to OpenClaw, whose agent model sometimes answers display
commands in prose (reciting the whole history out loud) instead of calling
the tool. Deterministic display intents are intercepted in on_silence BEFORE
any engine — cloud, router, OpenClaw, or the local LLM."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.app.conversation import ConversationManager, _match_display_intercept


# --- pattern matching ----------------------------------------------------------

@pytest.mark.parametrize("text,tool", [
    ("show me the conversation log", "show_conversation_log"),
    ("show me the conversation log, please.", "show_conversation_log"),
    ("Open the chat history", "show_conversation_log"),
    ("could you bring up the conversation history?", "show_conversation_log"),
    ("I want to see the chat log", "show_conversation_log"),
    ("hide the conversation log", "hide_conversation_log"),
    ("close the chat history please", "hide_conversation_log"),
    ("dismiss the conversation log", "hide_conversation_log"),
])
def test_intercept_matches(text, tool):
    m = _match_display_intercept(text)
    assert m is not None and m[0] == tool


@pytest.mark.parametrize("text", [
    "what did I ask you yesterday?",
    "show me the weather",
    "turn off the conversation mode",
    "is there anything interesting in our conversation history?",  # no verb
    "log a note that dinner is at eight",
])
def test_intercept_ignores_normal_turns(text):
    assert _match_display_intercept(text) is None


# --- on_silence integration ----------------------------------------------------

@pytest.fixture
def cm(mock_mcp_client, mock_tts):
    cm = ConversationManager(
        wake_word="hey homie",
        ollama_host="http://fake-ollama:11434",
        ollama_model="test-model",
        mcp_client=mock_mcp_client,
        tts_engine=mock_tts,
    )
    cm.on_response = AsyncMock()
    cm.mcp = MagicMock(call_tool=AsyncMock(return_value="Conversation log shown"))
    cm._run_llm_with_tools = AsyncMock(return_value="SHOULD NOT RUN")
    cm._run_openclaw = AsyncMock()
    return cm


@pytest.mark.asyncio
async def test_show_log_bypasses_every_engine(cm):
    cm._wake_detected = True   # in_conversation is a derived property
    cm._command_buffer = ["show me the conversation log, please."]
    await cm.on_silence()

    cm.mcp.call_tool.assert_awaited_once_with("show_conversation_log", {})
    cm._run_llm_with_tools.assert_not_awaited()
    cm._run_openclaw.assert_not_awaited()
    # The spoken reply is the canned confirmation, not LLM output.
    text = cm.on_response.await_args.args[0]
    assert text == "Here's the conversation log."
    # History keeps the exchange coherent for later turns.
    assert cm.history[-2:] == [
        {"role": "user", "content": "show me the conversation log, please."},
        {"role": "assistant", "content": "Here's the conversation log."},
    ]


@pytest.mark.asyncio
async def test_intercept_beats_openclaw_routing(cm):
    """Even with OpenClaw live (the flaky path), the intercept wins."""
    cm.openclaw_client = MagicMock()
    cm._wake_detected = True   # in_conversation is a derived property
    cm._command_buffer = ["hide the conversation log"]
    await cm.on_silence()
    cm.mcp.call_tool.assert_awaited_once_with("hide_conversation_log", {})
    cm._run_openclaw.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_turn_still_reaches_llm(cm):
    cm._wake_detected = True   # in_conversation is a derived property
    cm._command_buffer = ["what's the temperature in the bedroom?"]
    await cm.on_silence()
    cm.mcp.call_tool.assert_not_awaited()
    cm._run_llm_with_tools.assert_awaited_once()
