"""Tests for the Wyoming protocol TTS engine."""

import asyncio
import io
import json
import wave
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.app.tts import TTSEngine


def _make_wyoming_response(text: str = "hello", rate: int = 22050, width: int = 2, channels: int = 1) -> list[bytes]:
    """Build a sequence of Wyoming protocol messages simulating a TTS response."""
    raw_audio = b"\x00\x80" * 1000  # 1000 samples of simple audio

    messages = []

    # audio-start
    start_event = {"type": "audio-start", "data": {"rate": rate, "width": width, "channels": channels}}
    messages.append(json.dumps(start_event).encode() + b"\n")

    # audio-chunk with payload
    chunk_event = {"type": "audio-chunk", "data": {"rate": rate, "width": width, "channels": channels}, "payload_length": len(raw_audio)}
    messages.append(json.dumps(chunk_event).encode() + b"\n" + raw_audio)

    # audio-stop
    stop_event = {"type": "audio-stop"}
    messages.append(json.dumps(stop_event).encode() + b"\n")

    return messages


class FakeReader:
    """Simulates an asyncio.StreamReader with pre-loaded Wyoming messages."""

    def __init__(self, messages: list[bytes]):
        self._buffer = b"".join(messages)
        self._pos = 0

    async def readline(self):
        if self._pos >= len(self._buffer):
            return b""
        end = self._buffer.index(b"\n", self._pos) + 1
        line = self._buffer[self._pos:end]
        self._pos = end
        # If the JSON has payload_length, the payload follows immediately
        return line

    async def readexactly(self, n):
        data = self._buffer[self._pos:self._pos + n]
        self._pos += n
        return data


class FakeWriter:
    """Captures what was written to an asyncio.StreamWriter."""

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
        assert engine.sample_rate == 22050

    def test_init_custom(self):
        engine = TTSEngine(host="10.20.30.185", port=10300)
        assert engine.host == "10.20.30.185"
        assert engine.port == 10300


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
        messages = _make_wyoming_response("test text", rate=16000)
        reader = FakeReader(messages)
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

        # Verify the synthesize event was sent
        sent = writer.data.decode("utf-8", errors="replace")
        assert '"type": "synthesize"' in sent
        assert '"text": "test text"' in sent

        # Writer should be closed
        assert writer.closed

    @pytest.mark.asyncio
    async def test_synthesis_updates_sample_rate(self):
        messages = _make_wyoming_response(rate=44100, width=2, channels=2)
        reader = FakeReader(messages)
        writer = FakeWriter()

        engine = TTSEngine()
        assert engine.sample_rate == 22050  # default

        with patch("asyncio.open_connection", return_value=(reader, writer)):
            result = await engine.synthesize("hello")

        assert engine.sample_rate == 44100

    @pytest.mark.asyncio
    async def test_error_event_returns_none(self):
        error_event = {"type": "error", "data": {"text": "Model not loaded"}}
        messages = [json.dumps(error_event).encode() + b"\n"]
        reader = FakeReader(messages)
        writer = FakeWriter()

        engine = TTSEngine()
        with patch("asyncio.open_connection", return_value=(reader, writer)):
            result = await engine.synthesize("hello")

        assert result is None

    @pytest.mark.asyncio
    async def test_no_audio_chunks_returns_none(self):
        """Server sends audio-start and audio-stop with no chunks."""
        messages = [
            json.dumps({"type": "audio-start", "data": {"rate": 22050, "width": 2, "channels": 1}}).encode() + b"\n",
            json.dumps({"type": "audio-stop"}).encode() + b"\n",
        ]
        reader = FakeReader(messages)
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
        await TTSEngine._send_event(writer, {"type": "test", "data": {"key": "value"}})
        sent = writer.data.decode("utf-8")
        parsed = json.loads(sent.strip())
        assert parsed["type"] == "test"
        assert "payload_length" not in parsed

    @pytest.mark.asyncio
    async def test_send_event_with_payload(self):
        writer = FakeWriter()
        payload = b"\xff\x00\xab"
        await TTSEngine._send_event(writer, {"type": "chunk"}, payload=payload)
        sent_str = writer.data[:writer.data.index(b"\n") + 1].decode("utf-8")
        parsed = json.loads(sent_str)
        assert parsed["payload_length"] == 3
        assert writer.data.endswith(payload)

    @pytest.mark.asyncio
    async def test_recv_event_simple(self):
        event = {"type": "audio-stop"}
        data = json.dumps(event).encode() + b"\n"
        reader = FakeReader([data])
        result = await TTSEngine._recv_event(reader)
        assert result["type"] == "audio-stop"

    @pytest.mark.asyncio
    async def test_recv_event_with_payload(self):
        payload = b"\x01\x02\x03\x04"
        event = {"type": "audio-chunk", "payload_length": 4}
        data = json.dumps(event).encode() + b"\n" + payload
        reader = FakeReader([data])
        result = await TTSEngine._recv_event(reader)
        assert result["type"] == "audio-chunk"
        assert result["_payload"] == payload

    @pytest.mark.asyncio
    async def test_recv_event_eof(self):
        reader = FakeReader([])
        result = await TTSEngine._recv_event(reader)
        assert result is None
