"""Audio processing pipeline: VAD, transcription, speaker filtering."""

import asyncio
import logging
import time

import numpy as np

from .transcriber import StreamingTranscriber
from .speaker_filter import SpeakerFilter

log = logging.getLogger("hal.pipeline")

# Silence threshold: seconds of silence after speech to trigger command processing
SILENCE_THRESHOLD = 1.5


class AudioPipeline:
    """Processes raw audio: VAD → transcription → speaker filtering."""

    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self.transcriber: StreamingTranscriber | None = None
        self.speaker_filter: SpeakerFilter | None = None

        # VAD state
        self._vad_model = None
        self._speech_active = False
        self._silence_start: float | None = None
        self.silence_detected = False

        # Echo suppression: when AI is speaking, suppress transcription
        self._ai_speaking = False

        # Audio buffer for current speech segment
        self._audio_buffer = bytearray()
        self._partial_buffer = bytearray()
        self._partial_interval = 0.5  # seconds between partial transcriptions
        self._last_partial_time = 0.0

    async def initialize(self):
        """Load models."""
        log.info("Loading VAD model...")
        import torch
        self._vad_model, self._vad_utils = torch.hub.load(
            repo_or_dir="snakers4/silero-vad",
            model="silero_vad",
            force_reload=False,
            onnx=True,
        )

        log.info("Loading transcription model...")
        self.transcriber = StreamingTranscriber(model_size="base")
        await self.transcriber.initialize()

        log.info("Initializing speaker filter...")
        self.speaker_filter = SpeakerFilter()

        log.info("Audio pipeline ready.")

    def set_ai_speaking(self, speaking: bool):
        """Mark whether the AI is currently speaking (for echo suppression)."""
        self._ai_speaking = speaking
        if speaking:
            self._speech_active = False
            self._audio_buffer.clear()
            self._partial_buffer.clear()

    async def process_chunk(self, audio_bytes: bytes) -> dict | None:
        """
        Process a chunk of raw PCM 16-bit LE audio.

        Returns a transcription result dict or None.
        """
        if self._ai_speaking:
            return None

        # Convert bytes to float32 numpy array
        audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)
        audio_float = audio_int16.astype(np.float32) / 32768.0

        # Run VAD
        import torch
        chunk_tensor = torch.from_numpy(audio_float)

        # Silero VAD expects chunks of specific sizes (512 for 16kHz)
        vad_chunk_size = 512
        is_speech = False
        for i in range(0, len(chunk_tensor), vad_chunk_size):
            segment = chunk_tensor[i : i + vad_chunk_size]
            if len(segment) < vad_chunk_size:
                segment = torch.nn.functional.pad(segment, (0, vad_chunk_size - len(segment)))
            confidence = self._vad_model(segment, self.sample_rate).item()
            if confidence > 0.5:
                is_speech = True
                break

        now = time.monotonic()

        if is_speech:
            self._speech_active = True
            self._silence_start = None
            self.silence_detected = False
            self._audio_buffer.extend(audio_bytes)
            self._partial_buffer.extend(audio_bytes)

            # Emit partial transcriptions periodically
            if now - self._last_partial_time >= self._partial_interval and len(self._partial_buffer) > 0:
                self._last_partial_time = now
                partial_audio = np.frombuffer(bytes(self._partial_buffer), dtype=np.int16).astype(np.float32) / 32768.0
                text = await self.transcriber.transcribe(partial_audio)
                if text and text.strip():
                    return {"text": text.strip(), "is_partial": True, "speaker": "human"}
        else:
            if self._speech_active:
                # Speech just ended
                if self._silence_start is None:
                    self._silence_start = now
                elif now - self._silence_start >= SILENCE_THRESHOLD:
                    # Enough silence — finalize transcription
                    self._speech_active = False

                    if len(self._audio_buffer) > 0:
                        full_audio = np.frombuffer(bytes(self._audio_buffer), dtype=np.int16).astype(np.float32) / 32768.0
                        text = await self.transcriber.transcribe(full_audio)

                        self._audio_buffer.clear()
                        self._partial_buffer.clear()
                        self._silence_start = None

                        if text and text.strip():
                            # Speaker identification
                            speaker = self.speaker_filter.identify(full_audio, self.sample_rate)
                            self.silence_detected = True
                            return {"text": text.strip(), "is_partial": False, "speaker": speaker}

        return None
