"""Speech-to-text using faster-whisper."""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

import numpy as np

log = logging.getLogger("hal.transcriber")

_executor = ThreadPoolExecutor(max_workers=1)


class StreamingTranscriber:
    """Wraps faster-whisper for audio transcription."""

    def __init__(self, model_size: str = "base"):
        self.model_size = model_size
        self.model = None

    async def initialize(self):
        """Load the Whisper model."""
        from faster_whisper import WhisperModel

        log.info(f"Loading faster-whisper model: {self.model_size}")
        # Use GPU if available, otherwise CPU with int8 quantization
        try:
            self.model = WhisperModel(
                self.model_size,
                device="cuda",
                compute_type="float16",
            )
            log.info("Using CUDA for transcription")
        except Exception:
            self.model = WhisperModel(
                self.model_size,
                device="cpu",
                compute_type="int8",
            )
            log.info("Using CPU for transcription")

    def _transcribe_sync(self, audio: np.ndarray) -> str:
        """Synchronous transcription — runs in a thread pool."""
        segments, info = self.model.transcribe(
            audio,
            beam_size=1,
            language="en",
            vad_filter=True,
            vad_parameters=dict(
                min_silence_duration_ms=300,
                speech_pad_ms=200,
            ),
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            condition_on_previous_text=False,
        )

        text_parts = []
        for segment in segments:
            # Skip hallucinated/low-confidence segments
            if segment.no_speech_prob > 0.6:
                continue
            text_parts.append(segment.text)

        return " ".join(text_parts).strip()

    async def transcribe(self, audio: np.ndarray) -> str:
        """
        Transcribe a numpy float32 audio array.

        Returns the transcribed text.
        """
        if self.model is None:
            return ""

        if len(audio) < 1600:  # Less than 0.1s at 16kHz
            return ""

        loop = asyncio.get_event_loop()
        try:
            text = await asyncio.wait_for(
                loop.run_in_executor(_executor, self._transcribe_sync, audio),
                timeout=15.0,
            )
            return text
        except asyncio.TimeoutError:
            log.warning("Transcription timed out (15s), skipping segment")
            return ""
        except Exception as e:
            log.error(f"Transcription error: {e}")
            return ""
