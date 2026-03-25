"""Tests for the Wyoming protocol TTS engine."""

import asyncio
import io
import json
import struct
import wave
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.app.tts import TTSEngine


def _make_wyoming_event(event_type: str, data: dict | None = None, payload: bytes | None = None) -> bytes:
    """Build a Wyoming protocol event (header line + data bytes + payload bytes)."""
    data_bytes = json.dumps(data).encode("utf-8") if data else b""
    header = {
        "type": event_type,
        "data_length": len(data_bytes),
        "payload_length": len(payload) if payload else 0,
    }
    result = json.dumps(header).encode("utf-8") + b"\n"
    if data_bytes:
        result += data_bytes
    if payload:
        result += payload
    return result


def _make_wyoming_response(rate: int = 22050, width: int = 2, channels: int = 1) -> bytes:
    """Build a full Wyoming TTS response sequence."""
    raw_audio = b"\x00\x80" * 1000

    parts = []
    parts.append(_make_wyoming_event("audio-start", {"rate": rate, "width": width, "channels": channels}))
    parts.append(_make_wyoming_event("audio-chunk", {"rate": rate, "width": width, "channels": channels}, raw_audio))
    parts.append(_make_wyoming_event("audio-stop"))
    return b"".join(parts)


class FakeReader:
    """Simulates an asyncio.StreamReader with pre-loaded data."""

    def __init__(self, data: bytes):
        self._buf = data
        self._pos = 0

    async def readline(self):
        if self._pos >= len(self._buf):
            return b""
        end = self._buf.index(b"\n", self._pos) + 1
        line = self._buf[self._pos:end]
        self._pos = end
        return line

    async def readexactly(self, n):
        data = self._buf[self._pos:self._pos + n]
        self._pos += n
        return data


class FakeWriter:
    """Captures what was written to a StreamWriter."""

    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class TestTTSEngine:
    def test_init_defaults(self):
        engine = TTSEngine()
        assert engine.host == "localhost"
        assert engine.port == 10200
        assert engine.voice == ""
        assert engine.sample_rate == 22050

    def test_init_custom(self):
        engine = TTSEngine(host="10.20.30.185", port=10300, voice="en-us")
        assert engine.host == "10.20.30.185"
        assert engine.port == 10300
        assert engine.voice == "en-us"


class TestTTSSynthesize:
    @pytest.mark.asyncio
    async def test_empty_text_returns_none(self):
        engine = TTSEngine()
        result = await engine.synthesize("")
        assert result is None

    @pytest.mark.asyncio
    async def test_whitespace_text_returns_none(self):
        engine = TTSEngine()
        result = await engine.synthesize("   ")
        assert result is None

    @pytest.mark.asyncio
    async def test_connection_failure_returns_none(self):
        engine = TTSEngine(host="unreachable", port=99999)
        with patch("asyncio.open_connection", side_effect=ConnectionRefusedError("refused")):
            result = await engine.synthesize("hello")
        assert result is None

    @pytest.mark.asyncio
    async def test_successful_synthesis(self):
        response_data = _make_wyoming_response(rate=16000)
        reader = FakeReader(response_data)
        writer = FakeWriter()

        engine = TTSEngine()
        with patch("asyncio.open_connection", return_value=(reader, writer)):
            result = await engine.synthesize("test text")

        assert result is not None
        assert len(result) > 44  # WAV header + audio data

        # Verify it's valid WAV
        buf = io.BytesIO(result)
        with wave.open(buf, "rb") as wf:
            assert wf.getframerate() == 16000
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2

        assert writer.closed

    @pytest.mark.asyncio
    async def test_synthesis_updates_sample_rate(self):
        response_data = _make_wyoming_response(rate=44100, width=2, channels=2)
        reader = FakeReader(response_data)
        writer = FakeWriter()

        engine = TTSEngine()
        assert engine.sample_rate == 22050

        with patch("asyncio.open_connection", return_value=(reader, writer)):
            await engine.synthesize("hello")

        assert engine.sample_rate == 44100

    @pytest.mark.asyncio
    async def test_voice_included_in_request(self):
        response_data = _make_wyoming_response()
        reader = FakeReader(response_data)
        writer = FakeWriter()

        engine = TTSEngine(voice="en-us-female")
        with patch("asyncio.open_connection", return_value=(reader, writer)):
            await engine.synthesize("hello")

        sent = writer.data.decode("utf-8", errors="replace")
        assert "en-us-female" in sent

    @pytest.mark.asyncio
    async def test_error_event_returns_none(self):
        response_data = _make_wyoming_event("error", {"text": "Model not loaded"})
        reader = FakeReader(response_data)
        writer = FakeWriter()

        engine = TTSEngine()
        with patch("asyncio.open_connection", return_value=(reader, writer)):
            result = await engine.synthesize("hello")

        assert result is None

    @pytest.mark.asyncio
    async def test_no_audio_chunks_returns_none(self):
        parts = []
        parts.append(_make_wyoming_event("audio-start", {"rate": 22050, "width": 2, "channels": 1}))
        parts.append(_make_wyoming_event("audio-stop"))
        response_data = b"".join(parts)
        reader = FakeReader(response_data)
        writer = FakeWriter()

        engine = TTSEngine()
        with patch("asyncio.open_connection", return_value=(reader, writer)):
            result = await engine.synthesize("hello")

        assert result is None


class TestWrapWav:
    def test_valid_wav_output(self):
        raw = b"\x00" * 200
        wav_bytes = TTSEngine._wrap_wav(raw, 16000, 2, 1)

        buf = io.BytesIO(wav_bytes)
        with wave.open(buf, "rb") as wf:
            assert wf.getframerate() == 16000
            assert wf.getsampwidth() == 2
            assert wf.getnchannels() == 1
            assert wf.readframes(100) == raw


class TestSendRecvEvent:
    @pytest.mark.asyncio
    async def test_send_event_without_payload(self):
        writer = FakeWriter()
        await TTSEngine._send_event(writer, "synthesize", {"text": "hello"})
        sent = writer.data.decode("utf-8")
        # Header line should contain type and data_length
        header = json.loads(sent.split("\n")[0])
        assert header["type"] == "synthesize"
        assert header["data_length"] > 0
        assert header["payload_length"] == 0

    @pytest.mark.asyncio
    async def test_send_event_with_payload(self):
        writer = FakeWriter()
        payload = b"\xff\x00\xab"
        await TTSEngine._send_event(writer, "audio-chunk", {"rate": 22050}, payload=payload)
        # Find header line
        header_end = writer.data.index(b"\n") + 1
        header = json.loads(writer.data[:header_end].decode())
        assert header["payload_length"] == 3
        assert writer.data.endswith(payload)

    @pytest.mark.asyncio
    async def test_recv_event_simple(self):
        data = _make_wyoming_event("audio-stop")
        reader = FakeReader(data)
        event_type, event_data, payload = await TTSEngine._recv_event(reader)
        assert event_type == "audio-stop"
        assert payload is None

    @pytest.mark.asyncio
    async def test_recv_event_with_data_and_payload(self):
        audio = b"\x01\x02\x03\x04"
        data = _make_wyoming_event("audio-chunk", {"rate": 22050}, audio)
        reader = FakeReader(data)
        event_type, event_data, payload = await TTSEngine._recv_event(reader)
        assert event_type == "audio-chunk"
        assert event_data["rate"] == 22050
        assert payload == audio

    @pytest.mark.asyncio
    async def test_recv_event_eof(self):
        reader = FakeReader(b"")
        event_type, event_data, payload = await TTSEngine._recv_event(reader)
        assert event_type is None
