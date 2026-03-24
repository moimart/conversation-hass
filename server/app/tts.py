"""Text-to-speech via Wyoming protocol.

Connects to an existing Wyoming-compatible TTS service over TCP and
streams back synthesized audio.

Wyoming protocol framing (v1.x):
  Each event is a JSON header line (newline-terminated) containing:
    - "type": event type
    - "data_length": number of bytes of JSON data following the header
    - "payload_length": number of bytes of binary payload after the data
  Followed by `data_length` bytes of JSON data, then `payload_length`
  bytes of binary payload.
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

    def __init__(self, host: str = "localhost", port: int = 10200, voice: str = ""):
        self.host = host
        self.port = port
        self.voice = voice.strip() if voice else ""
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
            synth_data = {"text": text}
            if self.voice:
                synth_data["voice"] = {"name": self.voice}
            await self._send_event(writer, "synthesize", synth_data)

            # Collect audio chunks
            audio_chunks: list[bytes] = []
            sample_rate = self._sample_rate
            sample_width = self._sample_width
            channels = self._channels

            while True:
                event_type, data, payload = await self._recv_event(reader)
                if event_type is None:
                    break

                log.debug(f"Wyoming event: {event_type}")

                if event_type == "audio-start":
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
                    error_msg = data.get("text", "Unknown error")
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
    async def _send_event(writer: asyncio.StreamWriter, event_type: str, data: dict | None = None, payload: bytes | None = None):
        """
        Send a Wyoming protocol event.

        Format: JSON header line + data bytes + payload bytes
        """
        data_bytes = b""
        if data:
            data_bytes = json.dumps(data).encode("utf-8")

        header = {
            "type": event_type,
            "data_length": len(data_bytes),
            "payload_length": len(payload) if payload else 0,
        }
        header_line = json.dumps(header) + "\n"
        log.info(f"Wyoming send: {header_line.strip()}")

        writer.write(header_line.encode("utf-8"))
        if data_bytes:
            writer.write(data_bytes)
        if payload:
            writer.write(payload)
        await writer.drain()

    @staticmethod
    async def _recv_event(reader: asyncio.StreamReader) -> tuple[str | None, dict, bytes | None]:
        """
        Receive a Wyoming protocol event.

        Returns (event_type, data_dict, payload_bytes) or (None, {}, None) on EOF/error.
        """
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=30.0)
        except asyncio.TimeoutError:
            log.warning("Wyoming TTS response timeout")
            return None, {}, None

        if not line:
            return None, {}, None

        try:
            header = json.loads(line.decode("utf-8").strip())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            log.warning(f"Cannot parse Wyoming header: {e}")
            return None, {}, None

        event_type = header.get("type", "")
        data_length = header.get("data_length", 0)
        payload_length = header.get("payload_length", 0)

        # Read JSON data
        data = {}
        if data_length > 0:
            data_bytes = await reader.readexactly(data_length)
            try:
                data = json.loads(data_bytes.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                log.warning(f"Cannot parse Wyoming data: {e}")

        # Read binary payload
        payload = None
        if payload_length > 0:
            payload = await reader.readexactly(payload_length)

        return event_type, data, payload

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
