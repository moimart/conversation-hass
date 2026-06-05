"""Regression: the HA/MQTT `speak` entity must also reach satellites.

`_mqtt_speak` is a separate code path from /api/speak and speak_verbatim; it
used to push the verbatim audio only to the kiosk (RPi), so a force-speak
triggered from Home Assistant never played on paired phones. This wires the real
callback via mqtt_callbacks.wire() and asserts the connected satellite receives
the response text + the tts_play cue (with the audio cached for fetch)."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


class _WS:
    """Hashable fake websocket usable as a dict key and as the kiosk socket."""
    def __init__(self):
        self.send_text = AsyncMock()
        self.send_json = AsyncMock()
        self.send_bytes = AsyncMock()


def _bridge():
    b = MagicMock()
    b._cached_config = {}
    b._cached_display_state = None
    b.num_ctx_max = 0
    b.set_config_callback = MagicMock()
    b.publish_config = AsyncMock()
    b.publish_last_response = AsyncMock()
    return b


def _state(sat_ws):
    conv = SimpleNamespace(
        wake_word="steve", ollama_model="m", fallback_ollama_model="f",
        num_ctx=32768, state="idle",
    )
    return SimpleNamespace(
        ui_clients=set(),
        satellite_ws={"tS": sat_ws},
        satellite_ws_tokens={sat_ws: "tS"},
        satellite_tts={},
        satellite_photo_sessions={},
        audio_websocket=_WS(),
        tts_engine=SimpleNamespace(voice="v", synthesize=AsyncMock(return_value=b"RIFF....WAVE")),
        pipeline=SimpleNamespace(set_ai_speaking=MagicMock()),
        conversation=conv,
        themes=None,                 # not a ThemeRegistry → listener registration skipped
        runtime_config={},
        auto_theme=True, theme_day="d", theme_night="n",
        openclaw_enabled=False, openclaw_gateway_url="", openclaw_workspace="ws",
        display_orientation="portrait", orb_side="left",
        router_enabled=True, router_model="r",
        voice_options=[], model_options=[],
        cloud_model_options=[], cloud_llm_model="", cloud_llm_enabled=False,
        cloud_llm_client=None,
        conversation_log=None, conversation_event_logger=None,
    )


@pytest.mark.asyncio
async def test_mqtt_speak_reaches_satellite(monkeypatch):
    import server.app.main as srv_main
    from server.app.mqtt_callbacks import wire

    monkeypatch.setattr(srv_main, "_fetch_ollama_context_length", AsyncMock(return_value=131072))

    sat = _WS()
    state = _state(sat)
    bridge = _bridge()
    state.mqtt_bridge = bridge

    await wire(state, bridge)
    assert callable(bridge.on_speak)

    await bridge.on_speak("Dinner is ready")

    # The kiosk still got its audio (unchanged behavior).
    state.audio_websocket.send_bytes.assert_awaited()
    # The satellite got the response text AND the tts_play cue, with audio cached.
    sent = [json.loads(c.args[0]) for c in sat.send_text.await_args_list]
    types = [m["type"] for m in sent]
    assert "response" in types and "tts_play" in types
    assert state.satellite_tts["tS"]["audio"] == b"RIFF....WAVE"
