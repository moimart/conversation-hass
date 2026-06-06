"""Timer voice intercepts: "set a timer for 5 minutes" must ALWAYS create a
timer — same determinism rationale as the display intercepts (agentic models
sometimes answer in prose instead of calling the tool). The intercept passes
parsed args to the tool and speaks the TOOL's return value (spoken=None), so
the confirmation includes the assigned name ("Timer 1 set for 5 minutes.")."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.app.conversation import ConversationManager, _match_intercept


# --- matching ------------------------------------------------------------------

@pytest.mark.parametrize("text,secs", [
    ("set a timer for 5 minutes", 300),
    ("start a 10 minute timer", 600),
    ("create a timer for 90 seconds", 90),
    ("timer for 1 hour 10 minutes", 4200),
    ("set a timer for half an hour", 1800),
    ("10 minute timer please", 600),
])
def test_start_intercepts(text, secs):
    m = _match_intercept(text)
    assert m is not None
    tool, args, spoken = m
    assert tool == "start_timer"
    assert args == {"duration_s": secs}
    assert spoken is None      # speak the tool's return value (it has the name)


@pytest.mark.parametrize("text,name", [
    ("cancel the timer", ""),
    ("cancel all timers", ""),
    ("cancel timer 2", "2"),
    ("stop timer number 3", "3"),
    ("delete the timers", ""),
])
def test_cancel_intercepts(text, name):
    m = _match_intercept(text)
    assert m is not None
    tool, args, spoken = m
    assert tool == "cancel_timer"
    assert args == {"name": name}
    assert spoken is None


def test_cancel_beats_start():
    """"stop the 10 minute timer" must cancel, never create."""
    m = _match_intercept("stop the 10 minute timer")
    assert m is not None and m[0] == "cancel_timer"


@pytest.mark.parametrize("text", [
    "set a timer",                      # no duration -> LLM asks
    "what's a good timer app?",
    "how long until sunset",
    "stop the music",
])
def test_non_timer_or_ambiguous_falls_through(text):
    assert _match_intercept(text) is None


def test_display_intercepts_still_match_through_combined():
    m = _match_intercept("show me the conversation log")
    assert m is not None
    tool, args, spoken = m
    assert tool == "show_conversation_log"
    assert args == {} and isinstance(spoken, str)


# --- on_silence integration -----------------------------------------------------

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
    cm.mcp = MagicMock(call_tool=AsyncMock(return_value="Timer 1 set for 5 minutes."))
    cm._run_llm_with_tools = AsyncMock(return_value="SHOULD NOT RUN")
    cm._run_openclaw = AsyncMock()
    return cm


@pytest.mark.asyncio
async def test_timer_turn_bypasses_llm_and_speaks_tool_result(cm):
    cm._wake_detected = True
    cm._command_buffer = ["set a timer for 5 minutes"]
    await cm.on_silence()
    cm.mcp.call_tool.assert_awaited_once_with("start_timer", {"duration_s": 300})
    cm._run_llm_with_tools.assert_not_awaited()
    # The spoken response is the TOOL's return value (carries the name).
    assert cm.on_response.await_args.args[0] == "Timer 1 set for 5 minutes."
    assert cm.history[-1] == {"role": "assistant",
                              "content": "Timer 1 set for 5 minutes."}


@pytest.mark.asyncio
async def test_static_display_intercept_keeps_canned_text(cm):
    cm.mcp.call_tool = AsyncMock(return_value="Conversation log shown on the kiosk")
    cm._wake_detected = True
    cm._command_buffer = ["show me the conversation log"]
    await cm.on_silence()
    # Static intercepts keep their canned confirmation, not the tool result.
    assert cm.on_response.await_args.args[0] == "Here's the conversation log."
