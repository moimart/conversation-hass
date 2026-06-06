"""Intent hints + guard: the AI-in-the-loop replacement for deterministic
intercepts.

Tool-shaped commands ("show the conversation log", "set a timer for 5
minutes") no longer bypass the LLM. Instead the matched intent (a) appends a
steering note to the ENGINE input — history keeps the user's clean words —
and (b) arms a post-turn guard: if the engine finished without invoking the
expected tool (the probe watches LocalToolsClient's dispatch log), the tool
is executed directly and its result replaces the spoken reply."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.app.conversation import (
    ConversationManager,
    _match_intent_hint,
    _with_hint,
)


# --- matching ------------------------------------------------------------------

@pytest.mark.parametrize("text,tool,guard_args", [
    ("set a timer for 5 minutes", "start_timer", {"duration_s": 300}),
    ("start a 10 minute timer", "start_timer", {"duration_s": 600}),
    ("timer for 1 hour 10 minutes", "start_timer", {"duration_s": 4200}),
    ("set a timer for half an hour", "start_timer", {"duration_s": 1800}),
    ("cancel the timer", "cancel_timer", {"name": ""}),
    ("cancel timer 2", "cancel_timer", {"name": "2"}),
    ("stop timer number 3", "cancel_timer", {"name": "3"}),
    ("show me the conversation log", "show_conversation_log", {}),
    ("close the chat history", "hide_conversation_log", {}),
])
def test_intent_matches_with_guard(text, tool, guard_args):
    hint = _match_intent_hint(text)
    assert hint is not None
    assert hint.tool == tool
    assert hint.guard_args == guard_args
    assert hint.sentence    # steering note present


def test_cancel_beats_start():
    """"stop the 10 minute timer" must steer to cancel, never create."""
    hint = _match_intent_hint("stop the 10 minute timer")
    assert hint is not None and hint.tool == "cancel_timer"


def test_timer_without_duration_hints_but_does_not_guard():
    hint = _match_intent_hint("set a timer")
    assert hint is not None
    assert hint.tool == "start_timer"
    assert hint.guard_args is None     # nothing to auto-execute — model asks


@pytest.mark.parametrize("text", [
    "what did I ask you yesterday?",
    "show me the weather",
    "stop the music",
    "how long until sunset",
])
def test_normal_turns_get_no_hint(text):
    assert _match_intent_hint(text) is None


def test_with_hint_appends_note_only_when_hinted():
    hint = _match_intent_hint("set a timer for 5 minutes")
    out = _with_hint("set a timer for 5 minutes", hint)
    assert out.startswith("set a timer for 5 minutes")
    assert "[system note for the assistant:" in out
    assert "start_timer" in out
    assert _with_hint("hello", None) == "hello"


# --- on_silence integration ------------------------------------------------------

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
    cm._run_llm_with_tools = AsyncMock(return_value="On it.")
    cm._run_openclaw = AsyncMock()
    return cm


@pytest.mark.asyncio
async def test_engine_receives_hint_and_history_stays_clean(cm):
    cm.tool_call_probe = lambda tool, since: True   # engine complied
    cm._wake_detected = True
    cm._command_buffer = ["set a timer for 5 minutes"]
    await cm.on_silence()
    # The engine ran WITH the intent threaded through.
    args, kwargs = cm._run_llm_with_tools.await_args
    assert args[0] == "set a timer for 5 minutes"
    assert kwargs["intent"] is not None and kwargs["intent"].tool == "start_timer"
    # Engine complied -> guard did NOT fire, engine reply is spoken.
    cm.mcp.call_tool.assert_not_awaited()
    assert cm.on_response.await_args.args[0] == "On it."


@pytest.mark.asyncio
async def test_guard_fires_when_engine_skips_the_tool(cm):
    cm.tool_call_probe = lambda tool, since: False  # engine answered in prose
    cm._run_llm_with_tools = AsyncMock(
        return_value="A timer is a device that counts down...")
    cm._wake_detected = True
    cm._command_buffer = ["set a timer for 5 minutes"]
    await cm.on_silence()
    # Guard executed the tool with the parsed args...
    cm.mcp.call_tool.assert_awaited_once_with("start_timer", {"duration_s": 300})
    # ...and the TOOL result is what gets spoken, not the prose.
    assert cm.on_response.await_args.args[0] == "Timer 1 set for 5 minutes."
    # History's last assistant entry was corrected to match what was spoken.
    assert cm.history[-1] == {"role": "assistant",
                              "content": "Timer 1 set for 5 minutes."}


@pytest.mark.asyncio
async def test_guard_skipped_when_no_guard_args(cm):
    """"set a timer" (no duration) steers the model but can't auto-execute."""
    cm.tool_call_probe = lambda tool, since: False
    cm._run_llm_with_tools = AsyncMock(return_value="For how long?")
    cm._wake_detected = True
    cm._command_buffer = ["set a timer"]
    await cm.on_silence()
    cm.mcp.call_tool.assert_not_awaited()
    assert cm.on_response.await_args.args[0] == "For how long?"


@pytest.mark.asyncio
async def test_no_probe_means_guard_fires_conservatively(cm):
    """Without a probe the guard cannot confirm compliance — it executes
    (duplicate tool calls are preferable to silent no-ops)."""
    cm.tool_call_probe = None
    cm._wake_detected = True
    cm._command_buffer = ["show me the conversation log"]
    cm.mcp.call_tool = AsyncMock(return_value="Conversation log shown on the kiosk")
    await cm.on_silence()
    cm.mcp.call_tool.assert_awaited_once_with("show_conversation_log", {})


@pytest.mark.asyncio
async def test_unhinted_turn_unchanged(cm):
    cm._wake_detected = True
    cm._command_buffer = ["what's the temperature in the bedroom?"]
    await cm.on_silence()
    args, kwargs = cm._run_llm_with_tools.await_args
    assert kwargs["intent"] is None
    cm.mcp.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_openclaw_path_gets_hint_too(cm):
    from types import SimpleNamespace
    cm.openclaw_client = MagicMock()
    cm.tool_call_probe = lambda tool, since: True
    cm._run_openclaw = AsyncMock(
        return_value=SimpleNamespace(text="Log's on screen.", media_urls=[]))
    cm._wake_detected = True
    cm._command_buffer = ["show me the conversation log"]
    await cm.on_silence()
    args, kwargs = cm._run_openclaw.await_args
    assert kwargs["intent"] is not None
    assert kwargs["intent"].tool == "show_conversation_log"
    assert cm.on_response.await_args.args[0] == "Log's on screen."
