"""Tests for the RPi AudioManager class."""

import asyncio
import io
import json
import wave
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import numpy as np
import pytest

# Mock pyaudio before importing
import sys
mock_pyaudio = MagicMock()
mock_pyaudio.paInt16 = 8
mock_pyaudio.PyAudio.return_value = MagicMock()
sys.modules.setdefault("pyaudio", mock_pyaudio)

# Mock websockets
mock_websockets = MagicMock()
sys.modules.setdefault("websockets", mock_websockets)

# Mock evdev
mock_evdev = MagicMock()
sys.modules.setdefault("evdev", mock_evdev)

from rpi.audio_streamer.main import AudioManager, _resample_audio


# --- Fixtures ---

@pytest.fixture
def mgr():
    """Create an AudioManager with mocked PyAudio."""
    with patch("rpi.audio_streamer.main.pyaudio") as mock_pa:
        mock_pa.paInt16 = 8
        mock_pa.PyAudio.return_value = MagicMock()
        m = AudioManager(
            ai_server_host="10.0.0.1",
            sample_rate=16000,
            channels=1,
            chunk_size=4096,
        )
    return m


def make_wav(rate=22050, channels=1, width=2, n_samples=1000) -> bytes:
    """Generate a minimal WAV file as bytes."""
    raw = np.zeros(n_samples * channels, dtype=np.int16).tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        wf.writeframes(raw)
    return buf.getvalue()


# --- Resample function ---

class TestResampleAudio:
    def test_same_rate_returns_unchanged(self):
        samples = np.array([1, 2, 3, 4], dtype=np.int16)
        result = _resample_audio(samples, 48000, 48000)
        np.testing.assert_array_equal(result, samples)

    def test_upsample_doubles_length(self):
        samples = np.array([100, 200, 300, 400], dtype=np.int16)
        result = _resample_audio(samples, 22050, 44100)
        # Should be roughly double the length
        assert len(result) == 8

    def test_upsample_preserves_dtype(self):
        samples = np.array([100, 200], dtype=np.int16)
        result = _resample_audio(samples, 22050, 44100)
        assert result.dtype == np.int16

    def test_downsample_halves_length(self):
        samples = np.array([100, 200, 300, 400, 500, 600], dtype=np.int16)
        result = _resample_audio(samples, 44100, 22050)
        assert len(result) == 3


# --- AudioManager init ---

class TestAudioManagerInit:
    def test_defaults(self, mgr):
        assert mgr.ai_server_host == "10.0.0.1"
        assert mgr.target_sample_rate == 16000
        assert mgr.target_channels == 1
        assert mgr.chunk_size == 4096
        assert mgr.tts_playing is False
        assert mgr.tts_volume == 0.7
        assert mgr.mic_muted is False
        assert mgr.tts_receiving is False
        assert mgr.chime_receiving is False
        assert len(mgr.ui_clients) == 0


# --- Device probing ---

class TestDeviceProbing:
    def test_find_audio_device_found(self, mgr):
        mgr.pa.get_device_count.return_value = 2
        mgr.pa.get_device_info_by_index.side_effect = [
            {"name": "Default", "defaultSampleRate": 44100, "maxInputChannels": 2},
            {"name": "Anker PowerConf S330", "defaultSampleRate": 48000, "maxInputChannels": 2},
        ]
        result = mgr.find_audio_device()
        assert result == 1

    def test_find_audio_device_not_found(self, mgr):
        mgr.pa.get_device_count.return_value = 1
        mgr.pa.get_device_info_by_index.return_value = {"name": "Default", "defaultSampleRate": 44100, "maxInputChannels": 2}
        result = mgr.find_audio_device()
        assert result is None

    def test_find_audio_device_case_insensitive(self, mgr):
        mgr.pa.get_device_count.return_value = 1
        mgr.pa.get_device_info_by_index.return_value = {"name": "ANKER POWERCONF S330", "defaultSampleRate": 48000, "maxInputChannels": 2}
        result = mgr.find_audio_device()
        assert result == 0

    def test_find_supported_sample_rate_preferred(self, mgr):
        mgr.pa.is_format_supported.return_value = True
        rate = mgr.find_supported_sample_rate(0, 48000)
        assert rate == 48000

    def test_find_supported_sample_rate_fallback(self, mgr):
        def side_effect(rate, **kwargs):
            if rate == 48000:
                raise ValueError("unsupported")
            return True
        mgr.pa.is_format_supported.side_effect = side_effect
        # 48000 fails, next candidate (48000 again in list) fails, then 44100 succeeds
        rate = mgr.find_supported_sample_rate(0, 16000)
        # 16000 is first candidate, if it raises, then 48000, etc.
        # Actually: candidates = [16000, 48000, 44100, 32000, 16000, 8000]
        # 16000 would succeed since only 48000 raises
        assert rate == 16000

    def test_find_supported_sample_rate_device_default(self, mgr):
        mgr.pa.is_format_supported.side_effect = ValueError("all fail")
        mgr.pa.get_device_info_by_index.return_value = {"defaultSampleRate": 44100}
        rate = mgr.find_supported_sample_rate(0, 16000)
        assert rate == 44100

    def test_find_output_device_caches(self, mgr):
        mgr.pa.get_device_count.return_value = 1
        mgr.pa.get_device_info_by_index.return_value = {"name": "Anker PowerConf S330", "maxOutputChannels": 2}
        result1 = mgr.find_output_device()
        result2 = mgr.find_output_device()
        assert result1 == 0
        assert result2 == 0
        # get_device_count should only be called once (cached)
        assert mgr.pa.get_device_count.call_count == 1

    def test_get_device_channels(self, mgr):
        mgr.pa.get_device_info_by_index.return_value = {"maxInputChannels": 2}
        assert mgr.get_device_channels(0) == 2

    def test_get_device_channels_default(self, mgr):
        assert mgr.get_device_channels(None) == 1

    def test_probe_device(self, mgr):
        mgr.pa.get_device_count.return_value = 1
        mgr.pa.get_device_info_by_index.return_value = {
            "name": "Anker PowerConf S330",
            "defaultSampleRate": 48000,
            "maxInputChannels": 2,
        }
        mgr.pa.is_format_supported.return_value = True
        mgr.probe_device()
        assert mgr.device_index == 0
        assert mgr.device_channels == 2
        assert mgr.needs_downmix is True


# --- Audio processing ---

class TestAudioProcessing:
    def test_downmix_to_mono(self, mgr):
        mgr.device_channels = 2
        # Stereo: L=100, R=200, L=300, R=400
        stereo = np.array([100, 200, 300, 400], dtype=np.int16)
        mono = mgr.downmix_to_mono(stereo.tobytes())
        result = np.frombuffer(mono, dtype=np.int16)
        assert len(result) == 2
        assert result[0] == 150  # (100+200)/2
        assert result[1] == 350  # (300+400)/2

    def test_apply_volume_full(self, mgr):
        mgr.tts_volume = 1.0
        frames = np.array([1000, 2000], dtype=np.int16).tobytes()
        result = mgr.apply_volume(frames)
        assert result == frames  # unchanged at full volume

    def test_apply_volume_half(self, mgr):
        mgr.tts_volume = 0.5
        frames = np.array([1000, 2000], dtype=np.int16).tobytes()
        result = mgr.apply_volume(frames)
        samples = np.frombuffer(result, dtype=np.int16)
        assert samples[0] == 500
        assert samples[1] == 1000

    def test_apply_volume_zero(self, mgr):
        mgr.tts_volume = 0.0
        frames = np.array([1000, 2000], dtype=np.int16).tobytes()
        result = mgr.apply_volume(frames)
        samples = np.frombuffer(result, dtype=np.int16)
        assert samples[0] == 0
        assert samples[1] == 0

    def test_apply_volume_clips(self, mgr):
        mgr.tts_volume = 0.5
        frames = np.array([32767, -32768], dtype=np.int16).tobytes()
        result = mgr.apply_volume(frames)
        samples = np.frombuffer(result, dtype=np.int16)
        # Should not overflow
        assert -32768 <= samples[0] <= 32767
        assert -32768 <= samples[1] <= 32767


# --- Binary data handling ---

class TestBinaryDataHandling:
    def test_handle_binary_chime(self, mgr):
        mgr.chime_receiving = True
        mgr.handle_binary_data(b"\x01\x02\x03")
        assert bytes(mgr.chime_buffer) == b"\x01\x02\x03"

    def test_handle_binary_tts(self, mgr):
        mgr.tts_receiving = True
        mgr.handle_binary_data(b"\x04\x05\x06")
        assert bytes(mgr.tts_buffer) == b"\x04\x05\x06"

    def test_handle_binary_chime_takes_priority(self, mgr):
        mgr.chime_receiving = True
        mgr.tts_receiving = True
        mgr.handle_binary_data(b"\x01")
        assert bytes(mgr.chime_buffer) == b"\x01"
        assert bytes(mgr.tts_buffer) == b""

    def test_handle_binary_neither_receiving(self, mgr):
        mgr.handle_binary_data(b"\x01\x02\x03")
        assert bytes(mgr.chime_buffer) == b""
        assert bytes(mgr.tts_buffer) == b""

    def test_handle_binary_accumulates(self, mgr):
        mgr.tts_receiving = True
        mgr.handle_binary_data(b"\x01\x02")
        mgr.handle_binary_data(b"\x03\x04")
        assert bytes(mgr.tts_buffer) == b"\x01\x02\x03\x04"


# --- Server message handling ---

class TestServerMessageHandling:
    @pytest.mark.asyncio
    async def test_chime_start(self, mgr):
        await mgr.handle_server_message("chime_start", {}, MagicMock())
        assert mgr.chime_receiving is True
        assert bytes(mgr.chime_buffer) == b""

    @pytest.mark.asyncio
    async def test_chime_end_plays_audio(self, mgr):
        mgr.chime_receiving = True
        mgr.chime_buffer = bytearray(make_wav())
        with patch.object(mgr, "play_tts_audio", new_callable=AsyncMock) as mock_play:
            await mgr.handle_server_message("chime_end", {}, MagicMock())
        assert mgr.chime_receiving is False
        mock_play.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_chime_end_empty_buffer(self, mgr):
        mgr.chime_receiving = True
        mgr.chime_buffer = bytearray()
        with patch.object(mgr, "play_tts_audio", new_callable=AsyncMock) as mock_play:
            await mgr.handle_server_message("chime_end", {}, MagicMock())
        mock_play.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tts_start(self, mgr):
        mgr.broadcast_to_ui = AsyncMock()
        await mgr.handle_server_message("tts_start", {}, MagicMock())
        assert mgr.tts_receiving is True
        mgr.broadcast_to_ui.assert_awaited_with({"type": "state", "state": "speaking"})

    @pytest.mark.asyncio
    async def test_tts_end_plays_and_notifies(self, mgr):
        mgr.tts_receiving = True
        mgr.tts_buffer = bytearray(make_wav())
        mgr.broadcast_to_ui = AsyncMock()
        mock_ws = MagicMock()
        mock_ws.send = AsyncMock()

        with patch.object(mgr, "play_tts_audio", new_callable=AsyncMock):
            await mgr.handle_server_message("tts_end", {}, mock_ws)

        assert mgr.tts_receiving is False
        # Should broadcast idle state
        mgr.broadcast_to_ui.assert_awaited_with({"type": "state", "state": "idle"})
        # Should notify server playback finished
        mock_ws.send.assert_awaited_once()
        sent = json.loads(mock_ws.send.call_args[0][0])
        assert sent["type"] == "tts_finished"

    @pytest.mark.asyncio
    async def test_transcription_forwarded(self, mgr):
        mgr.broadcast_to_ui = AsyncMock()
        msg = {"type": "transcription", "text": "hello"}
        await mgr.handle_server_message("transcription", msg, MagicMock())
        mgr.broadcast_to_ui.assert_awaited_with(msg)

    @pytest.mark.asyncio
    async def test_response_forwarded(self, mgr):
        mgr.broadcast_to_ui = AsyncMock()
        msg = {"type": "response", "text": "hi there"}
        await mgr.handle_server_message("response", msg, MagicMock())
        mgr.broadcast_to_ui.assert_awaited_with(msg)

    @pytest.mark.asyncio
    async def test_wake_forwarded(self, mgr):
        mgr.broadcast_to_ui = AsyncMock()
        msg = {"type": "wake"}
        await mgr.handle_server_message("wake", msg, MagicMock())
        mgr.broadcast_to_ui.assert_awaited_with(msg)

    @pytest.mark.asyncio
    async def test_unknown_type_ignored(self, mgr):
        mgr.broadcast_to_ui = AsyncMock()
        await mgr.handle_server_message("unknown_type", {}, MagicMock())
        mgr.broadcast_to_ui.assert_not_awaited()


# --- UI broadcasting ---

class TestBroadcastToUI:
    @pytest.mark.asyncio
    async def test_broadcast_sends_to_all(self, mgr):
        ws1 = MagicMock()
        ws1.send_str = AsyncMock()
        ws2 = MagicMock()
        ws2.send_str = AsyncMock()
        mgr.ui_clients = {ws1, ws2}

        await mgr.broadcast_to_ui({"type": "test"})
        ws1.send_str.assert_awaited_once()
        ws2.send_str.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_broadcast_removes_dead_clients(self, mgr):
        ws_alive = MagicMock()
        ws_alive.send_str = AsyncMock()
        ws_dead = MagicMock()
        ws_dead.send_str = AsyncMock(side_effect=ConnectionError("gone"))
        mgr.ui_clients = {ws_alive, ws_dead}

        await mgr.broadcast_to_ui({"type": "test"})
        assert ws_dead not in mgr.ui_clients
        assert ws_alive in mgr.ui_clients

    @pytest.mark.asyncio
    async def test_broadcast_empty_clients(self, mgr):
        # Should not raise
        await mgr.broadcast_to_ui({"type": "test"})


# --- Close stream ---

class TestCloseStream:
    def test_close_stream_success(self, mgr):
        stream = MagicMock()
        mgr.close_stream(stream)
        stream.stop_stream.assert_called_once()
        stream.close.assert_called_once()

    def test_close_stream_error_suppressed(self, mgr):
        stream = MagicMock()
        stream.stop_stream.side_effect = RuntimeError("already closed")
        # Should not raise
        mgr.close_stream(stream)


# --- Play TTS audio ---

class TestPlayTTSAudio:
    @pytest.mark.asyncio
    async def test_sets_tts_playing_flag(self, mgr):
        mgr.pa.open.return_value = MagicMock()
        mgr.pa.get_format_from_width.return_value = 8
        mgr._output_device = 0
        mgr._output_rate = 22050

        wav_data = make_wav(rate=22050)
        await mgr.play_tts_audio(wav_data)
        assert mgr.tts_playing is False  # should be reset after playback

    @pytest.mark.asyncio
    async def test_tts_playing_reset_on_error(self, mgr):
        mgr.pa.open.side_effect = RuntimeError("device error")
        mgr._output_device = 0
        mgr._output_rate = 22050

        wav_data = make_wav(rate=22050)
        await mgr.play_tts_audio(wav_data)
        assert mgr.tts_playing is False  # must be reset even on error

    @pytest.mark.asyncio
    async def test_volume_applied(self, mgr):
        mock_stream = MagicMock()
        mgr.pa.open.return_value = mock_stream
        mgr.pa.get_format_from_width.return_value = 8
        mgr._output_device = 0
        mgr._output_rate = 22050
        mgr.tts_volume = 0.5

        # Make a WAV with known loud samples
        n = 100
        raw = np.full(n, 10000, dtype=np.int16).tobytes()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(22050)
            wf.writeframes(raw)
        wav_data = buf.getvalue()

        await mgr.play_tts_audio(wav_data)

        # Check that write was called with quieter audio
        written_data = b"".join(call[0][0] for call in mock_stream.write.call_args_list)
        written_samples = np.frombuffer(written_data, dtype=np.int16)
        # At 50% volume, 10000 should become ~5000
        assert np.max(written_samples) < 6000


# --- Entry point ---

class TestMain:
    def test_main_creates_manager(self):
        with patch.dict(os.environ, {
            "AI_SERVER_HOST": "1.2.3.4",
            "SAMPLE_RATE": "48000",
            "CHANNELS": "2",
            "CHUNK_SIZE": "2048",
        }):
            with patch("rpi.audio_streamer.main.AudioManager") as MockMgr:
                mock_instance = MagicMock()
                mock_instance.run = AsyncMock()
                MockMgr.return_value = mock_instance

                with patch("rpi.audio_streamer.main.asyncio") as mock_asyncio:
                    from rpi.audio_streamer.main import main
                    main()

                MockMgr.assert_called_once_with(
                    ai_server_host="1.2.3.4",
                    sample_rate=48000,
                    channels=2,
                    chunk_size=2048,
                )


import os


# --- Sendspin coordination ---

class TestSendspinMusicState:
    """The /api/music/state endpoint sets the music_playing flag."""

    @pytest.mark.asyncio
    async def test_sets_playing_true(self, mgr):
        request = MagicMock()
        request.json = AsyncMock(return_value={"playing": True})
        resp = await mgr._set_music_state(request)
        assert mgr.music_playing is True
        assert resp.status == 204

    @pytest.mark.asyncio
    async def test_sets_playing_false(self, mgr):
        mgr.music_playing = True
        request = MagicMock()
        request.json = AsyncMock(return_value={"playing": False})
        resp = await mgr._set_music_state(request)
        assert mgr.music_playing is False
        assert resp.status == 204

    @pytest.mark.asyncio
    async def test_missing_field_defaults_false(self, mgr):
        mgr.music_playing = True
        request = MagicMock()
        request.json = AsyncMock(return_value={})
        await mgr._set_music_state(request)
        assert mgr.music_playing is False

    @pytest.mark.asyncio
    async def test_bad_payload_returns_400(self, mgr):
        request = MagicMock()
        request.json = AsyncMock(side_effect=ValueError("not json"))
        resp = await mgr._set_music_state(request)
        assert resp.status == 400


class TestSendspinDefaults:
    """AudioManager init defaults for the new sendspin-coordination fields."""

    def test_music_playing_defaults_false(self, mgr):
        assert mgr.music_playing is False

    def test_server_ws_defaults_none(self, mgr):
        assert mgr._server_ws is None
