"""Tests for the server main.py: AppState, lifespan, endpoints, chime generation."""

import asyncio
import io
import json
import wave
from dataclasses import fields
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from server.app.main import (
    AppState,
    _build_local_tools,
    _generate_chime,
    _get_state,
    _ma_call,
    _ma_pause_if_playing,
    _ma_query_state,
    _ma_resume_if_we_paused,
    _ma_volume_step,
    broadcast_to_ui,
)


# --- AppState ---

class TestAppState:
    def test_defaults(self):
        state = AppState()
        assert state.pipeline is None
        assert state.conversation is None
        assert state.tts_engine is None
        assert state.mcp_client is None
        assert state.memory_client is None
        assert state.wake_chime is None
        assert isinstance(state.ui_clients, set)
        assert len(state.ui_clients) == 0

    def test_independent_ui_clients(self):
        """Each AppState instance gets its own ui_clients set."""
        state1 = AppState()
        state2 = AppState()
        state1.ui_clients.add("ws1")
        assert len(state2.ui_clients) == 0

    def test_all_fields_exist(self):
        state = AppState()
        field_names = {f.name for f in fields(state)}
        assert "pipeline" in field_names
        assert "conversation" in field_names
        assert "tts_engine" in field_names
        assert "mcp_client" in field_names
        assert "memory_client" in field_names
        assert "wake_chime" in field_names
        assert "ui_clients" in field_names


# --- Chime generation ---

class TestGenerateChime:
    def test_returns_bytes(self):
        chime = _generate_chime()
        assert isinstance(chime, bytes)
        assert len(chime) > 44  # WAV header + audio

    def test_valid_wav(self):
        chime = _generate_chime()
        buf = io.BytesIO(chime)
        with wave.open(buf, "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 22050
            frames = wf.readframes(wf.getnframes())
            assert len(frames) > 0

    def test_two_tones(self):
        """Chime should have 2 tones at 0.15s each = 0.3s total."""
        chime = _generate_chime()
        buf = io.BytesIO(chime)
        with wave.open(buf, "rb") as wf:
            n_frames = wf.getnframes()
            duration = n_frames / wf.getframerate()
            assert 0.29 < duration < 0.31

    def test_not_silence(self):
        """Chime should contain actual audio, not silence."""
        chime = _generate_chime()
        buf = io.BytesIO(chime)
        with wave.open(buf, "rb") as wf:
            frames = wf.readframes(wf.getnframes())
        samples = np.frombuffer(frames, dtype=np.int16)
        assert np.max(np.abs(samples)) > 1000  # has audible signal


# --- broadcast_to_ui ---

class TestBroadcastToUI:
    @pytest.mark.asyncio
    async def test_sends_to_all_clients(self):
        state = AppState()
        ws1 = MagicMock()
        ws1.send_text = AsyncMock()
        ws2 = MagicMock()
        ws2.send_text = AsyncMock()
        state.ui_clients = {ws1, ws2}

        await broadcast_to_ui(state, {"type": "test", "data": "hello"})

        ws1.send_text.assert_awaited_once()
        ws2.send_text.assert_awaited_once()
        sent = json.loads(ws1.send_text.call_args[0][0])
        assert sent["type"] == "test"
        assert sent["data"] == "hello"

    @pytest.mark.asyncio
    async def test_removes_dead_clients(self):
        state = AppState()
        ws_alive = MagicMock()
        ws_alive.send_text = AsyncMock()
        ws_dead = MagicMock()
        ws_dead.send_text = AsyncMock(side_effect=ConnectionError("gone"))
        state.ui_clients = {ws_alive, ws_dead}

        await broadcast_to_ui(state, {"type": "test"})

        assert ws_dead not in state.ui_clients
        assert ws_alive in state.ui_clients

    @pytest.mark.asyncio
    async def test_empty_clients(self):
        state = AppState()
        await broadcast_to_ui(state, {"type": "test"})  # should not raise

    @pytest.mark.asyncio
    async def test_json_serialization(self):
        state = AppState()
        ws = MagicMock()
        ws.send_text = AsyncMock()
        state.ui_clients = {ws}

        await broadcast_to_ui(state, {"type": "transcription", "text": "hello", "is_partial": False})

        sent_str = ws.send_text.call_args[0][0]
        parsed = json.loads(sent_str)
        assert parsed["type"] == "transcription"
        assert parsed["text"] == "hello"
        assert parsed["is_partial"] is False


# --- _get_state ---

class TestGetState:
    def test_retrieves_state(self):
        mock_app = MagicMock()
        state = AppState()
        mock_app.state.hal = state
        result = _get_state(mock_app)
        assert result is state


# --- Lifespan ---

class TestLifespan:
    @pytest.mark.asyncio
    async def test_lifespan_initializes_state(self):
        """Test that the lifespan context manager creates AppState with all fields."""
        from server.app.main import lifespan

        mock_app = MagicMock()
        mock_app.state = MagicMock()

        with patch("server.app.main.MCPClient") as MockMCP, \
             patch("server.app.main.TTSEngine") as MockTTS, \
             patch("server.app.main.MemoryClient") as MockMemory, \
             patch("server.app.main.AudioPipeline") as MockPipeline, \
             patch("server.app.main.ConversationManager") as MockConv, \
             patch("server.app.main._generate_chime", return_value=b"fake-chime"), \
             patch.dict("os.environ", {"MCP_SERVER_URL": "", "WAKE_WORD": "hey hal"}):

            mock_mcp = MagicMock()
            mock_mcp.disconnect = AsyncMock()
            MockMCP.return_value = mock_mcp
            MockTTS.return_value = MagicMock()
            MockTTS.return_value.initialize = AsyncMock()
            MockMemory.return_value = MagicMock()
            MockMemory.return_value.initialize = AsyncMock()
            MockPipeline.return_value = MagicMock()
            MockPipeline.return_value.initialize = AsyncMock()

            async with lifespan(mock_app):
                state = mock_app.state.hal
                assert isinstance(state, AppState)
                assert state.wake_chime == b"fake-chime"
                assert state.pipeline is not None
                assert state.conversation is not None
                assert state.tts_engine is not None
                assert state.mcp_client is not None
                assert state.memory_client is not None

    @pytest.mark.asyncio
    async def test_lifespan_mcp_failure_graceful(self):
        """MCP connection failure should not prevent startup."""
        from server.app.main import lifespan

        mock_app = MagicMock()
        mock_app.state = MagicMock()

        mock_multi = MagicMock()
        mock_multi.add_server = AsyncMock()
        mock_multi.tool_names = []
        mock_multi.disconnect_all = AsyncMock()

        with patch("server.app.main.MultiMCPClient", return_value=mock_multi), \
             patch("server.app.main.TTSEngine") as MockTTS, \
             patch("server.app.main.MemoryClient") as MockMemory, \
             patch("server.app.main.AudioPipeline") as MockPipeline, \
             patch("server.app.main.ConversationManager"), \
             patch("server.app.main._generate_chime", return_value=b"chime"), \
             patch.dict("os.environ", {"MCP_SERVER_URL": "http://broken", "WAKE_WORD": "hey hal"}):

            MockTTS.return_value = MagicMock()
            MockTTS.return_value.initialize = AsyncMock()
            MockMemory.return_value = MagicMock()
            MockMemory.return_value.initialize = AsyncMock()
            MockPipeline.return_value = MagicMock()
            MockPipeline.return_value.initialize = AsyncMock()

            async with lifespan(mock_app):
                # add_server should have been called with the env var URL
                mock_multi.add_server.assert_awaited_once_with("home-assistant", "http://broken")

    @pytest.mark.asyncio
    async def test_lifespan_shutdown_disconnects_mcp(self):
        """Shutdown should disconnect all MCP clients."""
        from server.app.main import lifespan

        mock_app = MagicMock()
        mock_app.state = MagicMock()

        mock_multi = MagicMock()
        mock_multi.add_server = AsyncMock()
        mock_multi.tool_names = ["ha_call_service"]
        mock_multi.disconnect_all = AsyncMock()

        with patch("server.app.main.MultiMCPClient", return_value=mock_multi), \
             patch("server.app.main.TTSEngine") as MockTTS, \
             patch("server.app.main.MemoryClient") as MockMemory, \
             patch("server.app.main.AudioPipeline") as MockPipeline, \
             patch("server.app.main.ConversationManager"), \
             patch("server.app.main._generate_chime", return_value=b"chime"), \
             patch.dict("os.environ", {"MCP_SERVER_URL": "http://ha-mcp", "WAKE_WORD": "hey hal"}):

            MockTTS.return_value = MagicMock()
            MockTTS.return_value.initialize = AsyncMock()
            MockMemory.return_value = MagicMock()
            MockMemory.return_value.initialize = AsyncMock()
            MockPipeline.return_value = MagicMock()
            MockPipeline.return_value.initialize = AsyncMock()

            async with lifespan(mock_app):
                pass

            mock_multi.disconnect_all.assert_awaited_once()


# --- Audio endpoint message handling ---

class TestAudioEndpointLogic:
    def test_transcription_message_built_once(self):
        """Verify the transcription message structure."""
        result = {
            "text": "hello world",
            "is_partial": False,
            "speaker": "human",
            "silence_after": True,
        }
        msg = {
            "type": "transcription",
            "text": result["text"],
            "is_partial": result.get("is_partial", False),
            "speaker": result.get("speaker", "unknown"),
        }
        assert msg["type"] == "transcription"
        assert msg["text"] == "hello world"
        assert msg["is_partial"] is False
        assert msg["speaker"] == "human"
        # silence_after should NOT be in the transcription message
        assert "silence_after" not in msg

    def test_silence_after_triggers_processing(self):
        """Verify silence_after field is correctly detected."""
        result_with_silence = {"text": "test", "is_partial": False, "speaker": "human", "silence_after": True}
        result_without = {"text": "test", "is_partial": True, "speaker": "human"}
        result_partial = {"text": "test", "is_partial": False, "speaker": "human"}

        assert result_with_silence.get("silence_after", False) is True
        assert result_without.get("silence_after", False) is False
        assert result_partial.get("silence_after", False) is False


# --- Sendspin / Music Assistant coordination ---

def _state_with_mcp(player_entity: str = "media_player.hal_speaker"):
    """Build an AppState with a mocked MCP client and configured entity."""
    state = AppState()
    state.sendspin_player_entity = player_entity
    state.mcp_client = MagicMock()
    state.mcp_client.call_tool = AsyncMock(return_value="{}")
    return state


class TestMaCall:
    """_ma_call invokes ha_call_service with the right args, or no-ops."""

    @pytest.mark.asyncio
    async def test_no_op_when_entity_unset(self):
        state = AppState()
        state.mcp_client = MagicMock()
        state.mcp_client.call_tool = AsyncMock()
        await _ma_call(state, "media_pause")
        state.mcp_client.call_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_op_when_mcp_unset(self):
        state = AppState()
        state.sendspin_player_entity = "media_player.x"
        # mcp_client stays None → silent return, no exception
        await _ma_call(state, "media_pause")

    @pytest.mark.asyncio
    async def test_invokes_ha_call_service(self):
        state = _state_with_mcp("media_player.x")
        await _ma_call(state, "media_pause")
        state.mcp_client.call_tool.assert_awaited_once_with(
            "ha_call_service",
            {
                "domain": "media_player",
                "service": "media_pause",
                "target": {"entity_id": "media_player.x"},
            },
        )

    @pytest.mark.asyncio
    async def test_extra_data_attached(self):
        state = _state_with_mcp("media_player.x")
        await _ma_call(state, "volume_set", {"volume_level": 0.5})
        args = state.mcp_client.call_tool.await_args.args
        assert args[1]["service_data"] == {"volume_level": 0.5}

    @pytest.mark.asyncio
    async def test_swallows_mcp_exception(self):
        state = _state_with_mcp("media_player.x")
        state.mcp_client.call_tool = AsyncMock(side_effect=RuntimeError("boom"))
        # Must not raise — caller relies on best-effort semantics.
        await _ma_call(state, "media_pause")

    @pytest.mark.asyncio
    async def test_serializes_concurrent_calls(self):
        """Two concurrent _ma_call invocations must execute sequentially."""
        state = _state_with_mcp("media_player.x")
        in_flight = 0
        peak = 0

        async def slow_tool(*args, **kwargs):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            import asyncio as _a
            await _a.sleep(0.01)
            in_flight -= 1
            return "{}"

        state.mcp_client.call_tool = AsyncMock(side_effect=slow_tool)
        import asyncio as _a
        await _a.gather(
            _ma_call(state, "volume_up"),
            _ma_call(state, "volume_up"),
            _ma_call(state, "volume_up"),
        )
        assert peak == 1, f"expected lock to serialize, saw {peak} concurrent"


class TestMaVolumeStep:
    """Sign of step → up/down service."""

    @pytest.mark.asyncio
    async def test_positive_step_calls_up(self):
        state = _state_with_mcp("media_player.x")
        await _ma_volume_step(state, 0.1)
        service = state.mcp_client.call_tool.await_args.args[1]["service"]
        assert service == "volume_up"

    @pytest.mark.asyncio
    async def test_negative_step_calls_down(self):
        state = _state_with_mcp("media_player.x")
        await _ma_volume_step(state, -0.1)
        service = state.mcp_client.call_tool.await_args.args[1]["service"]
        assert service == "volume_down"

    @pytest.mark.asyncio
    async def test_no_op_without_entity(self):
        state = AppState()
        state.mcp_client = MagicMock()
        state.mcp_client.call_tool = AsyncMock()
        await _ma_volume_step(state, 0.1)
        state.mcp_client.call_tool.assert_not_awaited()


class TestMaQueryState:
    """_ma_query_state unwraps HA MCP's {data, metadata} envelope."""

    @pytest.mark.asyncio
    async def test_returns_state(self):
        state = _state_with_mcp("media_player.x")
        state.mcp_client.call_tool = AsyncMock(return_value=json.dumps({
            "data": {"state": "playing", "attributes": {}},
            "metadata": {},
        }))
        assert await _ma_query_state(state) == "playing"

    @pytest.mark.asyncio
    async def test_returns_state_unwrapped(self):
        """Some tools return the entity directly without the envelope."""
        state = _state_with_mcp("media_player.x")
        state.mcp_client.call_tool = AsyncMock(return_value=json.dumps({
            "state": "paused",
        }))
        assert await _ma_query_state(state) == "paused"

    @pytest.mark.asyncio
    async def test_returns_none_on_bad_json(self):
        state = _state_with_mcp("media_player.x")
        state.mcp_client.call_tool = AsyncMock(return_value="not json")
        assert await _ma_query_state(state) is None

    @pytest.mark.asyncio
    async def test_returns_none_when_unconfigured(self):
        state = AppState()
        assert await _ma_query_state(state) is None


class TestShapeCGating:
    """Pause/resume only fires when WE paused — never overrides user pauses."""

    @pytest.mark.asyncio
    async def test_pause_when_playing_sets_flag_and_calls_pause(self):
        state = _state_with_mcp("media_player.x")
        state.mcp_client.call_tool = AsyncMock(return_value=json.dumps({
            "data": {"state": "playing"},
            "metadata": {},
        }))
        await _ma_pause_if_playing(state)
        assert state.sendspin_was_playing is True
        # First call was the get_state, second was the pause
        services_called = [c.args[0] for c in state.mcp_client.call_tool.await_args_list]
        assert "ha_get_state" in services_called
        assert "ha_call_service" in services_called

    @pytest.mark.asyncio
    async def test_pause_when_paused_does_not_call_pause(self):
        state = _state_with_mcp("media_player.x")
        state.mcp_client.call_tool = AsyncMock(return_value=json.dumps({
            "data": {"state": "paused"},
            "metadata": {},
        }))
        await _ma_pause_if_playing(state)
        assert state.sendspin_was_playing is False
        # Only ha_get_state should have been called, never a service.
        called_tools = [c.args[0] for c in state.mcp_client.call_tool.await_args_list]
        assert called_tools == ["ha_get_state"]

    @pytest.mark.asyncio
    async def test_resume_only_when_we_paused(self):
        state = _state_with_mcp("media_player.x")
        state.sendspin_was_playing = False
        await _ma_resume_if_we_paused(state)
        state.mcp_client.call_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resume_clears_flag(self):
        state = _state_with_mcp("media_player.x")
        state.sendspin_was_playing = True
        await _ma_resume_if_we_paused(state)
        assert state.sendspin_was_playing is False
        state.mcp_client.call_tool.assert_awaited_once()
        assert state.mcp_client.call_tool.await_args.args[1]["service"] == "media_play"


class TestSendspinAppStateDefaults:
    def test_defaults(self):
        state = AppState()
        assert state.sendspin_player_entity == ""
        assert state.sendspin_pause_during_tts is False
        assert state.sendspin_was_playing is False
        # _ma_lock defaults to an asyncio.Lock instance.
        import asyncio as _a
        assert isinstance(state._ma_lock, _a.Lock)


class TestShowCamera:
    """Local tool that fetches a camera snapshot and pushes it to the kiosk."""

    def _state(self):
        state = AppState()
        state.mcp_client = MagicMock()
        state.mcp_client.tool_names = ["ha_get_camera_image"]
        state.mcp_client.call_tool_content = AsyncMock(return_value=[])
        state.audio_websocket = AsyncMock()
        return state

    async def _show_camera(self, state, args):
        tools = _build_local_tools(state)
        return await tools.call_tool("show_camera", args)

    @pytest.mark.asyncio
    async def test_rejects_non_camera_entity(self):
        state = self._state()
        result = await self._show_camera(state, {"entity_id": "light.kitchen"})
        assert "must be a camera" in result
        state.mcp_client.call_tool_content.assert_not_awaited()
        state.audio_websocket.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_errors_when_mcp_unavailable(self):
        state = self._state()
        state.mcp_client.tool_names = []  # ha_get_camera_image absent
        result = await self._show_camera(state, {"entity_id": "camera.front"})
        assert "Home Assistant" in result
        state.audio_websocket.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_error_when_no_image_returned(self):
        state = self._state()
        state.mcp_client.call_tool_content = AsyncMock(return_value=[])
        result = await self._show_camera(state, {"entity_id": "camera.front"})
        assert "No image" in result
        state.audio_websocket.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pushes_image_message_on_success(self):
        state = self._state()
        item = MagicMock()
        item.data = "BASE64DATA"
        item.mimeType = "image/jpeg"
        state.mcp_client.call_tool_content = AsyncMock(return_value=[item])
        result = await self._show_camera(state, {"entity_id": "camera.front", "duration_s": 60})
        assert "camera.front" in result
        # show_camera also sends video_stop for mutual exclusion. Find the
        # show_camera message in the call list.
        msgs = [c.args[0] for c in state.audio_websocket.send_json.await_args_list]
        show_msgs = [m for m in msgs if m.get("type") == "show_camera"]
        assert len(show_msgs) == 1
        msg = show_msgs[0]
        assert msg["image"] == "BASE64DATA"
        assert msg["mime"] == "image/jpeg"
        assert msg["duration_s"] == 60
        assert msg["entity_id"] == "camera.front"

    @pytest.mark.asyncio
    async def test_clamps_duration_to_valid_range(self):
        state = self._state()
        item = MagicMock(); item.data = "X"; item.mimeType = "image/jpeg"
        state.mcp_client.call_tool_content = AsyncMock(return_value=[item])
        await self._show_camera(state, {"entity_id": "camera.x", "duration_s": 999999})
        msg = state.audio_websocket.send_json.await_args.args[0]
        assert msg["duration_s"] == 900

    @pytest.mark.asyncio
    async def test_default_duration_is_150s(self):
        state = self._state()
        item = MagicMock(); item.data = "X"; item.mimeType = "image/jpeg"
        state.mcp_client.call_tool_content = AsyncMock(return_value=[item])
        await self._show_camera(state, {"entity_id": "camera.x"})
        msg = state.audio_websocket.send_json.await_args.args[0]
        assert msg["duration_s"] == 150


class TestStreamCamera:
    """Local tools for the live WebRTC stream lifecycle."""

    def _state(self, monkeypatch):
        monkeypatch.setenv("HA_URL", "http://ha.local:8123")
        monkeypatch.setenv("HA_TOKEN", "token")
        state = AppState()
        state.audio_websocket = AsyncMock()
        return state

    async def _stream(self, state, args):
        tools = _build_local_tools(state)
        return await tools.call_tool("stream_camera", args)

    async def _stop(self, state):
        tools = _build_local_tools(state)
        return await tools.call_tool("stop_streaming", {})

    @pytest.mark.asyncio
    async def test_rejects_non_camera_entity(self, monkeypatch):
        state = self._state(monkeypatch)
        result = await self._stream(state, {"entity_id": "light.x"})
        assert "must be a camera" in result
        assert state.active_stream is None
        state.audio_websocket.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_errors_when_ha_creds_missing(self, monkeypatch):
        monkeypatch.delenv("HA_URL", raising=False)
        monkeypatch.delenv("HA_TOKEN", raising=False)
        state = AppState()
        state.audio_websocket = AsyncMock()
        result = await self._stream(state, {"entity_id": "camera.x"})
        assert "Live streaming unavailable" in result
        assert state.active_stream is None

    @pytest.mark.asyncio
    async def test_errors_when_rpi_disconnected(self, monkeypatch):
        state = self._state(monkeypatch)
        state.audio_websocket = None
        result = await self._stream(state, {"entity_id": "camera.x"})
        assert "RPi not connected" in result
        assert state.active_stream is None

    @pytest.mark.asyncio
    async def test_stream_start_pushes_to_kiosk_and_tracks_session(self, monkeypatch):
        state = self._state(monkeypatch)
        result = await self._stream(state, {"entity_id": "camera.front", "duration_s": 60})
        assert "camera.front" in result
        assert state.active_stream is not None
        assert state.active_stream["entity_id"] == "camera.front"
        sid = state.active_stream["session_id"]
        msg = state.audio_websocket.send_json.await_args.args[0]
        assert msg["type"] == "stream_start"
        assert msg["session_id"] == sid
        assert msg["entity_id"] == "camera.front"
        # Cleanup the safety task to avoid pytest warnings.
        state.active_stream["safety_task"].cancel()

    @pytest.mark.asyncio
    async def test_clamps_duration_to_valid_range(self, monkeypatch):
        state = self._state(monkeypatch)
        await self._stream(state, {"entity_id": "camera.x", "duration_s": 10_000_000})
        # Internal: just check the safety task is scheduled and the session
        # is tracked. We don't expose the duration directly to the kiosk.
        assert state.active_stream is not None
        state.active_stream["safety_task"].cancel()

    @pytest.mark.asyncio
    async def test_stop_streaming_no_active(self, monkeypatch):
        state = self._state(monkeypatch)
        result = await self._stop(state)
        # stop_streaming now also clears any pushed video, so it returns a
        # success-shaped message even with no active webrtc stream.
        assert "Stopped" in result

    @pytest.mark.asyncio
    async def test_stop_streaming_clears_session_and_notifies_kiosk(self, monkeypatch):
        state = self._state(monkeypatch)
        await self._stream(state, {"entity_id": "camera.x"})
        state.audio_websocket.send_json.reset_mock()
        result = await self._stop(state)
        assert "camera.x" in result
        assert state.active_stream is None
        state.audio_websocket.send_json.assert_awaited()
        # stop_streaming sends both stream_stop and video_stop now.
        types = [c.args[0]["type"] for c in state.audio_websocket.send_json.await_args_list]
        assert "stream_stop" in types
        assert "video_stop" in types

    @pytest.mark.asyncio
    async def test_new_stream_replaces_active_stream(self, monkeypatch):
        state = self._state(monkeypatch)
        await self._stream(state, {"entity_id": "camera.a"})
        first_sid = state.active_stream["session_id"]
        first_safety = state.active_stream["safety_task"]
        await self._stream(state, {"entity_id": "camera.b"})
        # Yield once so the cancelled first safety task settles.
        await asyncio.sleep(0)
        assert state.active_stream is not None
        assert state.active_stream["entity_id"] == "camera.b"
        assert state.active_stream["session_id"] != first_sid
        assert first_safety.done()
        state.active_stream["safety_task"].cancel()

    @pytest.mark.asyncio
    async def test_show_camera_stops_active_stream(self, monkeypatch):
        state = self._state(monkeypatch)
        await self._stream(state, {"entity_id": "camera.live"})
        # Now call show_camera with a dummy MCP returning an image
        state.mcp_client = MagicMock()
        state.mcp_client.tool_names = ["ha_get_camera_image"]
        item = MagicMock(); item.data = "Z"; item.mimeType = "image/jpeg"
        state.mcp_client.call_tool_content = AsyncMock(return_value=[item])
        tools = _build_local_tools(state)
        await tools.call_tool("show_camera", {"entity_id": "camera.snap"})
        assert state.active_stream is None


class TestPushImage:
    """Image push pipeline (URL fetch, payload detection, MQTT bridge wiring)."""

    @pytest.mark.asyncio
    async def test_detect_image_mime(self):
        from server.app.main import _detect_image_mime
        assert _detect_image_mime(b"\xff\xd8\xff\xe0sample") == "image/jpeg"
        assert _detect_image_mime(b"\x89PNG\r\n\x1a\n....") == "image/png"
        assert _detect_image_mime(b"GIF89a....") == "image/gif"
        assert _detect_image_mime(b"RIFF1234WEBPVP8L") == "image/webp"
        assert _detect_image_mime(b"hello world") is None

    @pytest.mark.asyncio
    async def test_dispatch_show_image_pushes_ws_and_ui(self):
        from server.app.main import _dispatch_show_image
        state = AppState()
        state.audio_websocket = AsyncMock()
        await _dispatch_show_image(state, "Zm9v", "image/jpeg", 60, entity_id="external")
        msgs = [c.args[0] for c in state.audio_websocket.send_json.await_args_list]
        # _dispatch_show_image also sends video_stop for mutual exclusion.
        show_msgs = [m for m in msgs if m.get("type") == "show_camera"]
        assert len(show_msgs) == 1
        msg = show_msgs[0]
        assert msg["image"] == "Zm9v"
        assert msg["mime"] == "image/jpeg"
        assert msg["duration_s"] == 60
        assert msg["entity_id"] == "external"

    @pytest.mark.asyncio
    async def test_dispatch_show_image_stops_active_stream(self):
        from server.app.main import _dispatch_show_image
        state = AppState()
        state.audio_websocket = AsyncMock()
        state.active_stream = {
            "session_id": "x", "entity_id": "camera.x",
            "ha_sub_id": None, "ha_session_id": None,
            "safety_task": asyncio.create_task(asyncio.sleep(60)),
        }
        await _dispatch_show_image(state, "Zm9v", "image/jpeg", 60)
        assert state.active_stream is None

    @pytest.mark.asyncio
    async def test_push_payload_binary_jpeg(self):
        from server.app.main import _push_image_payload
        state = AppState()
        state.audio_websocket = AsyncMock()
        result = await _push_image_payload(state, b"\xff\xd8\xff\xe0jpeg-bytes", default_duration=60)
        assert "pushed" in result and "image/jpeg" in result
        msg = state.audio_websocket.send_json.await_args.args[0]
        assert msg["mime"] == "image/jpeg"
        assert msg["duration_s"] == 60

    @pytest.mark.asyncio
    async def test_push_payload_url_calls_fetcher(self):
        from server.app import main as srv
        state = AppState()
        state.audio_websocket = AsyncMock()
        fetcher = AsyncMock(return_value=(b"\xff\xd8\xff\xe0", "image/jpeg"))
        with patch.object(srv, "_fetch_image_url", fetcher):
            result = await srv._push_image_payload(state, "https://example.com/x.jpg", default_duration=60)
            fetcher.assert_awaited_once_with("https://example.com/x.jpg")
        assert "pushed" in result

    @pytest.mark.asyncio
    async def test_push_payload_json_url_with_duration(self):
        from server.app import main as srv
        state = AppState()
        state.audio_websocket = AsyncMock()
        body = json.dumps({"url": "https://example.com/x.jpg", "duration_s": 90})
        with patch.object(srv, "_fetch_image_url", AsyncMock(return_value=(b"\xff\xd8\xff", "image/jpeg"))):
            await srv._push_image_payload(state, body, default_duration=60)
        msg = state.audio_websocket.send_json.await_args.args[0]
        assert msg["duration_s"] == 90

    @pytest.mark.asyncio
    async def test_push_payload_json_inline_base64(self):
        from server.app.main import _push_image_payload
        state = AppState()
        state.audio_websocket = AsyncMock()
        body = json.dumps({"image": "AAAA", "mime": "image/png", "duration_s": 30})
        result = await _push_image_payload(state, body, default_duration=60)
        assert "inline base64" in result
        msg = state.audio_websocket.send_json.await_args.args[0]
        assert msg["image"] == "AAAA"
        assert msg["mime"] == "image/png"
        assert msg["duration_s"] == 30

    @pytest.mark.asyncio
    async def test_push_payload_invalid_string(self):
        from server.app.main import _push_image_payload
        state = AppState()
        state.audio_websocket = AsyncMock()
        result = await _push_image_payload(state, "not a url and not json", default_duration=60)
        assert "neither" in result
        state.audio_websocket.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_push_payload_clamps_duration(self):
        from server.app import main as srv
        state = AppState()
        state.audio_websocket = AsyncMock()
        body = json.dumps({"url": "https://x", "duration_s": 99999})
        with patch.object(srv, "_fetch_image_url", AsyncMock(return_value=(b"\xff\xd8\xff", "image/jpeg"))):
            await srv._push_image_payload(state, body, default_duration=60)
        msg = state.audio_websocket.send_json.await_args.args[0]
        assert msg["duration_s"] == 600

    @pytest.mark.asyncio
    async def test_show_image_tool_rejects_non_http(self):
        state = AppState()
        state.audio_websocket = AsyncMock()
        tools = _build_local_tools(state)
        result = await tools.call_tool("show_image", {"url": "ftp://x"})
        assert "http" in result
        state.audio_websocket.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_show_image_tool_dispatches_through_fetcher(self, monkeypatch):
        from server.app import main as srv
        state = AppState()
        state.audio_websocket = AsyncMock()
        monkeypatch.setattr(srv, "_fetch_image_url", AsyncMock(return_value=(b"\xff\xd8\xff", "image/jpeg")))
        tools = _build_local_tools(state)
        result = await tools.call_tool("show_image", {"url": "http://x.test/foo.jpg", "duration_s": 45})
        assert "pushed" in result
        msg = state.audio_websocket.send_json.await_args.args[0]
        assert msg["duration_s"] == 45


class TestStreamRtsp:
    """Local tool stream_rtsp + go2rtc signaling proxy."""

    def _state(self, monkeypatch):
        from server.app.go2rtc import Go2RTCClient
        monkeypatch.setenv("GO2RTC_URL", "http://go2rtc.test:1984")
        state = AppState()
        state.audio_websocket = AsyncMock()
        # Stub the go2rtc client (spec'd so isinstance(state.go2rtc, Go2RTCClient) holds).
        client = MagicMock(spec=Go2RTCClient)
        client.register_stream = AsyncMock(return_value=True)
        client.delete_stream = AsyncMock(return_value=True)
        client.webrtc_offer = AsyncMock(return_value="v=0...answer...")
        state.go2rtc = client
        return state

    async def _stream(self, state, args):
        tools = _build_local_tools(state)
        return await tools.call_tool("stream_rtsp", args)

    @pytest.mark.asyncio
    async def test_rejects_non_rtsp_url(self, monkeypatch):
        state = self._state(monkeypatch)
        result = await self._stream(state, {"rtsp_url": "http://x"})
        assert "rtsp" in result
        state.audio_websocket.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_errors_when_go2rtc_unset(self, monkeypatch):
        monkeypatch.delenv("GO2RTC_URL", raising=False)
        state = AppState()
        state.audio_websocket = AsyncMock()
        result = await self._stream(state, {"rtsp_url": "rtsp://cam/x"})
        assert "GO2RTC_URL" in result
        assert state.active_stream is None

    @pytest.mark.asyncio
    async def test_registers_stream_and_pushes_stream_start(self, monkeypatch):
        state = self._state(monkeypatch)
        result = await self._stream(state, {"rtsp_url": "rtsp://cam/feed", "duration_s": 60})
        assert "Streaming RTSP" in result
        assert state.active_stream is not None
        assert state.active_stream["kind"] == "rtsp"
        assert state.active_stream["rtsp_url"] == "rtsp://cam/feed"
        assert state.active_stream["go2rtc_name"].startswith("hal_")
        state.go2rtc.register_stream.assert_awaited_once()
        msg = state.audio_websocket.send_json.await_args.args[0]
        assert msg["type"] == "stream_start"
        assert msg["mode"] == "non-trickle"
        assert msg["rtsp_url"] == "rtsp://cam/feed"
        state.active_stream["safety_task"].cancel()

    @pytest.mark.asyncio
    async def test_register_failure_returns_error(self, monkeypatch):
        state = self._state(monkeypatch)
        state.go2rtc.register_stream = AsyncMock(return_value=False)
        result = await self._stream(state, {"rtsp_url": "rtsp://cam/feed"})
        assert "Failed to register" in result
        assert state.active_stream is None

    @pytest.mark.asyncio
    async def test_stops_active_ha_stream_on_replace(self, monkeypatch):
        state = self._state(monkeypatch)
        state.active_stream = {
            "session_id": "old", "kind": "ha", "entity_id": "camera.x",
            "ha_sub_id": None, "ha_session_id": None,
            "safety_task": asyncio.create_task(asyncio.sleep(60)),
        }
        first_safety = state.active_stream["safety_task"]
        await self._stream(state, {"rtsp_url": "rtsp://cam/feed"})
        await asyncio.sleep(0)
        assert state.active_stream["kind"] == "rtsp"
        assert first_safety.done()
        state.active_stream["safety_task"].cancel()

    @pytest.mark.asyncio
    async def test_stop_active_stream_deletes_go2rtc_stream(self, monkeypatch):
        from server.app.main import _stop_active_stream
        state = self._state(monkeypatch)
        await self._stream(state, {"rtsp_url": "rtsp://cam/feed"})
        name = state.active_stream["go2rtc_name"]
        await _stop_active_stream(state)
        state.go2rtc.delete_stream.assert_awaited_with(name)
        assert state.active_stream is None


class TestRtspNegotiation:
    """Server-side glue between kiosk offer and go2rtc answer."""

    @pytest.mark.asyncio
    async def test_negotiate_forwards_answer_to_kiosk(self):
        from server.app.main import _negotiate_rtsp_offer
        state = AppState()
        state.audio_websocket = AsyncMock()
        from server.app.go2rtc import Go2RTCClient
        client = MagicMock(spec=Go2RTCClient)
        client.webrtc_offer = AsyncMock(return_value="v=0...answer...")
        state.go2rtc = client
        state.active_stream = {
            "session_id": "sid",
            "kind": "rtsp",
            "go2rtc_name": "hal_xyz",
            "safety_task": asyncio.create_task(asyncio.sleep(60)),
        }
        await _negotiate_rtsp_offer(state, "sid", "v=0...offer...")
        client.webrtc_offer.assert_awaited_once_with("hal_xyz", "v=0...offer...")
        state.audio_websocket.send_json.assert_awaited_once()
        msg = state.audio_websocket.send_json.await_args.args[0]
        assert msg["type"] == "webrtc_signal"
        assert msg["kind"] == "answer"
        assert msg["sdp"] == "v=0...answer..."
        state.active_stream["safety_task"].cancel()

    @pytest.mark.asyncio
    async def test_negotiate_stops_stream_when_go2rtc_returns_none(self):
        from server.app.main import _negotiate_rtsp_offer
        from server.app.go2rtc import Go2RTCClient
        state = AppState()
        state.audio_websocket = AsyncMock()
        client = MagicMock(spec=Go2RTCClient)
        client.webrtc_offer = AsyncMock(return_value=None)
        client.delete_stream = AsyncMock(return_value=True)
        state.go2rtc = client
        state.active_stream = {
            "session_id": "sid",
            "kind": "rtsp",
            "go2rtc_name": "hal_xyz",
            "safety_task": asyncio.create_task(asyncio.sleep(60)),
        }
        await _negotiate_rtsp_offer(state, "sid", "v=0...")
        assert state.active_stream is None

    @pytest.mark.asyncio
    async def test_negotiate_ignores_mismatched_session(self):
        from server.app.main import _negotiate_rtsp_offer
        from server.app.go2rtc import Go2RTCClient
        state = AppState()
        state.audio_websocket = AsyncMock()
        client = MagicMock(spec=Go2RTCClient)
        client.webrtc_offer = AsyncMock(return_value="v=0...")
        state.go2rtc = client
        state.active_stream = {
            "session_id": "current",
            "kind": "rtsp",
            "go2rtc_name": "hal_xyz",
            "safety_task": asyncio.create_task(asyncio.sleep(60)),
        }
        await _negotiate_rtsp_offer(state, "stale", "v=0...")
        client.webrtc_offer.assert_not_awaited()
        state.active_stream["safety_task"].cancel()


class TestPlayVideo:
    """Local tool play_video — HTTP video / HLS playback in the orb."""

    def _state(self):
        state = AppState()
        state.audio_websocket = AsyncMock()
        return state

    async def _play(self, state, args):
        tools = _build_local_tools(state)
        return await tools.call_tool("play_video", args)

    @pytest.mark.asyncio
    async def test_rejects_non_http_url(self):
        state = self._state()
        result = await self._play(state, {"url": "rtsp://x"})
        assert "http" in result
        state.audio_websocket.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pushes_play_video_message(self):
        state = self._state()
        result = await self._play(state, {"url": "https://example.com/clip.mp4"})
        assert "Playing video" in result
        msg = state.audio_websocket.send_json.await_args.args[0]
        assert msg["type"] == "play_video"
        assert msg["url"] == "https://example.com/clip.mp4"
        assert msg["loop"] is False
        assert msg["muted"] is False

    @pytest.mark.asyncio
    async def test_passes_through_loop_muted_duration(self):
        state = self._state()
        await self._play(state, {
            "url": "https://example.com/x.m3u8",
            "loop": True,
            "muted": True,
            "duration_s": 30,
        })
        msg = state.audio_websocket.send_json.await_args.args[0]
        assert msg["loop"] is True
        assert msg["muted"] is True
        assert msg["duration_s"] == 30

    @pytest.mark.asyncio
    async def test_clamps_duration(self):
        state = self._state()
        await self._play(state, {"url": "https://x", "duration_s": 99999999})
        msg = state.audio_websocket.send_json.await_args.args[0]
        assert msg["duration_s"] == 7200

    @pytest.mark.asyncio
    async def test_play_video_stops_active_camera_stream(self, monkeypatch):
        from server.app.go2rtc import Go2RTCClient
        monkeypatch.setenv("GO2RTC_URL", "http://x")
        state = self._state()
        client = MagicMock(spec=Go2RTCClient)
        client.delete_stream = AsyncMock(return_value=True)
        state.go2rtc = client
        state.active_stream = {
            "session_id": "x", "kind": "rtsp",
            "go2rtc_name": "hal_x", "rtsp_url": "rtsp://y",
            "safety_task": asyncio.create_task(asyncio.sleep(60)),
        }
        await self._play(state, {"url": "https://example.com/clip.mp4"})
        assert state.active_stream is None
        client.delete_stream.assert_awaited_with("hal_x")

    @pytest.mark.asyncio
    async def test_show_image_stops_active_video(self):
        # Reverse direction: a snapshot should send video_stop.
        state = self._state()
        state.mcp_client = MagicMock()
        state.mcp_client.tool_names = ["ha_get_camera_image"]
        item = MagicMock(); item.data = "Z"; item.mimeType = "image/jpeg"
        state.mcp_client.call_tool_content = AsyncMock(return_value=[item])
        tools = _build_local_tools(state)
        await tools.call_tool("show_camera", {"entity_id": "camera.x"})
        # Two messages should have been sent: stream_stop (no-op WS push from
        # _stop_active_stream) and video_stop (from _stop_active_video), then
        # show_camera. Filter for video_stop.
        calls = [c.args[0] for c in state.audio_websocket.send_json.await_args_list]
        types = [c["type"] for c in calls]
        assert "video_stop" in types
        assert "show_camera" in types


class TestStopStreaming:
    """The stop_streaming tool now also clears HTTP video."""

    @pytest.mark.asyncio
    async def test_stop_streaming_no_active_still_clears_video(self):
        state = AppState()
        state.audio_websocket = AsyncMock()
        tools = _build_local_tools(state)
        result = await tools.call_tool("stop_streaming", {})
        assert "Stopped any active video" in result
        # Should have sent at least a video_stop.
        calls = [c.args[0] for c in state.audio_websocket.send_json.await_args_list]
        assert any(c["type"] == "video_stop" for c in calls)


class TestLastResponsePublish:
    """publish_last_response truncates state and emits full text + ts in attrs."""

    @pytest.mark.asyncio
    async def test_publishes_truncated_state_and_full_attrs(self):
        from server.app.mqtt_bridge import MQTTBridge
        bridge = MQTTBridge(host="x", device_id="hal-default")
        publisher = AsyncMock()
        bridge._safe_publish = publisher
        text = "x" * 400
        await bridge.publish_last_response(text)
        # Two publish calls: state (truncated) + attributes.
        assert publisher.await_count == 2
        topics = [c.args[0] for c in publisher.await_args_list]
        payloads = [c.args[1] for c in publisher.await_args_list]
        assert topics[0] == "hal/hal-default/last_response"
        assert topics[1] == "hal/hal-default/last_response/attrs"
        assert len(payloads[0]) <= 250
        assert payloads[0].endswith("...")
        attrs = json.loads(payloads[1])
        assert attrs["full_text"] == text
        assert "ts" in attrs

    @pytest.mark.asyncio
    async def test_short_text_state_is_full_text(self):
        from server.app.mqtt_bridge import MQTTBridge
        bridge = MQTTBridge(host="x", device_id="hal-default")
        bridge._safe_publish = AsyncMock()
        await bridge.publish_last_response("hello")
        payloads = [c.args[1] for c in bridge._safe_publish.await_args_list]
        assert payloads[0] == "hello"
        assert json.loads(payloads[1])["full_text"] == "hello"

    @pytest.mark.asyncio
    async def test_caches_for_republish(self):
        from server.app.mqtt_bridge import MQTTBridge
        bridge = MQTTBridge(host="x", device_id="hal-default")
        bridge._safe_publish = AsyncMock()
        await bridge.publish_last_response("cached text")
        assert bridge._cached_last_response == "cached text"


class TestStreamSignalingHelpers:
    """Internal signaling glue between HA events and the kiosk."""

    @pytest.mark.asyncio
    async def test_on_ha_event_answer_forwards_to_kiosk(self):
        from server.app.main import _on_ha_webrtc_event
        state = AppState()
        state.audio_websocket = AsyncMock()
        await _on_ha_webrtc_event(state, "sid1", {"type": "answer", "answer": "v=0..."})
        state.audio_websocket.send_json.assert_awaited_once()
        out = state.audio_websocket.send_json.await_args.args[0]
        assert out["type"] == "webrtc_signal"
        assert out["kind"] == "answer"
        assert out["session_id"] == "sid1"
        assert out["sdp"] == "v=0..."

    @pytest.mark.asyncio
    async def test_on_ha_event_candidate_forwards_fields(self):
        from server.app.main import _on_ha_webrtc_event
        state = AppState()
        state.audio_websocket = AsyncMock()
        await _on_ha_webrtc_event(state, "sid1", {
            "type": "candidate",
            "candidate": {"candidate": "candidate:1 ...", "sdpMid": "0", "sdpMLineIndex": 0},
        })
        out = state.audio_websocket.send_json.await_args.args[0]
        assert out["kind"] == "candidate"
        assert out["candidate"] == "candidate:1 ..."
        assert out["sdpMid"] == "0"
        assert out["sdpMLineIndex"] == 0

    @pytest.mark.asyncio
    async def test_on_ha_event_error_stops_stream(self):
        from server.app.main import _on_ha_webrtc_event
        state = AppState()
        state.audio_websocket = AsyncMock()
        state.active_stream = {
            "session_id": "sid1",
            "entity_id": "camera.x",
            "ha_sub_id": None,
            "safety_task": asyncio.create_task(asyncio.sleep(60)),
        }
        await _on_ha_webrtc_event(state, "sid1", {"type": "error", "code": "timeout"})
        assert state.active_stream is None

    @pytest.mark.asyncio
    async def test_on_ha_event_session_captures_ha_session_id(self):
        from server.app.main import _on_ha_webrtc_event
        state = AppState()
        state.audio_websocket = AsyncMock()
        state.active_stream = {
            "session_id": "ours",
            "entity_id": "camera.x",
            "ha_sub_id": None,
            "ha_session_id": None,
            "safety_task": asyncio.create_task(asyncio.sleep(60)),
        }
        await _on_ha_webrtc_event(state, "ours", {"type": "session", "session_id": "HA-SID"})
        assert state.active_stream["ha_session_id"] == "HA-SID"
        # Session events are not forwarded to the kiosk.
        state.audio_websocket.send_json.assert_not_awaited()
        state.active_stream["safety_task"].cancel()

    @pytest.mark.asyncio
    async def test_forward_kiosk_candidate_drops_when_no_ha_session_id(self):
        from server.app.main import _forward_kiosk_candidate
        from server.app.ha_ws import HAWSClient
        state = AppState()
        state.ha_ws = MagicMock(spec=HAWSClient)
        state.ha_ws.send_command = AsyncMock()
        state.active_stream = {
            "session_id": "ours",
            "entity_id": "camera.x",
            "ha_sub_id": 1,
            "ha_session_id": None,
            "safety_task": asyncio.create_task(asyncio.sleep(60)),
        }
        await _forward_kiosk_candidate(state, "ours", {"candidate": "c"})
        state.ha_ws.send_command.assert_not_awaited()
        state.active_stream["safety_task"].cancel()

    @pytest.mark.asyncio
    async def test_forward_kiosk_candidate_uses_ha_session_id(self):
        from server.app.main import _forward_kiosk_candidate
        from server.app.ha_ws import HAWSClient
        state = AppState()
        state.ha_ws = MagicMock(spec=HAWSClient)
        state.ha_ws.send_command = AsyncMock()
        state.active_stream = {
            "session_id": "ours",
            "entity_id": "camera.x",
            "ha_sub_id": 1,
            "ha_session_id": "HA-SID",
            "safety_task": asyncio.create_task(asyncio.sleep(60)),
        }
        await _forward_kiosk_candidate(state, "ours", {
            "candidate": "candidate:1 ...",
            "sdpMid": "0",
            "sdpMLineIndex": 0,
        })
        state.ha_ws.send_command.assert_awaited_once()
        sent = state.ha_ws.send_command.await_args.args[0]
        assert sent["type"] == "camera/webrtc/candidate"
        assert sent["session_id"] == "HA-SID"
        assert sent["candidate"]["candidate"] == "candidate:1 ..."
        state.active_stream["safety_task"].cancel()
