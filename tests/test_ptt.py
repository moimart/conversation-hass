"""Tests for the Push-to-Talk session lifecycle (server/app/ptt.py)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.app import ptt


# -----------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------

def _state(mic_muted: bool = False, ai_speaking: bool = False):
    """Build a minimal AppState-ish object the PTT module accepts.

    We only need the attributes ptt.py actually touches:
      * conversation (with _wake_detected / _awaiting_followup /
        _command_buffer / ptt_active / _set_state)
      * pipeline (with _ai_speaking / set_ai_speaking / force_finalize)
      * audio_websocket (with send_json)
      * mic_muted
      * ui_clients (broadcast_to_ui iterates this; we keep it empty)
      * ptt (initially None)
    """
    conv = SimpleNamespace()
    conv._wake_detected = False
    conv._awaiting_followup = False
    conv._command_buffer = []
    conv.ptt_active = False
    conv._set_state = AsyncMock()
    conv.process_text = AsyncMock()
    conv.on_silence = AsyncMock()

    pipeline = SimpleNamespace()
    pipeline._ai_speaking = ai_speaking
    pipeline.set_ai_speaking = MagicMock()
    pipeline.force_finalize = AsyncMock(return_value=None)

    ws = AsyncMock()
    ws.send_json = AsyncMock()

    state = SimpleNamespace()
    state.conversation = conv
    state.pipeline = pipeline
    state.audio_websocket = ws
    state.mic_muted = mic_muted
    state.ui_clients = set()
    state.ptt = None
    return state


def _ws_sent_types(state):
    """All `type` values that have been sent to the RPi via send_json."""
    return [call.args[0].get("type") for call in state.audio_websocket.send_json.await_args_list]


# -----------------------------------------------------------------
# start_ptt
# -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_sets_wake_and_state_listening():
    state = _state()
    result = await ptt.start_ptt(state)
    assert result == {"status": "ok", "session": True}
    assert state.ptt is not None
    assert state.conversation._wake_detected is True
    assert state.conversation.ptt_active is True
    assert state.conversation._awaiting_followup is False
    state.conversation._set_state.assert_awaited_with("listening")
    # Tell the kiosk PTT is live
    assert "ptt_active" in _ws_sent_types(state)


@pytest.mark.asyncio
async def test_start_idempotent_does_not_open_second_session():
    state = _state()
    first = await ptt.start_ptt(state)
    assert first["status"] == "ok"
    session_obj = state.ptt
    second = await ptt.start_ptt(state)
    assert second == {"status": "already_active", "session": True}
    # Same session, untouched
    assert state.ptt is session_obj


@pytest.mark.asyncio
async def test_start_auto_unmutes_when_muted():
    state = _state(mic_muted=True)
    await ptt.start_ptt(state)
    sent = state.audio_websocket.send_json.await_args_list
    # mute_set false should have been sent
    assert any(c.args[0] == {"type": "mute_set", "muted": False} for c in sent)
    assert state.ptt.prev_muted is True


@pytest.mark.asyncio
async def test_start_does_not_unmute_when_not_muted():
    state = _state(mic_muted=False)
    await ptt.start_ptt(state)
    sent = [c.args[0] for c in state.audio_websocket.send_json.await_args_list]
    assert not any(m.get("type") == "mute_set" for m in sent)
    assert state.ptt.prev_muted is False


@pytest.mark.asyncio
async def test_start_cancels_tts_when_speaking():
    state = _state(ai_speaking=True)
    await ptt.start_ptt(state)
    sent_types = _ws_sent_types(state)
    assert "tts_cancel" in sent_types
    state.pipeline.set_ai_speaking.assert_called_with(False)
    assert state.ptt.was_tts_active is True


@pytest.mark.asyncio
async def test_start_returns_not_ready_when_pipeline_missing():
    state = _state()
    state.pipeline = None
    result = await ptt.start_ptt(state)
    assert result["status"] == "not_ready"
    assert state.ptt is None


# -----------------------------------------------------------------
# end_ptt
# -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_end_without_start_is_noop():
    state = _state()
    result = await ptt.end_ptt(state)
    assert result == {"status": "not_active", "session": False}
    state.conversation.on_silence.assert_not_awaited()


@pytest.mark.asyncio
async def test_end_runs_on_silence_after_normal_release():
    state = _state()
    await ptt.start_ptt(state)
    # Held for longer than the debounce window
    await asyncio.sleep(ptt.DEBOUNCE_S + 0.02)
    result = await ptt.end_ptt(state)
    assert result == {"status": "ok", "session": False}
    # Let the scheduled on_silence task run
    await asyncio.sleep(0)
    state.conversation.on_silence.assert_awaited()
    assert state.ptt is None


@pytest.mark.asyncio
async def test_end_debounces_when_held_under_threshold():
    state = _state()
    await ptt.start_ptt(state)
    # Immediate release — well under DEBOUNCE_S
    result = await ptt.end_ptt(state)
    assert result["status"] == "cancelled"
    state.conversation.on_silence.assert_not_awaited()
    state.pipeline.force_finalize.assert_awaited_with(discard=True)


@pytest.mark.asyncio
async def test_end_cancel_drops_audio_and_skips_llm():
    state = _state()
    await ptt.start_ptt(state)
    await asyncio.sleep(ptt.DEBOUNCE_S + 0.02)
    result = await ptt.end_ptt(state, cancel=True)
    assert result["status"] == "cancelled"
    state.pipeline.force_finalize.assert_awaited_with(discard=True)
    state.conversation.on_silence.assert_not_awaited()


@pytest.mark.asyncio
async def test_end_restores_mute_when_previously_muted():
    state = _state(mic_muted=True)
    await ptt.start_ptt(state)
    await asyncio.sleep(ptt.DEBOUNCE_S + 0.02)
    await ptt.end_ptt(state)
    sent = [c.args[0] for c in state.audio_websocket.send_json.await_args_list]
    # mute_set true should appear AFTER mute_set false
    mute_msgs = [m for m in sent if m.get("type") == "mute_set"]
    assert mute_msgs[0] == {"type": "mute_set", "muted": False}
    assert mute_msgs[-1] == {"type": "mute_set", "muted": True}


@pytest.mark.asyncio
async def test_end_feeds_transcription_into_conversation():
    state = _state()
    state.pipeline.force_finalize = AsyncMock(return_value={
        "text": "turn on the lights",
        "is_partial": False,
        "speaker": "human",
        "silence_after": True,
    })
    await ptt.start_ptt(state)
    await asyncio.sleep(ptt.DEBOUNCE_S + 0.02)
    await ptt.end_ptt(state)
    state.conversation.process_text.assert_awaited_with("turn on the lights")


@pytest.mark.asyncio
async def test_end_skips_ai_speaker_transcription():
    state = _state()
    state.pipeline.force_finalize = AsyncMock(return_value={
        "text": "leftover ai echo",
        "is_partial": False,
        "speaker": "ai",
        "silence_after": True,
    })
    await ptt.start_ptt(state)
    await asyncio.sleep(ptt.DEBOUNCE_S + 0.02)
    await ptt.end_ptt(state)
    state.conversation.process_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_safety_timeout_fires_and_closes_session(monkeypatch):
    """Trigger the safety timeout immediately by patching the constant."""
    monkeypatch.setattr(ptt, "SAFETY_TIMEOUT_S", 0.05)
    state = _state()
    await ptt.start_ptt(state)
    # Wait long enough for the timeout task to fire
    await asyncio.sleep(0.12)
    assert state.ptt is None
    state.conversation.on_silence.assert_awaited()
