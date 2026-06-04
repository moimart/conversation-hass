"""REST route handlers split out of main via APIRouter."""
from __future__ import annotations

import asyncio
import io
import logging
import os
import time

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

log = logging.getLogger("hal")
router = APIRouter()


class CommandRequest(BaseModel):
    text: str


class DisplayRequest(BaseModel):
    # "on", "off", or "toggle". Anything else → 422.
    state: str


class PhotoFrameStartRequest(BaseModel):
    entity_id: str = ""


class PhotoFrameIdleRequest(BaseModel):
    # Minutes of inactivity before the photo frame auto-activates.
    # 0 disables the feature. Anything else clamped to 0..720.
    minutes: int


class VolumeRequest(BaseModel):
    direction: str  # "up" or "down"
    step: float = 0.1


class SpeakRequest(BaseModel):
    text: str


class CloudLLMRequest(BaseModel):
    # All optional: set the override switch, pick a model, or refresh the
    # provider/model list. Keys are configured server-side only
    # (runtime/cloud_providers.json) and are NEVER accepted or returned here.
    enabled: bool | None = None
    model: str | None = None
    refresh: bool = False


@router.get("/health")
async def health(request: Request):
    from .main import _get_state
    state = _get_state(request.app)
    return {
        "status": "ok",
        "pipeline_ready": state.pipeline is not None,
        "mcp_connected": state.mcp_client is not None and len(state.mcp_client.tool_names) > 0,
        "tts_available": state.tts_engine is not None,
        "memory_available": state.memory_client is not None and state.memory_client.available,
        "openclaw_enabled": state.openclaw_enabled,
    }


@router.post("/api/ptt/start")
async def post_ptt_start(request: Request):
    """Open a Push-to-Talk session. See server/app/ptt.py for semantics."""
    from .main import _get_state
    from .ptt import start_ptt
    state = _get_state(request.app)
    return await start_ptt(state)


@router.post("/api/ptt/end")
async def post_ptt_end(request: Request):
    """Close the active PTT session and run the LLM on whatever was captured."""
    from .main import _get_state
    from .ptt import end_ptt
    state = _get_state(request.app)
    return await end_ptt(state)


@router.post("/api/ptt/cancel")
async def post_ptt_cancel(request: Request):
    """Close the active PTT session and DISCARD whatever was captured. Used
    when the trigger party knows the press was an accident."""
    from .main import _get_state
    from .ptt import end_ptt
    state = _get_state(request.app)
    return await end_ptt(state, cancel=True)


@router.get("/api/display")
async def get_display(request: Request):
    """Return the kiosk display power state and idle-blank timeout."""
    from .main import _get_state
    state = _get_state(request.app)
    return {
        "state": state.display_state,
        "auto_off_seconds": int(state.display_auto_off_seconds or 0),
        "available": bool(state.display_available),
    }


@router.post("/api/display")
async def post_display(request: Request, req: DisplayRequest):
    """Turn the kiosk display on or off (DPMS). 'toggle' flips current state."""
    from .main import _get_state, _set_display
    state = _get_state(request.app)
    target = (req.state or "").strip().lower()
    if target == "toggle":
        target = "off" if state.display_state == "on" else "on"
    if target not in ("on", "off"):
        return {"status": "error", "message": "state must be on|off|toggle"}
    if not state.display_available:
        return {"status": "unavailable", "message": "no display backend on the kiosk host"}
    pushed = await _set_display(state, target)
    return {
        "status": "ok" if pushed else "rpi_disconnected",
        "state": state.display_state,
    }


@router.post("/api/photo_frame/start")
async def post_photo_frame_start(request: Request, req: PhotoFrameStartRequest | None = None):
    """Open the photo frame on the kiosk. Optional body overrides the
    configured default: `{"entity_id": "image.weather_radar"}`."""
    from .main import _get_state
    from .photo_frame import start_photo_frame
    state = _get_state(request.app)
    entity_id = (req.entity_id if req else "") or ""
    return await start_photo_frame(state, entity_id=entity_id)


@router.post("/api/photo_frame/end")
async def post_photo_frame_end(request: Request):
    """Dismiss the active photo frame. No-op if none is open."""
    from .main import _get_state
    from .photo_frame import stop_photo_frame
    state = _get_state(request.app)
    return await stop_photo_frame(state, reason="explicit")


@router.get("/api/photo_frame/video")
async def get_photo_frame_video():
    """Serve the cached looping video. The RPi pulls this and stores it
    locally for the kiosk to play. 404 when no video is cached."""
    from fastapi.responses import FileResponse, Response
    from .main import _photo_frame_video_cache_path
    cache = _photo_frame_video_cache_path()
    if not os.path.isfile(cache):
        return Response(status_code=404)
    return FileResponse(cache, media_type="video/mp4")


@router.get("/api/photo_frame/idle")
async def get_photo_frame_idle(request: Request):
    """Return the photo-frame idle auto-activation setting (minutes)."""
    from .main import _get_state
    state = _get_state(request.app)
    return {
        "minutes": int(state.photo_frame_idle_minutes or 0),
        "active": (state.photo_frame_session is not None
                   or state.photo_frame_video_session is not None),
    }


@router.post("/api/photo_frame/idle")
async def post_photo_frame_idle(request: Request, req: PhotoFrameIdleRequest):
    """Set the photo-frame idle auto-activation threshold in minutes.
    0 disables the feature."""
    from .main import _get_state
    state = _get_state(request.app)
    try:
        n = int(req.minutes)
    except (TypeError, ValueError):
        n = 0
    n = max(0, min(720, n))
    cb = state.mqtt_bridge._config_callbacks.get("photo_frame_idle_minutes") if state.mqtt_bridge else None
    if cb is not None:
        await cb(n)
    else:
        state.photo_frame_idle_minutes = n
        state.user_last_activity = time.time()
        if state.runtime_config is not None:
            state.runtime_config.set("photo_frame_idle_minutes", n)
    return {"status": "ok", "minutes": int(state.photo_frame_idle_minutes or 0)}


@router.post("/api/command")
async def post_command(request: Request, req: CommandRequest):
    """
    Direct text command to the LLM, bypassing STT.

    The text is shown on the UI as a transcription and processed by
    the LLM without requiring a wake word. The TTS response plays
    on the RPi like any voice command.
    """
    from .main import (
        _get_state,
        broadcast_to_ui,
        send_to_device,
        _record_user_activity,
        _dismiss_photo_frame_async,
    )
    from .pairing import require_token_enabled, extract_bearer
    state = _get_state(request.app)

    token = extract_bearer(request)
    token_valid = bool(token) and state.pairing and state.pairing.is_valid_token(token)

    # Mobile auth gate (opt-in): when HAL_REQUIRE_TOKEN is set, a paired device
    # token is required. Default OFF leaves the existing LAN behaviour intact.
    if require_token_enabled() and not token_valid:
        return Response(
            content='{"status":"error","message":"unauthorized"}',
            status_code=401, media_type="application/json",
        )

    # Satellite origin: a command from a paired device that is CURRENTLY connected
    # as a satellite (/ws/ui) is routed only back to that device. A valid token
    # whose device isn't connected as a satellite falls back to global (None).
    origin = token if (token_valid and token in state.satellite_ws) else None

    conversation = state.conversation
    text = req.text.strip()

    if not text:
        return {"status": "error", "message": "Empty text"}

    if not conversation:
        return {"status": "error", "message": "Server not ready"}

    _record_user_activity(state)
    if origin is None:
        # Global command — dismiss the kiosk photo frame as before.
        _dismiss_photo_frame_async(state, reason="command")

    log.info(f"REST command received ({'satellite' if origin else 'global'}): '{text[:80]}'")

    # Echo as a transcription. Satellite-origin → only the originating device;
    # global → kiosk (RPi) + mirror UIs, exactly as before.
    transcription_msg = {
        "type": "transcription",
        "text": text,
        "is_partial": False,
        "speaker": "human",
    }
    if origin is not None:
        await send_to_device(state, origin, transcription_msg)
    else:
        await broadcast_to_ui(state, transcription_msg)
        if state.audio_websocket:
            try:
                await state.audio_websocket.send_json(transcription_msg)
            except Exception:
                pass

    # Feed directly to conversation manager (skip wake word). Tag the turn origin
    # so the response/state/TTS callbacks route to the same destination.
    conversation._pending_origin = origin
    conversation._command_buffer.append(text)
    conversation._wake_detected = True  # bypass wake word check in on_silence

    # Process immediately as a background task (like on_silence)
    asyncio.create_task(conversation.on_silence())

    return {"status": "ok", "message": "Command received"}


@router.get("/api/satellite/tts")
async def get_satellite_tts(request: Request):
    """Return the cached TTS audio (WAV) for the requesting satellite device.
    Token-scoped: the caller must be a paired token that is CURRENTLY connected as
    a satellite, and only ever receives its OWN cached audio. The phone fetches
    this after a `tts_play` message and plays it."""
    from .main import _get_state
    from .pairing import extract_bearer
    state = _get_state(request.app)
    token = extract_bearer(request)
    if not (token and state.pairing and state.pairing.is_valid_token(token)
            and token in state.satellite_ws):
        return Response(status_code=401)
    entry = state.satellite_tts.get(token)
    if not entry or (time.monotonic() - entry.get("ts", 0.0)) > 60.0:
        return Response(status_code=404)
    return Response(
        content=entry["audio"],
        media_type=entry.get("mime", "audio/wav"),
        headers={"Cache-Control": "no-store"},
    )


def _connected_satellite_token(state, request: Request) -> str | None:
    """The Bearer token IF it's a paired device currently connected as a
    satellite, else None (used to gate the satellite-only routes)."""
    from .pairing import extract_bearer
    token = extract_bearer(request)
    if (token and state.pairing and state.pairing.is_valid_token(token)
            and token in state.satellite_ws):
        return token
    return None


@router.get("/api/cloud_llm")
async def get_cloud_llm(request: Request):
    """Cloud override status. NEVER includes provider keys or endpoints."""
    from .main import _get_state
    state = _get_state(request.app)
    return {
        "enabled": bool(state.cloud_llm_enabled),
        "model": state.cloud_llm_model or "",
        "options": list(state.cloud_model_options or []),
    }


@router.post("/api/cloud_llm")
async def post_cloud_llm(request: Request, req: CloudLLMRequest):
    """Set the cloud override switch / model, or refresh the model list.
    Dispatches through the same MQTT config callbacks so HA stays in sync
    (mirrors the post_photo_frame_idle pattern)."""
    from .main import _get_state
    state = _get_state(request.app)
    callbacks = state.mqtt_bridge._config_callbacks if state.mqtt_bridge else {}
    if req.model is not None:
        cb = callbacks.get("cloud_llm_model")
        if cb is not None:
            await cb(req.model)
        else:
            state.cloud_llm_model = (req.model or "").strip()
    if req.enabled is not None:
        cb = callbacks.get("cloud_llm_enabled")
        if cb is not None:
            await cb("on" if req.enabled else "off")
        else:
            state.cloud_llm_enabled = bool(req.enabled)
    if req.refresh and state.cloud_llm_client is not None:
        try:
            opts = await state.cloud_llm_client.list_models()
            if opts:
                state.cloud_model_options = opts
                if state.mqtt_bridge:
                    state.mqtt_bridge.cloud_model_options = list(opts)
                    await state.mqtt_bridge.publish_discovery()
        except Exception as e:
            log.warning(f"cloud models refresh failed: {type(e).__name__}")
    return {
        "enabled": bool(state.cloud_llm_enabled),
        "model": state.cloud_llm_model or "",
        "options": list(state.cloud_model_options or []),
    }


def _stream_fallback_upstream(state) -> tuple[str, dict] | None:
    """Resolve the upstream MJPEG URL (+ headers) for the active live stream.

    Used by the satellite stream fallback: when a phone's WebRTC can't reach
    the camera's LAN ICE candidates (no VPN/subnet route), it falls back to
    MJPEG proxied through this server — the single-URL path.
      kind=ha   → HA's camera_proxy_stream (HA produces MJPEG for any camera)
      kind=rtsp → go2rtc's stream.mjpeg for the registered stream
    Returns None when there's no active stream or the upstream isn't configured.
    """
    sess = state.active_stream
    if not sess:
        return None
    kind = sess.get("kind")
    if kind == "ha":
        ha_url = os.environ.get("HA_URL", "").rstrip("/")
        ha_token = os.environ.get("HA_TOKEN", "")
        entity_id = sess.get("entity_id", "")
        if not (ha_url and ha_token and entity_id):
            return None
        return (
            f"{ha_url}/api/camera_proxy_stream/{entity_id}",
            {"Authorization": f"Bearer {ha_token}"},
        )
    if kind == "rtsp":
        g = os.environ.get("GO2RTC_URL", "").rstrip("/")
        name = sess.get("go2rtc_name", "")
        if not (g and name):
            return None
        return (f"{g}/api/stream.mjpeg?src={name}", {})
    return None


@router.get("/api/satellite/stream.mjpeg")
async def get_satellite_stream_mjpeg(request: Request):
    """MJPEG fallback for the active live stream, proxied through this server.

    A satellite whose WebRTC negotiation can't connect (remote phone without a
    route to the camera/go2rtc LAN addresses) loads this in an <img> instead —
    everything then flows through the one server URL. Token-gated like the other
    satellite routes; accepts ?token= because <img> can't send headers."""
    import httpx
    from fastapi.responses import StreamingResponse
    from .main import _get_state
    from .pairing import extract_bearer
    state = _get_state(request.app)
    token = request.query_params.get("token") or extract_bearer(request)
    if not (token and state.pairing and state.pairing.is_valid_token(token)
            and token in state.satellite_ws):
        return Response(status_code=401)
    upstream = _stream_fallback_upstream(state)
    if upstream is None:
        return Response(status_code=404)
    url, headers = upstream

    client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None))
    try:
        req = client.build_request("GET", url, headers=headers)
        resp = await client.send(req, stream=True)
    except Exception as e:
        await client.aclose()
        log.warning(f"stream.mjpeg upstream connect failed: {e}")
        return Response(status_code=502)
    if resp.status_code != 200:
        await resp.aclose()
        await client.aclose()
        return Response(status_code=502)

    media_type = resp.headers.get("content-type", "multipart/x-mixed-replace")

    async def gen():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        except Exception:
            pass  # phone went away / upstream ended — normal teardown
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        gen(), media_type=media_type, headers={"Cache-Control": "no-store"},
    )


@router.post("/api/satellite/photo_frame/start")
async def post_satellite_photo_start(request: Request, req: PhotoFrameStartRequest):
    """Start the requesting satellite phone's own ambient photo frame (targeted
    to it only). The phone calls this from its idle screensaver timer."""
    from .main import _get_state
    from .photo_frame import start_photo_frame_for_device
    state = _get_state(request.app)
    token = _connected_satellite_token(state, request)
    if token is None:
        return Response(status_code=401)
    return await start_photo_frame_for_device(state, token, entity_id=req.entity_id)


@router.post("/api/satellite/photo_frame/stop")
async def post_satellite_photo_stop(request: Request):
    """Stop the requesting satellite phone's photo frame."""
    from .main import _get_state
    from .photo_frame import stop_photo_frame_for_device
    state = _get_state(request.app)
    token = _connected_satellite_token(state, request)
    if token is None:
        return Response(status_code=401)
    return await stop_photo_frame_for_device(state, token)


@router.post("/api/openclaw/response")
async def post_openclaw_response(request: Request):
    """Callback endpoint for OpenClaw channel plugin to deliver agent responses."""
    from .main import _get_state
    state = _get_state(request.app)
    if not state.openclaw_client:
        return {"status": "error", "message": "OpenClaw client not configured"}

    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "message": "Invalid JSON"}

    request_id = body.get("request_id", "")
    if not request_id:
        return {"status": "error", "message": "request_id required"}

    state.openclaw_client.resolve_response(request_id, body)
    return {"status": "ok"}


@router.post("/api/openclaw/say")
async def post_openclaw_say(request: Request):
    """Proactive agent-initiated speech — no request_id, no pending turn.

    The OpenClaw channel's outbound path POSTs here when the agent wants
    to speak to the user unprompted (reminders, notifications). HAL
    synthesizes the text and plays it through the kiosk immediately.
    """
    from .main import _get_state, _speak_proactively
    state = _get_state(request.app)
    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "message": "Invalid JSON"}

    text = str(body.get("text", "") or "").strip()
    media_urls = body.get("media_urls") or []
    if not isinstance(media_urls, list):
        media_urls = []
    if not text and not media_urls:
        return {"status": "error", "message": "text or media_urls required"}

    await _speak_proactively(state, text, media_urls)
    return {"status": "ok"}


@router.post("/api/volume")
async def post_volume(request: Request, req: VolumeRequest):
    """Adjust RPi volume up or down."""
    from .main import _get_state
    state = _get_state(request.app)
    ws = state.audio_websocket
    if not ws:
        return {"status": "error", "message": "RPi not connected"}

    step = abs(req.step)
    if req.direction == "down":
        step = -step

    try:
        # Tell RPi to adjust volume relatively
        await ws.send_json({"type": "volume_adjust", "step": step})
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/api/snapshot")
async def post_snapshot(request: Request):
    """Receive a JPEG snapshot from the RPi and forward to MQTT."""
    from .main import _get_state
    state = _get_state(request.app)
    body = await request.body()
    if not body:
        return {"status": "error", "message": "Empty body"}

    if state.display_orientation == "portrait":
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(body))
            img = img.rotate(90, expand=True)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            body = buf.getvalue()
        except Exception as e:
            log.warning(f"Snapshot rotation failed: {e}")

    # Cache for GET /api/snapshot.jpg
    state.last_snapshot = body

    # Forward to MQTT for HA camera entity
    if state.mqtt_bridge:
        await state.mqtt_bridge.publish_snapshot(body)

    return {"status": "ok", "size": len(body)}


@router.get("/api/snapshot.jpg")
async def get_snapshot(request: Request):
    """Return the most recent JPEG snapshot."""
    from .main import _get_state
    state = _get_state(request.app)
    if not state.last_snapshot:
        return Response(status_code=404)
    return Response(content=state.last_snapshot, media_type="image/jpeg")


@router.post("/api/speak")
async def post_speak(request: Request, req: SpeakRequest):
    """
    Speak text verbatim through the RPi speaker — bypasses the LLM.

    Useful for announcements, notifications, or anything that should be
    vocalized exactly without going through Steve's persona.
    """
    from .main import _get_state, broadcast_to_ui, speak_to_satellites
    state = _get_state(request.app)
    text = (req.text or "").strip()

    if not text:
        return {"status": "error", "message": "Empty text"}

    ws = state.audio_websocket
    if not ws:
        return {"status": "error", "message": "RPi not connected"}

    if not state.tts_engine:
        return {"status": "error", "message": "TTS engine not available"}

    log.info(f"REST speak: '{text[:80]}'")

    try:
        audio_bytes = await state.tts_engine.synthesize(text)
    except Exception as e:
        return {"status": "error", "message": f"TTS synthesis failed: {e}"}

    if not audio_bytes:
        return {"status": "error", "message": "TTS produced no audio"}

    # Show on UI as response
    msg = {"type": "response", "text": text}
    try:
        await ws.send_json(msg)
    except Exception:
        pass
    await broadcast_to_ui(state, msg)
    # Force-speak also reaches satellites: text + HAL's voice on each phone.
    await speak_to_satellites(state, text, audio_bytes)
    if state.mqtt_bridge:
        await state.mqtt_bridge.publish_last_response(text)

    # Stream audio to RPi
    try:
        await ws.send_json({"type": "tts_start", "size": len(audio_bytes)})
        if state.pipeline:
            state.pipeline.set_ai_speaking(True)
        chunk_size = 8192
        for i in range(0, len(audio_bytes), chunk_size):
            await ws.send_bytes(audio_bytes[i : i + chunk_size])
        await ws.send_json({"type": "tts_end"})
    except Exception as e:
        if state.pipeline:
            state.pipeline.set_ai_speaking(False)
        return {"status": "error", "message": f"Failed to send audio: {e}"}

    return {"status": "ok", "spoke": text}


@router.post("/api/mute")
async def post_mute_toggle(request: Request):
    """Toggle RPi mic mute. Returns the new mute state."""
    from .main import _get_state
    state = _get_state(request.app)
    ws = state.audio_websocket
    if not ws:
        return {"status": "error", "message": "RPi not connected"}

    try:
        await ws.send_json({"type": "mute_toggle"})
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/api/mute")
async def get_mute_status(request: Request):
    """Get current RPi mic mute state."""
    from .main import _get_state
    state = _get_state(request.app)
    return {"muted": state.mic_muted}


# --- Theme plug-in endpoints -------------------------------------------------

@router.get("/api/themes")
async def list_themes_endpoint(request: Request):
    """JSON list of installed themes. Kiosk fetches this on startup
    and on `themes_changed` to build the picker dropdown."""
    from .main import _get_state
    from .themes import ThemeRegistry
    state = _get_state(request.app)
    if not isinstance(state.themes, ThemeRegistry):
        return {"themes": []}
    return {"themes": [t.to_public() for t in state.themes.themes]}


@router.get("/themes/{name}/{filename}")
async def get_theme_file(request: Request, name: str, filename: str):
    """Serve a theme's static asset (theme.css or effect.js)."""
    from fastapi.responses import FileResponse, Response
    from .main import _get_state
    from .themes import ThemeRegistry
    state = _get_state(request.app)
    if not isinstance(state.themes, ThemeRegistry):
        return Response(status_code=404)
    path = state.themes.static_path(name, filename)
    if not path:
        return Response(status_code=404)
    media_type = "text/css" if filename.endswith(".css") else (
        "application/javascript" if filename.endswith(".js") else "application/octet-stream"
    )
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )
