"""Speech-to-text using faster-whisper."""

import logging

import numpy as np

log = logging.getLogger("hal.transcriber")


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

    async def transcribe(self, audio: np.ndarray) -> str:
        """
        Transcribe a numpy float32 audio array.

        Returns the transcribed text.
        """
        if self.model is None:
            return ""

        if len(audio) < 1600:  # Less than 0.1s at 16kHz
            return ""

        segments, info = self.model.transcribe(
            audio,
            beam_size=5,
            language="en",
            vad_filter=True,
            vad_parameters=dict(
                min_silence_duration_ms=300,
                speech_pad_ms=200,
            ),
        )

        text_parts = []
        for segment in segments:
            text_parts.append(segment.text)

        return " ".join(text_parts).strip()
