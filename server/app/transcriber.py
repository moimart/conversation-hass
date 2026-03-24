"""Speech-to-text engine with runtime selection between faster-whisper and Nemotron."""

import asyncio
import logging
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor

import numpy as np

log = logging.getLogger("hal.transcriber")

_executor = ThreadPoolExecutor(max_workers=1)


class BaseTranscriber(ABC):
    """Abstract base for STT engines."""

    @abstractmethod
    async def initialize(self):
        pass

    @abstractmethod
    def _transcribe_sync(self, audio: np.ndarray) -> str:
        pass

    async def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe a numpy float32 audio array with timeout."""
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


class WhisperTranscriber(BaseTranscriber):
    """faster-whisper STT engine."""

    def __init__(self, model_size: str = "large-v3-turbo"):
        self.model_size = model_size
        self.model = None

    async def initialize(self):
        from faster_whisper import WhisperModel

        log.info(f"Loading faster-whisper model: {self.model_size}")
        try:
            self.model = WhisperModel(
                self.model_size,
                device="cuda",
                compute_type="float16",
            )
            log.info("faster-whisper using CUDA")
        except Exception:
            self.model = WhisperModel(
                self.model_size,
                device="cpu",
                compute_type="int8",
            )
            log.info("faster-whisper using CPU")

    def _transcribe_sync(self, audio: np.ndarray) -> str:
        if self.model is None:
            return ""

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
            if segment.no_speech_prob > 0.6:
                continue
            text_parts.append(segment.text)

        return " ".join(text_parts).strip()


class NemotronTranscriber(BaseTranscriber):
    """NVIDIA Nemotron Speech ASR engine (via NeMo toolkit)."""

    def __init__(self, model_name: str = "nvidia/parakeet-tdt-0.6b-v2"):
        self.model_name = model_name
        self.model = None

    async def initialize(self):
        import nemo.collections.asr as nemo_asr

        log.info(f"Loading Nemotron/NeMo ASR model: {self.model_name}")
        self.model = nemo_asr.models.ASRModel.from_pretrained(model_name=self.model_name)
        self.model.eval()
        log.info("Nemotron ASR ready")

    def _transcribe_sync(self, audio: np.ndarray) -> str:
        if self.model is None:
            return ""

        # NeMo expects 16kHz mono float32
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)

        results = self.model.transcribe([audio], batch_size=1)

        # Results can be a list of strings or a list of Hypothesis objects
        if not results:
            return ""

        if isinstance(results[0], str):
            return results[0].strip()

        # Hypothesis object
        if hasattr(results[0], "text"):
            return results[0].text.strip()

        return str(results[0]).strip()


def create_transcriber(engine: str, model: str = "") -> BaseTranscriber:
    """
    Factory: create a transcriber by engine name.

    engine: "whisper" or "nemotron"
    model:  model name/size (optional, uses sensible defaults)
    """
    engine = engine.lower().strip()

    if engine == "nemotron":
        model_name = model or "nvidia/parakeet-tdt-0.6b-v2"
        log.info(f"Using Nemotron ASR engine: {model_name}")
        return NemotronTranscriber(model_name=model_name)
    else:
        model_size = model or "large-v3-turbo"
        log.info(f"Using Whisper ASR engine: {model_size}")
        return WhisperTranscriber(model_size=model_size)
