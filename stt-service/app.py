"""STT microservice: a thin HTTP wrapper around the local ASR engines.

The faster-whisper / NeMo engines live next to this file in engines.py (owned
by this image — no cross-service COPY). The model is loaded and warmed at
startup and kept resident; the HAL AI server's RemoteTranscriber POSTs raw
float32 16kHz PCM to /transcribe and gets back the text. VAD, buffering, and the
partial/final cadence stay in the AI server — this service is stateless.
"""

import logging
import os
from contextlib import asynccontextmanager

import numpy as np
from fastapi import FastAPI, Request, Response

from engines import create_transcriber

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("stt-service")

# "remote" would point back at this service — fall back to a real local engine.
STT_ENGINE = os.environ.get("STT_ENGINE", "whisper")
if STT_ENGINE not in ("whisper", "nemotron"):
    STT_ENGINE = "whisper"
STT_MODEL = os.environ.get("STT_MODEL", "")

_transcriber = None
_ready = False


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _transcriber, _ready
    log.info(f"Loading STT engine '{STT_ENGINE}' (model={STT_MODEL or 'default'})...")
    _transcriber = create_transcriber(STT_ENGINE, STT_MODEL)
    await _transcriber.initialize()
    await _transcriber.warm_up()
    _ready = True
    log.info("STT service ready.")
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    if _ready:
        return {"status": "ok"}
    return Response(status_code=503)


@app.post("/transcribe")
async def transcribe(request: Request):
    if not _ready or _transcriber is None:
        return Response(status_code=503)
    body = await request.body()
    # Raw float32 little-endian, 16kHz mono — byte-identical to what the AI
    # server's pipeline already produces via _to_16k_float. Copy to get a
    # writable array (np.frombuffer is read-only).
    audio = np.frombuffer(body, dtype=np.float32).copy()
    text = await _transcriber.transcribe(audio)
    return {"text": text}
