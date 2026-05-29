"""Speech-to-text engine with runtime selection between faster-whisper and Nemotron.

Uses a self-healing thread pool: when a transcription times out, the stuck
thread can't be killed (Python limitation), so instead we replace the entire
executor and track consecutive failures to trigger a full model reload.
"""

import asyncio
import logging
import os
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor

import httpx
import numpy as np

log = logging.getLogger("hal.transcriber")


class BaseTranscriber(ABC):
    """Abstract base for STT engines."""

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._consecutive_failures = 0
        self._max_failures = 3  # reload model after 3 consecutive failures

    @abstractmethod
    async def initialize(self):
        pass

    @abstractmethod
    def _transcribe_sync(self, audio: np.ndarray) -> str:
        pass

    async def warm_up(self) -> None:
        """Run one dummy inference so the model's CUDA kernels are
        JIT-compiled and cuDNN auto-tuned BEFORE the first real user
        request. Without this, the first transcribe call after a server
        restart pays a 1-2 s setup tax. We pay it here at startup
        instead, where nobody is waiting."""
        log.info(f"{self.__class__.__name__}: warming up model")
        silence = np.zeros(16000, dtype=np.float32)  # 1 s of silence at 16 kHz
        try:
            loop = asyncio.get_event_loop()
            await asyncio.wait_for(
                loop.run_in_executor(self._executor, self._transcribe_sync, silence),
                timeout=60.0,
            )
            log.info(f"{self.__class__.__name__}: warm-up complete")
        except Exception as e:
            log.warning(f"{self.__class__.__name__}: warm-up failed (non-fatal): {e}")

    def _reload_model(self):
        """Override in subclasses to reload the STT model."""
        pass

    def _replace_executor(self):
        """Create a fresh thread pool, abandoning any stuck threads."""
        log.warning("Replacing thread pool executor (old worker may be stuck)")
        old = self._executor
        self._executor = ThreadPoolExecutor(max_workers=1)
        # Don't wait for old executor — its thread may be stuck forever
        # Python will clean it up when the threads eventually die
        try:
            old.shutdown(wait=False)
        except Exception:
            pass

    async def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe a numpy float32 audio array with timeout and self-healing."""
        if len(audio) < 1600:  # Less than 0.1s at 16kHz
            return ""

        loop = asyncio.get_event_loop()
        try:
            text = await asyncio.wait_for(
                loop.run_in_executor(self._executor, self._transcribe_sync, audio),
                timeout=15.0,
            )
            if text:
                self._consecutive_failures = 0
            return text
        except asyncio.TimeoutError:
            self._consecutive_failures += 1
            log.warning(f"Transcription timed out (15s), consecutive failures: {self._consecutive_failures}")

            # Replace the executor since the old thread is stuck
            self._replace_executor()

            # After repeated failures, reload the model entirely
            if self._consecutive_failures >= self._max_failures:
                log.error(f"Transcription failed {self._consecutive_failures} times, reloading model")
                try:
                    self._reload_model()
                    self._consecutive_failures = 0
                    log.info("Model reloaded successfully")
                except Exception as e:
                    log.error(f"Model reload failed: {e}")

            return ""
        except Exception as e:
            self._consecutive_failures += 1
            log.error(f"Transcription error: {e}")
            if self._consecutive_failures >= self._max_failures:
                self._replace_executor()
                try:
                    self._reload_model()
                    self._consecutive_failures = 0
                except Exception as re:
                    log.error(f"Model reload failed: {re}")
            return ""


class WhisperTranscriber(BaseTranscriber):
    """faster-whisper STT engine."""

    def __init__(self, model_size: str = "large-v3-turbo"):
        super().__init__()
        self.model_size = model_size
        self.model = None

    async def initialize(self):
        self._load_model()

    def _load_model(self):
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

    def _reload_model(self):
        log.info("Reloading faster-whisper model")
        import torch
        # Free old model
        self.model = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        self._load_model()

    def _transcribe_sync(self, audio: np.ndarray) -> str:
        if self.model is None:
            return ""

        import torch

        segments, info = self.model.transcribe(
            audio,
            beam_size=1,
            language="en",
            vad_filter=False,  # Our pipeline already runs Silero VAD upstream
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            condition_on_previous_text=False,
        )

        text_parts = []
        for segment in segments:
            if segment.no_speech_prob > 0.6:
                continue
            text_parts.append(segment.text)

        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        return " ".join(text_parts).strip()


class NemotronTranscriber(BaseTranscriber):
    """NVIDIA Nemotron Speech ASR engine (via NeMo toolkit)."""

    def __init__(self, model_name: str = "nvidia/parakeet-tdt-0.6b-v2"):
        super().__init__()
        self.model_name = model_name
        self.model = None

    async def initialize(self):
        self._load_model()

    def _load_model(self):
        import nemo.collections.asr as nemo_asr

        log.info(f"Loading Nemotron/NeMo ASR model: {self.model_name}")
        self.model = nemo_asr.models.ASRModel.from_pretrained(model_name=self.model_name)
        self.model.eval()
        log.info("Nemotron ASR ready")

    def _reload_model(self):
        log.info("Reloading Nemotron model")
        import torch
        self.model = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        self._load_model()

    def _transcribe_sync(self, audio: np.ndarray) -> str:
        if self.model is None:
            return ""

        import torch

        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)

        with torch.no_grad(), torch.inference_mode():
            results = self.model.transcribe([audio], batch_size=1, verbose=False)

        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        if not results:
            return ""

        if isinstance(results[0], str):
            return results[0].strip()

        if hasattr(results[0], "text"):
            return results[0].text.strip()

        return str(results[0]).strip()


class RemoteTranscriber(BaseTranscriber):
    """STT via a remote transcribe(audio)->text HTTP microservice.

    The local audio pipeline keeps VAD, buffering, and the partial/final
    cadence; this only ships the float32 buffer to the STT container and
    returns the text. It overrides the async network path instead of the
    thread-pool _transcribe_sync path.

    Discipline: this must NEVER raise into the pipeline. On any timeout or
    connection failure it returns "" so transcription degrades to "no
    transcript" rather than wedging process_chunk.
    """

    def __init__(self, url: str):
        super().__init__()
        self.url = url.rstrip("/")
        self._client: "httpx.AsyncClient | None" = None

    async def initialize(self):
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=2.0),
                limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=60.0),
            )
        log.info(f"RemoteTranscriber → {self.url}")

    def _transcribe_sync(self, audio: np.ndarray) -> str:
        # Unused: RemoteTranscriber overrides transcribe() with the network path.
        raise NotImplementedError("RemoteTranscriber uses the async network path")

    def _reload_model(self):
        # The remote service owns its own model lifecycle/reloads.
        pass

    async def warm_up(self) -> None:
        # Wait for the remote model to be ready instead of a local dummy
        # inference. Non-fatal: log and continue if unreachable so the AI
        # server still starts (transcription returns "" until STT is up).
        if self._client is None:
            await self.initialize()
        for _ in range(30):  # ~30s grace for remote model load + warm-up
            try:
                r = await self._client.get(f"{self.url}/health")
                if r.status_code == 200:
                    log.info("RemoteTranscriber: STT service healthy")
                    return
            except Exception:
                pass
            await asyncio.sleep(1.0)
        log.warning("RemoteTranscriber: STT service not healthy after 30s (continuing)")

    async def transcribe(self, audio: np.ndarray) -> str:
        if len(audio) < 1600:  # < 0.1s at 16kHz — parity with the base class
            return ""
        if self._client is None:
            await self.initialize()
        body = audio.astype(np.float32).tobytes()
        headers = {
            "X-Sample-Rate": "16000",
            "Content-Type": "application/octet-stream",
        }
        for attempt in range(2):  # one retry, on connect/read errors only
            try:
                r = await self._client.post(
                    f"{self.url}/transcribe", content=body, headers=headers
                )
                r.raise_for_status()
                return (r.json().get("text") or "").strip()
            except (httpx.ConnectError, httpx.ReadError) as e:
                if attempt == 0:
                    continue
                log.warning(f"RemoteTranscriber connect/read failed: {e}")
                return ""
            except Exception as e:
                log.warning(f"RemoteTranscriber error: {e}")
                return ""
        return ""


def create_transcriber(engine: str, model: str = "") -> BaseTranscriber:
    """
    Factory: create a transcriber by engine name.

    engine: "whisper", "nemotron", or "remote"
    model:  model name/size (optional, uses sensible defaults)
    """
    engine = engine.lower().strip()

    if engine == "remote":
        url = os.environ.get("STT_REMOTE_URL", "http://stt-service:8770")
        log.info(f"Using remote STT engine → {url}")
        return RemoteTranscriber(url=url)
    elif engine == "nemotron":
        model_name = model or "nvidia/parakeet-tdt-0.6b-v2"
        log.info(f"Using Nemotron ASR engine: {model_name}")
        return NemotronTranscriber(model_name=model_name)
    else:
        model_size = model or "large-v3-turbo"
        log.info(f"Using Whisper ASR engine: {model_size}")
        return WhisperTranscriber(model_size=model_size)
