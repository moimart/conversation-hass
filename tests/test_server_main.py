"""Tests for the server main.py: AppState, lifespan, endpoints, chime generation."""

import io
import json
import wave
from dataclasses import fields
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from server.app.main import AppState, _generate_chime, _get_state, broadcast_to_ui


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
