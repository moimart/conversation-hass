"""STT client for the HAL AI server — talks to the standalone stt-service.

The heavy ASR engines (faster-whisper / NeMo, torch, CUDA, model weights) live
in stt-service/engines.py and run in their own container. The AI server keeps
only the lightweight local audio front-end (VAD, buffering, the partial/final
cadence) and ships the float32 buffer to the STT service over HTTP via
RemoteTranscriber. This module imports NO ML libraries — just httpx + numpy —
which is what lets the server image stay slim (CPU-only, no CUDA base).

Discipline: RemoteTranscriber.transcribe must NEVER raise into the pipeline. On
any timeout or connection failure it returns "" so transcription degrades to
"no transcript" rather than wedging process_chunk.
"""

import asyncio
import logging
import os
from abc import ABC, abstractmethod

import httpx
import numpy as np

log = logging.getLogger("hal.transcriber")


class BaseTranscriber(ABC):
    """Minimal STT interface the audio pipeline depends on.

    The pipeline holds a `BaseTranscriber` and calls `initialize`, `warm_up`,
    `transcribe`, and (on repeated failures) `_reload_model` /
    `_consecutive_failures`. The only concrete engine on the server is
    RemoteTranscriber; the heavy local engines live in stt-service/engines.py.
    """

    def __init__(self):
        # Tracked by the pipeline's failure/reload bookkeeping.
        self._consecutive_failures = 0

    @abstractmethod
    async def initialize(self) -> None:
        ...

    @abstractmethod
    async def transcribe(self, audio: np.ndarray) -> str:
        ...

    async def warm_up(self) -> None:
        """Optional startup warm-up. No-op by default."""
        return None

    def _reload_model(self) -> None:
        """Optional model reload hook. No-op for the remote engine — the
        stt-service owns its own model lifecycle."""
        return None


class RemoteTranscriber(BaseTranscriber):
    """STT via a remote transcribe(audio)->text HTTP microservice.

    Holds one persistent keep-alive httpx client; each partial/final is a
    single small POST of the raw float32 16 kHz buffer.
    """

    def __init__(self, url: str):
        super().__init__()
        self.url = url.rstrip("/")
        self._client: "httpx.AsyncClient | None" = None

    async def initialize(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=2.0),
                limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=60.0),
            )
        log.info(f"RemoteTranscriber → {self.url}")

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
        if len(audio) < 1600:  # < 0.1s at 16kHz — skip the round-trip
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
    """Factory for the AI server. Only the remote engine is available here —
    the heavy local engines (whisper/nemotron) live in the stt-service. Any
    other engine name falls back to remote with a warning."""
    engine = (engine or "").lower().strip()
    if engine and engine != "remote":
        log.warning(
            f"STT engine {engine!r} is not available on the AI server "
            f"(faster-whisper/NeMo live in stt-service); using the remote engine."
        )
    url = os.environ.get("STT_REMOTE_URL", "http://stt-service:8770")
    log.info(f"Using remote STT engine → {url}")
    return RemoteTranscriber(url=url)
