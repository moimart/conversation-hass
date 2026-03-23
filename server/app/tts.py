"""Text-to-speech via Wyoming protocol.

Connects to an existing Wyoming-compatible TTS service (e.g., whisper-tts)
over TCP and streams back synthesized audio.

Wyoming protocol framing:
  - Each event: 4-byte big-endian header length + JSON header + optional payload
  - The JSON header contains "type", "data", and optionally "payload_length"
  - If "payload_length" > 0, that many bytes of binary data follow the header
"""

import asyncio
import io
import json
import logging
import struct
import wave

log = logging.getLogger("hal.tts")


class TTSEngine:
    """Wyoming protocol TTS client."""

    def __init__(self, host: str = "localhost", port: int = 10200):
        self.host = host
        self.port = port
        self._sample_rate = 22050
        self._sample_width = 2
        self._channels = 1

    async def initialize(self):
        """Verify connectivity to the Wyoming TTS service."""
        log.info(f"Wyoming TTS endpoint: {self.host}:{self.port}")
        try:
            reader, writer = await asyncio.open_connection(self.host, self.port)
            writer.close()
            await writer.wait_closed()
            log.info("Wyoming TTS service is reachable")
        except Exception as e:
            log.warning(f"Wyoming TTS not reachable at startup: {e}")
            log.warning("TTS will retry on first synthesis request")

    async def synthesize(self, text: str) -> bytes | None:
        """
        Synthesize text to speech via Wyoming protocol.

        Returns WAV audio bytes or None on failure.
        """
        if not text.strip():
            return None

        try:
            reader, writer = await asyncio.open_connection(self.host, self.port)
        except Exception as e:
            log.error(f"Cannot connect to Wyoming TTS at {self.host}:{self.port}: {e}")
            return None

        try:
            # Send synthesize event
            synth_event = {
                "type": "synthesize",
                "data": {
                    "text": text,
                },
            }
            await self._send_event(writer, synth_event)

            # Collect audio chunks
            audio_chunks: list[bytes] = []
            sample_rate = self._sample_rate
            sample_width = self._sample_width
            channels = self._channels

            while True:
                event, payload = await self._recv_event(reader)
                if event is None:
                    break

                event_type = event.get("type", "")

                if event_type == "audio-start":
                    data = event.get("data", {})
                    sample_rate = data.get("rate", sample_rate)
                    sample_width = data.get("width", sample_width)
                    channels = data.get("channels", channels)
                    log.debug(f"TTS audio: {sample_rate}Hz, {sample_width * 8}bit, {channels}ch")

                elif event_type == "audio-chunk":
                    if payload:
                        audio_chunks.append(payload)

                elif event_type == "audio-stop":
                    break

                elif event_type == "error":
                    error_msg = event.get("data", {}).get("text", "Unknown error")
                    log.error(f"Wyoming TTS error: {error_msg}")
                    return None

            writer.close()
            await writer.wait_closed()

            if not audio_chunks:
                log.warning("TTS returned no audio")
                return None

            # Assemble into WAV
            raw_audio = b"".join(audio_chunks)
            self._sample_rate = sample_rate
            self._sample_width = sample_width
            self._channels = channels

            wav_bytes = self._wrap_wav(raw_audio, sample_rate, sample_width, channels)
            log.info(f"TTS synthesized {len(raw_audio)} bytes of audio ({len(text)} chars)")
            return wav_bytes

        except Exception as e:
            log.error(f"TTS synthesis failed: {e}")
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            return None

    @staticmethod
    async def _send_event(writer: asyncio.StreamWriter, event: dict, payload: bytes | None = None):
        """Send a Wyoming protocol event (length-prefixed)."""
        if payload:
            event["payload_length"] = len(payload)

        header_bytes = json.dumps(event).encode("utf-8")
        header_len = struct.pack(">I", len(header_bytes))

        writer.write(header_len + header_bytes)
        if payload:
            writer.write(payload)
        await writer.drain()

    @staticmethod
    async def _recv_event(reader: asyncio.StreamReader) -> tuple[dict | None, bytes | None]:
        """Receive a Wyoming protocol event (length-prefixed)."""
        try:
            # Read 4-byte header length
            len_bytes = await asyncio.wait_for(reader.readexactly(4), timeout=30.0)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            return None, None

        header_len = struct.unpack(">I", len_bytes)[0]

        if header_len == 0 or header_len > 1_000_000:
            log.warning(f"Invalid Wyoming header length: {header_len}")
            return None, None

        # Read JSON header
        header_bytes = await reader.readexactly(header_len)
        event = json.loads(header_bytes.decode("utf-8"))

        # Read binary payload if present
        payload = None
        payload_length = event.get("payload_length", 0)
        if payload_length > 0:
            payload = await reader.readexactly(payload_length)

        return event, payload

    @staticmethod
    def _wrap_wav(raw_audio: bytes, sample_rate: int, sample_width: int, channels: int) -> bytes:
        """Wrap raw PCM audio in a WAV container."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(sample_width)
            wf.setframerate(sample_rate)
            wf.writeframes(raw_audio)
        return buf.getvalue()

    @property
    def sample_rate(self) -> int:
        return self._sample_rate
