"""Voice notes: satellites record → server transcribes → stored next to the
conversation log (``voice_notes`` table, see conversation_log.py) → browsed in
the Notes view and exposed to agents over MCP.

Routes:
  POST   /api/satellite/note   raw mono PCM (f32le default, or s16le), possibly
                               chunked; transcribed and stored.
  GET    /api/notes            keyset page, ASC within the page (like the log).
  DELETE /api/notes/{id}       204 / 404.

Long audio: the remote STT service is built for utterances (the client has a
15 s request timeout and whisper-class models see ≤30 s), so a note is split
into ≤WINDOW_S windows, each cut at the quietest 20 ms frame within the last
SEARCH_S seconds of the window (so words are rarely split), transcribed in
order, and the texts joined.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

log = logging.getLogger("hal.notes")
router = APIRouter()

TARGET_SR = 16000
MAX_BODY_BYTES = 64 * 1024 * 1024
# ~10 min notes; a little slack so a client that stops at exactly 10:00 (plus
# encoder/stream jitter) is never rejected.
MAX_DURATION_S = 630.0
WINDOW_S = 28.0
SEARCH_S = 4.0
FRAME_MS = 20
SAMPLE_FORMATS = {"f32le": 4, "s16le": 2}
# A window whose loudest 20 ms frame is below this RMS is digital silence:
# skip the STT round-trip (whisper-class models hallucinate on silence).
_SILENCE_RMS = 1e-4


class _TooLarge(Exception):
    pass


# --- pure helpers (unit-tested) ----------------------------------------------

def decode_pcm(body: bytes, fmt: str):
    """Raw little-endian mono PCM → float32 numpy array in [-1, 1]. Trailing
    partial samples are dropped."""
    import numpy as np
    width = SAMPLE_FORMATS[fmt]
    usable = len(body) - (len(body) % width)
    if fmt == "s16le":
        return np.frombuffer(body[:usable], dtype="<i2").astype(np.float32) / 32768.0
    return np.frombuffer(body[:usable], dtype="<f4").astype(np.float32)


def prepare_audio(body: bytes, fmt: str, sr: int):
    """Decode + resample to 16 kHz (CPU-bound; call via asyncio.to_thread)."""
    audio = decode_pcm(body, fmt)
    if audio.size and sr != TARGET_SR:
        import librosa
        audio = librosa.resample(audio, orig_sr=sr, target_sr=TARGET_SR)
    return audio


def split_windows(audio, sr: int = TARGET_SR, window_s: float = WINDOW_S,
                  search_s: float = SEARCH_S, frame_ms: int = FRAME_MS) -> list[tuple[int, int]]:
    """[(start, end)] sample ranges covering `audio`, each ≤ window_s long.
    Every cut but the last lands in the middle of the quietest frame_ms frame
    within the final search_s seconds of its window."""
    import numpy as np
    n = len(audio)
    win = int(window_s * sr)
    if n <= win:
        return [(0, n)] if n else []
    search = max(1, int(search_s * sr))
    frame = max(1, int(sr * frame_ms / 1000))
    out: list[tuple[int, int]] = []
    start = 0
    while n - start > win:
        hard_end = start + win
        lo = max(start + 1, hard_end - search)
        region = audio[lo:hard_end]
        nframes = len(region) // frame
        if nframes >= 1:
            frames = region[: nframes * frame].reshape(nframes, frame)
            energy = np.mean(frames.astype(np.float64) ** 2, axis=1)
            q = int(np.argmin(energy))
            cut = lo + q * frame + frame // 2
        else:
            cut = hard_end
        out.append((start, cut))
        start = cut
    out.append((start, n))
    return out


def _is_silent(chunk, sr: int = TARGET_SR) -> bool:
    import numpy as np
    if len(chunk) == 0:
        return True
    frame = max(1, int(sr * FRAME_MS / 1000))
    nframes = len(chunk) // frame
    if nframes == 0:
        return float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2))) < _SILENCE_RMS
    frames = chunk[: nframes * frame].reshape(nframes, frame).astype(np.float64)
    return float(np.sqrt(np.max(np.mean(frames ** 2, axis=1)))) < _SILENCE_RMS


async def transcribe_long(transcriber, audio, sr: int = TARGET_SR) -> str:
    """Transcribe arbitrarily long 16 kHz audio window by window, in order,
    and join the non-empty texts with single spaces."""
    windows = await asyncio.to_thread(split_windows, audio, sr)
    parts: list[str] = []
    for i, (a, b) in enumerate(windows):
        chunk = audio[a:b]
        if _is_silent(chunk, sr):
            continue
        try:
            text = await transcriber.transcribe(chunk)
        except Exception:
            log.warning(f"[notes] window {i + 1}/{len(windows)} transcription failed", exc_info=True)
            text = ""
        text = (text or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts).strip()


async def _read_body(request: Request, cap: int) -> bytes:
    """Read a (possibly chunked) body incrementally; _TooLarge past `cap`."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap:
        raise _TooLarge()
    buf = bytearray()
    async for chunk in request.stream():
        if chunk:
            buf.extend(chunk)
            if len(buf) > cap:
                raise _TooLarge()
    return bytes(buf)


# --- broadcast -----------------------------------------------------------------

async def broadcast_note_added(state, note: dict) -> None:
    """New note → every display surface: web mirrors + satellites
    (broadcast_force_action) AND the hub RPi (audio_websocket; the
    audio_streamer forward-allowlists `note_added`)."""
    from .main import _push_to_rpi, broadcast_force_action
    msg = {"type": "note_added", "note": note}
    try:
        await broadcast_force_action(state, msg)
    except Exception:
        log.debug("[notes] note_added broadcast failed", exc_info=True)
    await _push_to_rpi(state, msg)


def _notes_store(state):
    clog = getattr(state, "conversation_log", None)
    if clog is None or not getattr(clog, "enabled", False):
        return None
    return clog


# --- routes ----------------------------------------------------------------------

@router.post("/api/satellite/note")
async def post_satellite_note(request: Request):
    """Record a voice note: raw mono PCM body (X-Sample-Rate 8000..48000,
    X-Sample-Format f32le|s16le), transcribed and stored. Auth mirrors
    /api/satellite/stt: a paired Bearer token whose scope grants `notes`
    (full tokens; the watch scope is denied)."""
    from .main import _get_state
    from .pairing import demo_mode, extract_bearer
    state = _get_state(request.app)
    token = extract_bearer(request)
    if not (token and state.pairing and state.pairing.is_valid_token(token)):
        return Response(status_code=401)
    if not state.pairing.scope_allows(token, "notes"):
        return Response(status_code=403)
    if demo_mode():
        return JSONResponse({"error": "notes_disabled_in_demo"}, status_code=403)

    try:
        sr = int(request.headers.get("x-sample-rate", str(TARGET_SR)))
    except (TypeError, ValueError):
        return JSONResponse({"error": "bad_sample_rate"}, status_code=400)
    if not 8000 <= sr <= 48000:
        return JSONResponse({"error": "bad_sample_rate"}, status_code=400)
    fmt = (request.headers.get("x-sample-format") or "f32le").strip().lower()
    if fmt not in SAMPLE_FORMATS:
        return JSONResponse({"error": "bad_sample_format"}, status_code=400)

    try:
        body = await _read_body(request, MAX_BODY_BYTES)
    except _TooLarge:
        return JSONResponse({"error": "too_large"}, status_code=413)
    duration_s = (len(body) // SAMPLE_FORMATS[fmt]) / float(sr)
    if duration_s <= 0:
        return JSONResponse({"error": "empty_body"}, status_code=400)
    if duration_s > MAX_DURATION_S:
        return JSONResponse({"error": "too_long", "max_duration_s": MAX_DURATION_S},
                            status_code=413)

    transcriber = getattr(getattr(state, "pipeline", None), "transcriber", None)
    if transcriber is None:
        return JSONResponse({"error": "stt_unavailable"}, status_code=503)

    try:
        audio = await asyncio.to_thread(prepare_audio, body, fmt, sr)
        del body
        text = await transcribe_long(transcriber, audio)
    except Exception:
        log.warning("[notes] transcription failed", exc_info=True)
        return JSONResponse({"error": "stt_failed"}, status_code=502)
    if not text:
        return JSONResponse({"error": "no_speech"}, status_code=422)

    origin = state.pairing.device_name(token)
    duration_s = round(duration_s, 2)
    store = _notes_store(state)
    note = await store.add_note(text, origin=origin, duration_s=duration_s) if store else None
    log.info(f"[notes] {duration_s:.1f}s note from {origin or '?'}: "
             f"{len(text)} chars, stored={note is not None}")
    if note is None:
        # Never lose a transcription: hand the text back even when it
        # couldn't be persisted.
        return {
            "id": None,
            "ts": datetime.now(timezone.utc).isoformat(),
            "text": text,
            "origin": origin,
            "duration_s": duration_s,
            "stored": False,
        }
    await broadcast_note_added(state, note)
    return {**note, "stored": True}


@router.get("/api/notes")
async def get_notes(request: Request):
    """One page of voice notes, oldest→newest. ?limit=100 (1..500),
    ?before_id=N for older pages, ?q= case-insensitive text search. LAN-open
    like /api/conversation/log; a valid token without the `notes` scope → 403."""
    from .main import _get_state
    from .pairing import demo_mode
    from .routes_http import _scope_forbids
    state = _get_state(request.app)
    if _scope_forbids(state, request, "notes"):
        return Response(status_code=403)
    if demo_mode():
        return {"rows": [], "has_more": False}
    store = _notes_store(state)
    if store is None:
        return {"rows": [], "has_more": False, "disabled": True}
    try:
        limit = int(request.query_params.get("limit", "100"))
    except ValueError:
        limit = 100
    before_raw = request.query_params.get("before_id")
    try:
        before_id = int(before_raw) if before_raw else None
    except ValueError:
        before_id = None
    q = request.query_params.get("q") or None
    try:
        return await store.fetch_notes(limit=limit, before_id=before_id, q=q)
    except Exception as e:
        log.warning(f"notes fetch failed: {type(e).__name__}: {e}")
        return JSONResponse({"error": "notes_unavailable", "rows": []}, status_code=503)


@router.delete("/api/notes/{note_id}")
async def delete_note(note_id: int, request: Request):
    """Delete one note → 204, or 404 if it doesn't exist. LAN-open like the
    log (HAL_REQUIRE_TOKEN on ⇒ a valid token is required); a valid token
    without the `notes` scope → 403."""
    from .main import _get_state
    from .pairing import demo_mode, extract_bearer, require_token_enabled
    from .routes_http import _scope_forbids
    state = _get_state(request.app)
    if _scope_forbids(state, request, "notes"):
        return Response(status_code=403)
    if require_token_enabled():
        token = extract_bearer(request)
        if not (token and state.pairing and state.pairing.is_valid_token(token)):
            return Response(status_code=401)
    if demo_mode():
        return Response(status_code=403)
    store = _notes_store(state)
    if store is None:
        return Response(status_code=404)
    try:
        removed = await store.delete_note(note_id)
    except Exception as e:
        log.warning(f"note delete failed: {type(e).__name__}: {e}")
        return Response(status_code=503)
    return Response(status_code=204 if removed else 404)


# --- shared by the local tools / MCP ------------------------------------------------

def format_note_line(n: dict) -> str:
    ts = n.get("ts") or "?"
    try:
        ts = datetime.fromisoformat(ts).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        pass
    dev = n.get("origin") or "unknown device"
    return f"#{n['id']} [{ts}, {dev}] {n.get('text', '')}"
