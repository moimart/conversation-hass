"""HAL — Main FastAPI server coordinating audio pipeline."""

import asyncio
import io
import json
import logging
import math
import os
import re
import struct
import time
import wave
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .audio_pipeline import AudioPipeline
from .calendar_ha import (
    fetch_calendar_events,
    list_calendars,
    resolve_calendars,
)
from .conversation import ConversationManager
from .go2rtc import Go2RTCClient
from .ha_ws import HAWSClient
from .local_tools import LocalToolsClient
from .runtime_config import RuntimeConfig
from .themes import ThemeRegistry
from .mcp_client import MCPClient, MultiMCPClient
from .memory import MemoryClient
from .mqtt_bridge import MQTTBridge
from .tts import TTSEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("hal")


@dataclass
class AppState:
    """All server state in one place — no module-level globals."""

    pipeline: AudioPipeline | None = None
    conversation: ConversationManager | None = None
    tts_engine: TTSEngine | None = None
    mcp_client: MultiMCPClient | None = None
    memory_client: MemoryClient | None = None
    wake_chime: bytes | None = None
    ui_clients: set = field(default_factory=set)
    audio_websocket: WebSocket | None = None  # RPi audio client
    mic_muted: bool = False  # cached from RPi
    local_tools: object | None = None  # LocalToolsClient
    current_theme: str = "dark"
    theme_day: str = "birch"
    theme_night: str = "dark"
    auto_theme: bool = True
    runtime_config: object | None = None  # RuntimeConfig
    voice_options: list = field(default_factory=list)
    model_options: list = field(default_factory=list)
    themes: object | None = None  # ThemeRegistry
    theme_scheduler_task: object | None = None
    mqtt_bridge: MQTTBridge | None = None
    tts_volume: float = 0.7  # cached, mirrors RPi state
    last_snapshot: bytes | None = None  # latest JPEG from the RPi
    sendspin_player_entity: str = ""  # e.g. media_player.hal_speaker
    sendspin_pause_during_tts: bool = False  # Shape C fallback
    # Set when we paused MA ourselves so we know to resume; never set
    # when the user paused manually before HAL spoke.
    sendspin_was_playing: bool = False
    # Serializes media_player service calls so rapid HID volume mashes
    # don't fan out into N concurrent HA calls arriving out of order.
    _ma_lock: object = field(default_factory=asyncio.Lock)
    # HA WebSocket client (lazy) used for WebRTC signaling
    ha_ws: object | None = None
    # go2rtc REST client (lazy) used by stream_rtsp
    go2rtc: object | None = None
    # Active WebRTC session, if any. Shape:
    #   {session_id, kind: "ha"|"rtsp", entity_id|rtsp_url, safety_task,
    #    ha_sub_id?, ha_session_id?, go2rtc_name?}
    active_stream: dict | None = None
    # Active Push-to-Talk session (server/app/ptt.PTTSession), if any.
    # Open via ptt.start_ptt, closed via ptt.end_ptt or safety timeout.
    ptt: object | None = None
    # Active photo-frame session (server/app/photo_frame.PhotoFrameSession).
    # Open via photo_frame.start_photo_frame, closed via stop_photo_frame
    # or by the kiosk's photo_frame_dismissed message.
    photo_frame_session: object | None = None


def _generate_chime() -> bytes:
    """Generate a short two-tone chime as WAV bytes."""
    sample_rate = 22050
    duration = 0.15
    n_samples = int(sample_rate * duration)
    freqs = [880, 1320]

    import numpy as np
    all_samples = []
    for freq in freqs:
        t = np.arange(n_samples) / sample_rate
        envelope = np.minimum(1.0, np.arange(n_samples) / 200) * np.minimum(1.0, np.arange(n_samples - 1, -1, -1) / 200)
        tone = (envelope * 12000 * np.sin(2 * np.pi * freq * t)).astype(np.int16)
        all_samples.append(tone)

    raw = np.concatenate(all_samples).tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(raw)
    return buf.getvalue()


VALID_THEMES = {
    "dark", "birch", "odyssey", "japandi", "sal", "glados",
    "matrix", "mother", "joi", "kitt", "forest", "sunset",
}


def _theme_names(state: "AppState") -> list[str]:
    """Names of themes the registry knows about. Falls back to the static
    set if the registry hasn't loaded yet (or is empty for some reason)."""
    if isinstance(state.themes, ThemeRegistry) and state.themes.names:
        return state.themes.names
    return sorted(VALID_THEMES)


def _is_valid_theme(state: "AppState", name: str) -> bool:
    return name in _theme_names(state)


def _parse_iso(ts: str) -> "datetime | None":
    from datetime import datetime
    if not ts:
        return None
    try:
        # HA returns ISO with offset like 2026-04-15T18:42:00+00:00
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


async def _query_sun_state(state: AppState) -> str | None:
    """Fetch HA's sun.sun entity state ('above_horizon'/'below_horizon').

    Returns None if HA MCP isn't connected or the response can't be parsed.
    Shared by the theme scheduler and the live-config theme handlers.
    """
    if not state.mcp_client or "ha_get_state" not in state.mcp_client.tool_names:
        return None
    result = await state.mcp_client.call_tool("ha_get_state", {"entity_id": "sun.sun"})
    data = None
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        m = re.search(r"\{[\s\S]*\}", result or "")
        if m:
            try:
                data = json.loads(m.group(0))
            except (TypeError, ValueError):
                pass
    if not isinstance(data, dict):
        return None
    entity = data.get("data", data)
    return entity.get("state") if isinstance(entity, dict) else None


async def _theme_scheduler(state: AppState):
    """Background task: switch themes at dusk/dawn based on HA's sun.sun.

    Strategy: poll sun.sun every 5 minutes. Apply the correct theme for
    the current sun state (below_horizon → night, above_horizon → day).
    Simple and robust — no precise scheduling needed since we just keep
    the theme in sync with the sun's current state.
    """
    import asyncio
    import json
    import re

    log.info(f"Theme scheduler started (day={state.theme_day}, night={state.theme_night})")

    while True:
        try:
            if not state.mcp_client or "ha_get_state" not in state.mcp_client.tool_names:
                log.warning("Theme scheduler: HA tool ha_get_state not available, retry in 5min")
                await asyncio.sleep(300)
                continue

            result = await state.mcp_client.call_tool("ha_get_state", {"entity_id": "sun.sun"})
            log.info(f"Theme scheduler: sun.sun raw response (len={len(result or '')}): {(result or '')[:300]}")

            # Parse JSON (sometimes wrapped in markdown)
            data = None
            try:
                data = json.loads(result)
            except (TypeError, ValueError):
                m = re.search(r"\{[\s\S]*\}", result or "")
                if m:
                    try:
                        data = json.loads(m.group(0))
                    except (TypeError, ValueError):
                        pass

            if data is None:
                log.warning("Theme scheduler: could not parse sun.sun response — skipping")
                await asyncio.sleep(300)
                continue

            # HA MCP wraps the entity in {"data": {...}, "metadata": {...}}
            entity = data.get("data", data) if isinstance(data, dict) else {}
            sun_state = entity.get("state") if isinstance(entity, dict) else None
            log.info(f"Theme scheduler: sun_state={sun_state!r}, current_theme={state.current_theme}, day={state.theme_day}, night={state.theme_night}")

            if sun_state is None:
                log.warning(f"Theme scheduler: no 'state' field in response. Top-level keys: {list(data.keys()) if isinstance(data, dict) else type(data).__name__}")
                await asyncio.sleep(300)
                continue

            target = state.theme_night if sun_state == "below_horizon" else state.theme_day

            if state.current_theme != target:
                log.info(f"Theme scheduler: sun is {sun_state} → switching {state.current_theme} → {target}")
                await apply_theme(state, target)
            else:
                log.debug(f"Theme scheduler: already on correct theme ({target})")

            await asyncio.sleep(300)  # check every 5 minutes

        except asyncio.CancelledError:
            log.info("Theme scheduler cancelled")
            return
        except Exception as e:
            log.error(f"Theme scheduler error: {e}")
            await asyncio.sleep(300)


async def _push_to_rpi(state: AppState, msg: dict) -> bool:
    """Send a JSON message to the RPi audio websocket (if connected)."""
    ws = state.audio_websocket
    if not ws:
        return False
    try:
        await ws.send_json(msg)
        return True
    except Exception as e:
        log.warning(f"Could not send {msg.get('type')} to RPi: {e}")
        return False


async def _ma_call(state: AppState, service: str, extra: dict | None = None) -> None:
    """Invoke a media_player service on the configured Sendspin player.

    Serialized via state._ma_lock so concurrent button presses arrive
    at HA in the order they were issued, with a 5s timeout per call so
    a hung MCP tool can't pile up the queue indefinitely.
    """
    entity = state.sendspin_player_entity
    if not entity or not state.mcp_client:
        return
    args = {
        "domain": "media_player",
        "service": service,
        "target": {"entity_id": entity},
    }
    if extra:
        args["service_data"] = extra
    async with state._ma_lock:
        try:
            await asyncio.wait_for(
                state.mcp_client.call_tool("ha_call_service", args),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            log.warning(f"ha_call_service {service} timed out after 5s")
        except Exception as e:
            log.warning(f"ha_call_service {service} failed: {e}")


async def _ma_volume_step(state: AppState, step: float) -> None:
    """Forward a hardware vol button press to the Sendspin player in MA."""
    if not state.sendspin_player_entity:
        return
    service = "volume_up" if step > 0 else "volume_down"
    await _ma_call(state, service)


async def _ma_query_state(state: AppState) -> str | None:
    """Return the Sendspin player's current state ('playing'/'paused'/...).

    Returns None on any error or if not configured.
    """
    entity = state.sendspin_player_entity
    if not entity or not state.mcp_client:
        return None
    try:
        result = await asyncio.wait_for(
            state.mcp_client.call_tool("ha_get_state", {"entity_id": entity}),
            timeout=3.0,
        )
    except (asyncio.TimeoutError, Exception) as e:
        log.warning(f"ha_get_state for sendspin player failed: {e}")
        return None
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        return None
    # HA MCP wraps the entity in {"data": {...}, "metadata": {...}}
    entity_data = data.get("data", data) if isinstance(data, dict) else {}
    if not isinstance(entity_data, dict):
        return None
    return entity_data.get("state")


async def _ma_pause_if_playing(state: AppState) -> None:
    """Pause MA only if currently playing; remember so we can resume later."""
    ma_state = await _ma_query_state(state)
    if ma_state == "playing":
        state.sendspin_was_playing = True
        await _ma_call(state, "media_pause")
    else:
        # User paused/stopped manually — don't touch it on resume.
        state.sendspin_was_playing = False


async def _ma_resume_if_we_paused(state: AppState) -> None:
    """Resume MA only if we were the ones who paused it."""
    if state.sendspin_was_playing:
        state.sendspin_was_playing = False
        await _ma_call(state, "media_play")


# --- WebRTC streaming ---------------------------------------------------------

async def _ensure_ha_ws(state: AppState) -> HAWSClient | None:
    """Lazy-connect the HA WS client. Returns None if HA_URL/HA_TOKEN unset."""
    ha_url = os.environ.get("HA_URL", "").strip()
    ha_token = os.environ.get("HA_TOKEN", "").strip()
    if not ha_url or not ha_token:
        return None
    if state.ha_ws and getattr(state.ha_ws, "connected", False):
        return state.ha_ws
    client = HAWSClient(ha_url, ha_token)
    await client.connect()
    state.ha_ws = client
    return client


async def _stop_active_stream(state: AppState, *, notify_kiosk: bool = True) -> None:
    """Tear down the active WebRTC session (if any)."""
    session = state.active_stream
    if not session:
        return
    state.active_stream = None
    safety = session.get("safety_task")
    if safety:
        safety.cancel()
    # HA path
    sub_id = session.get("ha_sub_id")
    if sub_id is not None and isinstance(state.ha_ws, HAWSClient):
        await state.ha_ws.unsubscribe(sub_id)
    # go2rtc path
    go2rtc_name = session.get("go2rtc_name")
    if go2rtc_name and isinstance(state.go2rtc, Go2RTCClient):
        await state.go2rtc.delete_stream(go2rtc_name)
    if notify_kiosk:
        msg = {"type": "stream_stop", "session_id": session.get("session_id", "")}
        ws = state.audio_websocket
        if ws:
            try:
                await ws.send_json(msg)
            except Exception:
                pass
        await broadcast_to_ui(state, msg)


def _ensure_go2rtc(state: AppState) -> Go2RTCClient | None:
    """Lazy-construct the go2rtc client. Returns None if GO2RTC_URL unset."""
    url = os.environ.get("GO2RTC_URL", "").strip()
    if not url:
        return None
    if isinstance(state.go2rtc, Go2RTCClient):
        return state.go2rtc
    state.go2rtc = Go2RTCClient(url)
    return state.go2rtc


async def _stop_active_video(state: AppState) -> None:
    """Tell the kiosk to clear any HTTP video that may be playing.

    We don't track video state server-side (the kiosk owns the lifecycle —
    no peer connection or HA subscription to clean up), so this is just a
    fire-and-forget message. Safe to call at any time.
    """
    msg = {"type": "video_stop"}
    ws = state.audio_websocket
    if ws:
        try:
            await ws.send_json(msg)
        except Exception:
            pass
    await broadcast_to_ui(state, msg)


async def _dispatch_play_video(
    state: AppState,
    url: str,
    *,
    duration_s: int | None = None,
    loop: bool = False,
    muted: bool = False,
) -> bool:
    """Push a play_video message to the kiosk. Clears any active stream first."""
    await _stop_active_stream(state)  # mutual exclusion with WebRTC streams
    msg = {
        "type": "play_video",
        "url": url,
        "loop": bool(loop),
        "muted": bool(muted),
    }
    if duration_s is not None:
        msg["duration_s"] = max(1, min(7200, int(duration_s)))
    ws = state.audio_websocket
    sent = False
    if ws:
        try:
            await ws.send_json(msg)
            sent = True
        except Exception as e:
            log.warning(f"play_video ws send failed: {e}")
    await broadcast_to_ui(state, msg)
    return sent


async def _negotiate_rtsp_offer(state: AppState, session_id: str, offer_sdp: str) -> None:
    """Hand a kiosk-side SDP offer to go2rtc and forward the answer back."""
    if not state.active_stream or state.active_stream.get("session_id") != session_id:
        return
    if state.active_stream.get("kind") != "rtsp":
        return
    name = state.active_stream.get("go2rtc_name")
    if not name or not isinstance(state.go2rtc, Go2RTCClient):
        return
    answer_sdp = await state.go2rtc.webrtc_offer(name, offer_sdp)
    if not answer_sdp:
        log.warning(f"go2rtc returned no answer for {name!r}")
        await _stop_active_stream(state)
        return
    msg = {
        "type": "webrtc_signal",
        "session_id": session_id,
        "kind": "answer",
        "sdp": answer_sdp,
    }
    ws = state.audio_websocket
    if ws:
        try:
            await ws.send_json(msg)
        except Exception as e:
            log.debug(f"forward go2rtc answer to kiosk failed: {e}")


async def _on_ha_webrtc_event(state: AppState, session_id: str, event: dict) -> None:
    """Forward an HA WebRTC subscription event to the kiosk as a webrtc_signal.

    HA emits events:
      {type: "session", session_id: <HA's id>}    — first; capture for candidate routing
      {type: "answer",  answer: <sdp>}            — forward as 'answer'
      {type: "candidate", candidate: {...}}       — forward as 'candidate'
      {type: "error",   code, message}            — tear down the stream
    """
    msg_type = event.get("type")
    log.debug(f"HA WebRTC event for session {session_id}: type={msg_type}")
    out: dict | None = None
    if msg_type == "session":
        if state.active_stream and state.active_stream.get("session_id") == session_id:
            state.active_stream["ha_session_id"] = event.get("session_id")
            log.info(f"Captured HA session_id={state.active_stream['ha_session_id']} for {session_id}")
        return
    elif msg_type == "answer":
        out = {
            "type": "webrtc_signal",
            "session_id": session_id,
            "kind": "answer",
            "sdp": event.get("answer"),
        }
    elif msg_type == "candidate":
        cand = event.get("candidate") or {}
        out = {
            "type": "webrtc_signal",
            "session_id": session_id,
            "kind": "candidate",
            "candidate": cand.get("candidate") if isinstance(cand, dict) else cand,
            "sdpMid": cand.get("sdpMid") if isinstance(cand, dict) else None,
            "sdpMLineIndex": cand.get("sdpMLineIndex") if isinstance(cand, dict) else None,
        }
    elif msg_type == "error":
        log.warning(f"HA WebRTC error: {event}")
        await _stop_active_stream(state)
        return
    if not out:
        return
    ws = state.audio_websocket
    if ws:
        try:
            await ws.send_json(out)
        except Exception as e:
            log.debug(f"forward HA WebRTC event to kiosk failed: {e}")


async def _start_webrtc_stream(state: AppState, entity_id: str, session_id: str, offer_sdp: str) -> bool:
    """Begin the HA WebRTC offer subscription. Returns True on success."""
    ha = await _ensure_ha_ws(state)
    if not ha:
        log.warning("HA WS unavailable (HA_URL/HA_TOKEN unset)")
        return False

    async def handler(event: dict) -> None:
        await _on_ha_webrtc_event(state, session_id, event)

    sub_id = await ha.subscribe(
        {"type": "camera/webrtc/offer", "entity_id": entity_id, "offer": offer_sdp},
        handler,
    )
    if state.active_stream:
        state.active_stream["ha_sub_id"] = sub_id
    return True


async def _forward_kiosk_candidate(state: AppState, session_id: str, payload: dict) -> None:
    """Forward a kiosk-side ICE candidate to HA for the active session.

    Uses HA's session_id (captured from the first 'session' event), not
    our internal session_id — HA only knows the id it issued.
    """
    if not state.active_stream or state.active_stream.get("session_id") != session_id:
        return
    if not isinstance(state.ha_ws, HAWSClient):
        return
    ha_session_id = state.active_stream.get("ha_session_id")
    if not ha_session_id:
        # HA hasn't emitted its session event yet — drop the candidate; the
        # peer connection will retry/regenerate as needed.
        log.debug("kiosk candidate dropped: no HA session_id yet")
        return
    try:
        await state.ha_ws.send_command(
            {
                "type": "camera/webrtc/candidate",
                "session_id": ha_session_id,
                "candidate": {
                    "candidate": payload.get("candidate", ""),
                    "sdpMid": payload.get("sdpMid"),
                    "sdpMLineIndex": payload.get("sdpMLineIndex"),
                },
            },
            timeout=2.0,
        )
    except Exception as e:
        log.debug(f"forward kiosk candidate failed: {e}")


# --- Image push to orb -------------------------------------------------------

# Anything pushed to the kiosk uses the existing show_camera WS message. The
# kiosk doesn't care whether the bytes came from an HA camera, an MQTT topic,
# or an LLM tool — it just paints them inside the orb for duration_s seconds.

_IMAGE_MAX_BYTES = 8 * 1024 * 1024  # 8 MB
_IMAGE_FETCH_TIMEOUT = 10.0


async def _fetch_ollama_tags(host: str) -> list[str]:
    """Return the model names Ollama has installed (empty list on failure)."""
    import httpx
    url = host.rstrip("/") + "/api/tags"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json() or {}
            return sorted({m.get("name") for m in (data.get("models") or []) if m.get("name")})
    except Exception as e:
        log.warning(f"Ollama tags fetch failed: {e}")
        return []


async def _fetch_ollama_context_length(host: str, model: str) -> int | None:
    """Return the model's native max context window via /api/show.

    Ollama exposes the architecture-specific value under
    `model_info["<arch>.context_length"]` (e.g.
    `llama.context_length`, `gemma3.context_length`,
    `qwen2.context_length`). Returns None on any failure so callers
    can fall back to a static cap.
    """
    if not model:
        return None
    import httpx
    url = host.rstrip("/") + "/api/show"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(url, json={"model": model})
            r.raise_for_status()
            data = r.json() or {}
        info = data.get("model_info") or {}
        arch = info.get("general.architecture") or ""
        if arch:
            v = info.get(f"{arch}.context_length")
            if isinstance(v, int) and v > 0:
                return v
        # Fall back: scan for any *.context_length key.
        for k, v in info.items():
            if k.endswith(".context_length") and isinstance(v, int) and v > 0:
                return v
    except Exception as e:
        log.warning(f"Ollama show failed for {model!r}: {e}")
    return None


async def _fetch_ha_image_entity(entity_id: str) -> tuple[bytes, str] | None:
    """Fetch an HA image.* entity via /api/image_proxy/<entity_id>.

    Uses HA_URL + HA_TOKEN. Returns (bytes, mime) or None. Reuses the
    URL fetcher's caps (8 MB, 10 s, image/* content types).
    """
    ha_url = os.environ.get("HA_URL", "").strip().rstrip("/")
    ha_token = os.environ.get("HA_TOKEN", "").strip()
    if not ha_url or not ha_token:
        log.warning("HA image fetch unavailable (HA_URL / HA_TOKEN unset)")
        return None
    return await _fetch_image_url(f"{ha_url}/api/image_proxy/{entity_id}")


async def _fetch_image_url(url: str) -> tuple[bytes, str] | None:
    """Fetch an image URL with sane caps. Returns (bytes, mime) or None.

    Auto-attaches the HA bearer token when the URL points at the configured
    HA instance, so /local/ and /api/camera_proxy/ paths work without extra
    config. Refuses non-image content types and anything over 8 MB.
    """
    import httpx
    headers: dict[str, str] = {}
    ha_url = os.environ.get("HA_URL", "").rstrip("/")
    ha_token = os.environ.get("HA_TOKEN", "")
    if ha_url and ha_token and url.startswith(ha_url):
        headers["Authorization"] = f"Bearer {ha_token}"
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=_IMAGE_FETCH_TIMEOUT,
            max_redirects=3,
        ) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            mime = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            if not mime.startswith("image/"):
                log.warning(f"refusing non-image content-type {mime!r} from {url}")
                return None
            data = r.content
            if len(data) > _IMAGE_MAX_BYTES:
                log.warning(f"image at {url} is {len(data)} bytes, cap is {_IMAGE_MAX_BYTES}")
                return None
            return data, mime
    except Exception as e:
        log.warning(f"image fetch failed for {url}: {e}")
        return None


def _detect_image_mime(data: bytes) -> str | None:
    """Return the MIME type for a JPEG/PNG/GIF/WebP byte payload, or None."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def _dispatch_show_image(
    state: AppState,
    image_b64: str,
    mime: str,
    duration_s: int,
    *,
    entity_id: str = "external",
) -> bool:
    """Push an image to the kiosk via the existing show_camera WS message.

    Stops any active live stream and any playing video first (mutual
    exclusion). Returns True if we got the message out — even if the kiosk
    websocket is closed (the UI broadcast is best-effort).
    """
    await _stop_active_stream(state)
    await _stop_active_video(state)
    msg = {
        "type": "show_camera",
        "image": image_b64,
        "mime": mime,
        "duration_s": duration_s,
        "entity_id": entity_id,
    }
    ws = state.audio_websocket
    sent = False
    if ws:
        try:
            await ws.send_json(msg)
            sent = True
        except Exception as e:
            log.warning(f"show_image ws send failed: {e}")
    await broadcast_to_ui(state, msg)
    return sent


async def _push_image_payload(state: AppState, raw: bytes | str, default_duration: int = 60) -> str:
    """Handle a payload from the MQTT image/set topic or text helper.

    Detects the payload form (binary image / URL / JSON wrapper) and
    dispatches it. Returns a short status string for logging.
    """
    duration_s = default_duration
    image_bytes: bytes | None = None
    mime: str | None = None
    image_b64_inline: str | None = None
    url: str | None = None

    if isinstance(raw, (bytes, bytearray)):
        # Binary: image bytes if it looks like one, otherwise try as text.
        detected = _detect_image_mime(bytes(raw))
        if detected:
            image_bytes = bytes(raw)
            mime = detected
        else:
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return "unrecognized binary payload"

    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("{"):
            try:
                data = json.loads(s)
            except json.JSONDecodeError as e:
                return f"invalid JSON: {e}"
            try:
                duration_s = int(data.get("duration_s", default_duration))
            except (TypeError, ValueError):
                duration_s = default_duration
            if data.get("url"):
                url = str(data["url"])
            elif data.get("image"):
                image_b64_inline = str(data["image"])
                mime = str(data.get("mime") or "image/jpeg")
        elif s.startswith("http://") or s.startswith("https://"):
            url = s
        else:
            return "payload is neither image bytes, URL, nor JSON"

    duration_s = max(5, min(600, duration_s))

    if url:
        fetched = await _fetch_image_url(url)
        if not fetched:
            return f"fetch failed for {url}"
        image_bytes, mime = fetched

    if image_b64_inline:
        await _dispatch_show_image(state, image_b64_inline, mime or "image/jpeg", duration_s)
        return f"pushed inline base64 ({len(image_b64_inline)} chars) for {duration_s}s"

    if image_bytes and mime:
        import base64
        b64 = base64.b64encode(image_bytes).decode("ascii")
        await _dispatch_show_image(state, b64, mime, duration_s)
        return f"pushed {len(image_bytes)} bytes ({mime}) for {duration_s}s"

    return "nothing to display"


async def apply_theme(state: AppState, theme: str) -> bool:
    """Update server-side theme and push to RPi web UI."""
    if not _is_valid_theme(state, theme):
        return False
    state.current_theme = theme
    msg = {"type": "set_theme", "name": theme}
    await broadcast_to_ui(state, msg)
    await _push_to_rpi(state, msg)
    if state.mqtt_bridge:
        await state.mqtt_bridge.publish_theme(theme)
    log.info(f"Theme set to: {theme}")
    return True


# === Calendar overlay ========================================================

_VALID_CAL_VIEWS = ("month", "week", "day")


def _calendar_range(view: str, anchor: "datetime | None" = None) -> "tuple[datetime, datetime]":
    """Return (start, end) UTC datetimes covering the requested view.

    `anchor` is the date the user wants to land on. Defaults to today (UTC).
    For "tomorrow's calendar", the caller passes tomorrow's date; this
    function then computes the day/week/month that contains it.

    * `day`:   anchor 00:00 → anchor+1 day 00:00 (just that day)
    * `week`:  Monday of anchor's week → following Monday
    * `month`: first of anchor's month → first of next month
    """
    from datetime import datetime, timedelta, timezone
    now = anchor or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if view == "day":
        return today, today + timedelta(days=1)
    if view == "week":
        # ISO weekday: Mon=0 .. Sun=6
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=7)
    # month — first to first-of-next-month
    first = today.replace(day=1)
    if first.month == 12:
        next_first = first.replace(year=first.year + 1, month=1)
    else:
        next_first = first.replace(month=first.month + 1)
    return first, next_first


def _parse_anchor_date(s: str) -> "datetime | None":
    """Parse the LLM's / MQTT's `anchor_date` string into a UTC datetime.

    Accepts:
      * ISO date            "2026-05-18"
      * ISO datetime        "2026-05-18T00:00:00"
      * Short ISO datetime  "2026-05-18T15:30"

    Returns None on empty/unparseable input — callers should fall back to
    today() when None.
    """
    from datetime import datetime, timezone
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        log.warning(f"_parse_anchor_date: unparseable {s!r}, falling back to today")
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


async def _show_calendar(
    state: AppState,
    view: str = "month",
    calendar_name: str = "",
    duration_s: int | None = None,
    anchor_date: str = "",
) -> dict:
    """Resolve calendar(s), fetch events, push show_calendar to kiosk + UI clients.

    Returns the same payload that was pushed (useful for tests / LLM tool reply).
    `calendar_name` empty / no match → merged view of all HA calendars.
    `anchor_date` empty → today; otherwise ISO date string (see
    `_parse_anchor_date`) to show a specific day/week/month.
    """
    view = (view or "month").lower().strip()
    if view not in _VALID_CAL_VIEWS:
        view = "month"

    if duration_s is None:
        duration_s = int(state.runtime_config.get("calendar_dismiss_seconds", 30) or 30)
    duration_s = max(5, min(600, int(duration_s)))

    name_query = (calendar_name or "").strip() or str(
        state.runtime_config.get("calendar_default_source", "") or ""
    ).strip()

    available = await list_calendars()
    chosen = resolve_calendars(name_query or None, available)

    anchor = _parse_anchor_date(anchor_date)
    start, end = _calendar_range(view, anchor)
    events = await fetch_calendar_events([c["entity_id"] for c in chosen], start, end)

    if not chosen:
        source_label = "(no calendars)"
    elif name_query and len(chosen) == 1:
        source_label = chosen[0]["name"]
    else:
        source_label = "All calendars"

    payload = {
        "type": "show_calendar",
        "view": view,
        "title": _format_range_title(view, start),
        "source_label": source_label,
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "events": events,
        "duration_s": duration_s,
    }
    await _push_to_rpi(state, payload)
    await broadcast_to_ui(state, payload)
    log.info(
        f"calendar shown: view={view} source={source_label!r} "
        f"events={len(events)} duration={duration_s}s"
    )
    return payload


async def _hide_calendar(state: AppState) -> bool:
    msg = {"type": "hide_calendar"}
    pushed = await _push_to_rpi(state, msg)
    await broadcast_to_ui(state, msg)
    log.info("calendar hidden")
    return pushed


def _format_range_title(view: str, start: "datetime") -> str:
    """e.g. 'May 2026' / 'Week of 11 May 2026' / 'Saturday 16 May 2026'."""
    if view == "month":
        return start.strftime("%B %Y")
    if view == "week":
        return "Week of " + start.strftime("%d %B %Y")
    return start.strftime("%A %d %B %Y")


def _build_local_tools(state: AppState) -> LocalToolsClient:
    """Construct the LocalToolsClient with all tools wired to AppState."""
    tools = LocalToolsClient()

    async def set_theme(args: dict) -> str:
        name = (args.get("name") or "").lower().strip()
        valid = _theme_names(state)
        if name not in valid:
            return f"Invalid theme '{name}'. Valid: {', '.join(valid)}"
        ok = await apply_theme(state, name)
        return f"Theme set to {name}" if ok else "Failed to apply theme"

    tools.register(
        "ui_set_theme",
        "Change the HAL web UI theme. Use 'dark' at night/dusk, 'birch' or 'japandi' for light rooms during the day, 'odyssey' for a sterile white look.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "enum": _theme_names(state), "description": "Theme name"},
            },
            "required": ["name"],
        },
        set_theme,
    )

    async def get_current_theme(_args: dict) -> str:
        return f"Current theme: {state.current_theme}"

    tools.register(
        "ui_get_theme",
        "Get the currently active HAL UI theme.",
        {"type": "object", "properties": {}},
        get_current_theme,
    )

    async def set_volume(args: dict) -> str:
        try:
            level = float(args.get("level", 0.7))
        except (TypeError, ValueError):
            return "Volume level must be a number between 0 and 1"
        level = max(0.0, min(1.0, level))
        ok = await _push_to_rpi(state, {"type": "volume", "level": level})
        return f"Volume set to {level:.0%}" if ok else "RPi not connected"

    tools.register(
        "audio_set_volume",
        "Set the speaker volume on the Raspberry Pi (0.0 = silent, 1.0 = max).",
        {
            "type": "object",
            "properties": {
                "level": {"type": "number", "minimum": 0, "maximum": 1, "description": "Volume level"},
            },
            "required": ["level"],
        },
        set_volume,
    )

    async def adjust_volume(args: dict) -> str:
        direction = (args.get("direction") or "").lower()
        if direction not in ("up", "down"):
            return "direction must be 'up' or 'down'"
        try:
            step = float(args.get("step", 0.1))
        except (TypeError, ValueError):
            step = 0.1
        if direction == "down":
            step = -abs(step)
        else:
            step = abs(step)
        ok = await _push_to_rpi(state, {"type": "volume_adjust", "step": step})
        return f"Volume {direction} by {abs(step):.0%}" if ok else "RPi not connected"

    tools.register(
        "audio_adjust_volume",
        "Adjust the speaker volume up or down by a small step (default 10%).",
        {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down"]},
                "step": {"type": "number", "default": 0.1},
            },
            "required": ["direction"],
        },
        adjust_volume,
    )

    async def mute(_args: dict) -> str:
        ok = await _push_to_rpi(state, {"type": "mute_toggle"})
        return "Mic mute toggled" if ok else "RPi not connected"

    tools.register(
        "audio_toggle_mute",
        "Toggle the microphone mute on the Raspberry Pi.",
        {"type": "object", "properties": {}},
        mute,
    )

    async def get_sun_times(_args: dict) -> str:
        """Query Home Assistant's sun.sun entity for sunrise/sunset/dusk."""
        # Find an MCP server that has ha_get_state
        if not state.mcp_client or "ha_get_state" not in state.mcp_client.tool_names:
            return "Home Assistant integration not available"
        result = await state.mcp_client.call_tool("ha_get_state", {"entity_id": "sun.sun"})
        return result

    tools.register(
        "get_sun_times",
        "Get sunrise, sunset, dusk and dawn times from Home Assistant for the current location.",
        {"type": "object", "properties": {}},
        get_sun_times,
    )

    async def speak_verbatim(args: dict) -> str:
        """Speak the given text out loud through the RPi, exactly as written.

        Bypasses the natural-language reply: synthesizes TTS for the
        provided text and pushes it directly to the RPi audio websocket.
        Sets a flag so the LLM's final text response does not get spoken
        again (avoids double audio).
        """
        text = (args.get("text") or "").strip()
        if not text:
            return "Cannot speak empty text"

        if not state.tts_engine:
            return "TTS engine not available"

        ws = state.audio_websocket
        if not ws:
            return "RPi not connected — cannot play audio"

        # Synthesize the verbatim text
        try:
            audio_bytes = await state.tts_engine.synthesize(text)
        except Exception as e:
            return f"TTS synthesis failed: {e}"

        if not audio_bytes:
            return "TTS produced no audio"

        # Send the verbatim text to UI clients as the response
        msg = {"type": "response", "text": text}
        try:
            await ws.send_json(msg)
        except Exception:
            pass
        await broadcast_to_ui(state, msg)
        if state.mqtt_bridge:
            await state.mqtt_bridge.publish_last_response(text)

        # Push audio to RPi using the same protocol as on_response
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
            return f"Failed to send audio: {e}"

        # Suppress the final TTS so the LLM's wrap-up doesn't get spoken too
        if state.conversation:
            state.conversation._suppress_final_tts = True

        return f"Spoke verbatim: {text[:80]}"

    tools.register(
        "speak_verbatim",
        "Speak text out loud through HAL's speaker, exactly as written. Use this when the user asks to 'say X out loud', 'repeat X', 'speak X exactly', or any request that needs the precise wording vocalized. After calling this, your text response will NOT be spoken (only the verbatim text is heard), so keep your text reply minimal — the user has already heard the words.",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The exact text to speak out loud"},
            },
            "required": ["text"],
        },
        speak_verbatim,
    )

    async def show_camera(args: dict) -> str:
        entity_id = (args.get("entity_id") or "").strip()
        if not (entity_id.startswith("camera.") or entity_id.startswith("image.")):
            return "entity_id must be a camera.* or image.* entity"
        try:
            duration_s = int(args.get("duration_s", 150))
        except (TypeError, ValueError):
            duration_s = 150
        duration_s = max(5, min(900, duration_s))

        # camera.* — fetch via MCP (returns base64 already)
        if entity_id.startswith("camera."):
            if not state.mcp_client or "ha_get_camera_image" not in state.mcp_client.tool_names:
                return "Camera fetch unavailable (Home Assistant MCP not connected)"
            content = await state.mcp_client.call_tool_content(
                "ha_get_camera_image", {"entity_id": entity_id}
            )
            image_b64 = ""
            mime = "image/jpeg"
            for item in content:
                data = getattr(item, "data", None)
                if data:
                    image_b64 = data
                    mime = getattr(item, "mimeType", None) or mime
                    break
            if not image_b64:
                return f"No image returned for {entity_id}"
            await _dispatch_show_image(state, image_b64, mime, duration_s, entity_id=entity_id)
            return f"Showing {entity_id} on the orb for {duration_s} seconds"

        # image.* — fetch via HA REST /api/image_proxy + bearer
        fetched = await _fetch_ha_image_entity(entity_id)
        if not fetched:
            return f"Could not fetch {entity_id}"
        import base64
        raw, mime = fetched
        await _dispatch_show_image(
            state, base64.b64encode(raw).decode("ascii"), mime, duration_s, entity_id=entity_id,
        )
        return f"Showing {entity_id} on the orb for {duration_s} seconds"

    tools.register(
        "show_camera",
        "Display a Home Assistant camera snapshot OR image entity inside HAL's orb on the kiosk. Accepts both camera.* (fetched via MCP) and image.* (fetched via HA REST /api/image_proxy with bearer token). Use when the user asks to 'show me the <camera>' or 'show the <image entity>'. The image is shown for duration_s seconds (default 150) then the orb returns to normal. To find the right entity_id, use ha_search_entities with domain=camera or domain=image first.",
        {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "camera.* or image.* entity_id, e.g. camera.front_door or image.weather_radar"},
                "duration_s": {"type": "integer", "minimum": 5, "maximum": 900, "default": 150, "description": "How long to keep the image on screen, in seconds"},
            },
            "required": ["entity_id"],
        },
        show_camera,
    )

    async def show_image(args: dict) -> str:
        url = (args.get("url") or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            return "url must start with http:// or https://"
        try:
            duration_s = int(args.get("duration_s", 60))
        except (TypeError, ValueError):
            duration_s = 60
        duration_s = max(5, min(600, duration_s))
        result = await _push_image_payload(state, url, default_duration=duration_s)
        return result

    tools.register(
        "show_image",
        "Display an arbitrary image (by URL) inside HAL's orb. Use when the user asks to 'show a picture of X', 'put X on screen', or 'display the photo at <url>'. Default duration is 60 s; pass duration_s for longer or shorter. Replaces any active camera snapshot or stream.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Public http(s) URL of the image"},
                "duration_s": {"type": "integer", "minimum": 5, "maximum": 600, "default": 60, "description": "How long to show the image, in seconds"},
            },
            "required": ["url"],
        },
        show_image,
    )

    async def stream_camera(args: dict) -> str:
        entity_id = (args.get("entity_id") or "").strip()
        if not entity_id.startswith("camera."):
            return "entity_id must be a camera.* entity"
        try:
            duration_s = int(args.get("duration_s", 300))
        except (TypeError, ValueError):
            duration_s = 300
        duration_s = max(10, min(1800, duration_s))

        if not os.environ.get("HA_URL") or not os.environ.get("HA_TOKEN"):
            return "Live streaming unavailable (HA_URL/HA_TOKEN not configured)"

        # Replace any active modality (snapshot or stream)
        await _stop_active_stream(state)
        ws = state.audio_websocket
        if not ws:
            return "RPi not connected — cannot start stream"

        import uuid
        session_id = uuid.uuid4().hex

        async def safety_stop():
            try:
                await asyncio.sleep(duration_s)
                await _stop_active_stream(state)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        state.active_stream = {
            "session_id": session_id,
            "kind": "ha",
            "entity_id": entity_id,
            "ha_sub_id": None,
            "safety_task": asyncio.create_task(safety_stop()),
        }

        try:
            await ws.send_json({
                "type": "stream_start",
                "session_id": session_id,
                "entity_id": entity_id,
                "mode": "trickle",
            })
        except Exception as e:
            await _stop_active_stream(state, notify_kiosk=False)
            return f"Failed to notify kiosk: {e}"

        return f"Streaming {entity_id} live for up to {duration_s} seconds — say 'stop streaming' to end early"

    tools.register(
        "stream_camera",
        "Start a live WebRTC video stream from a Home Assistant camera and display it inside HAL's orb. Use when the user asks to 'watch <camera> live', 'stream the <camera>', or 'show <camera> live'. The stream stays up to duration_s seconds (default 300 = 5 minutes); the user can end it earlier with 'stop streaming'. Replaces any active snapshot or stream. To find the right entity_id, use ha_search_entities with domain=camera first.",
        {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "Camera entity_id, e.g. camera.front_door"},
                "duration_s": {"type": "integer", "minimum": 10, "maximum": 1800, "default": 300, "description": "Maximum stream duration in seconds (safety auto-stop)"},
            },
            "required": ["entity_id"],
        },
        stream_camera,
    )

    async def stream_rtsp(args: dict) -> str:
        rtsp_url = (args.get("rtsp_url") or "").strip()
        if not (rtsp_url.startswith("rtsp://") or rtsp_url.startswith("rtsps://")):
            return "rtsp_url must start with rtsp:// or rtsps://"
        try:
            duration_s = int(args.get("duration_s", 300))
        except (TypeError, ValueError):
            duration_s = 300
        duration_s = max(10, min(1800, duration_s))

        client = _ensure_go2rtc(state)
        if not client:
            return "Live RTSP streaming unavailable (GO2RTC_URL not configured)"

        await _stop_active_stream(state)
        ws = state.audio_websocket
        if not ws:
            return "RPi not connected — cannot start stream"

        import uuid
        session_id = uuid.uuid4().hex
        stream_name = f"hal_{session_id}"

        if not await client.register_stream(stream_name, rtsp_url):
            return f"Failed to register RTSP stream with go2rtc"

        async def safety_stop():
            try:
                await asyncio.sleep(duration_s)
                await _stop_active_stream(state)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        state.active_stream = {
            "session_id": session_id,
            "kind": "rtsp",
            "rtsp_url": rtsp_url,
            "go2rtc_name": stream_name,
            "safety_task": asyncio.create_task(safety_stop()),
        }

        try:
            await ws.send_json({
                "type": "stream_start",
                "session_id": session_id,
                "rtsp_url": rtsp_url,
                # go2rtc's HTTP API is non-trickle: kiosk waits for ICE
                # gathering to complete before sending its bundled offer.
                "mode": "non-trickle",
            })
        except Exception as e:
            await _stop_active_stream(state, notify_kiosk=False)
            return f"Failed to notify kiosk: {e}"

        return f"Streaming RTSP for up to {duration_s} seconds — say 'stop streaming' to end early"

    tools.register(
        "stream_rtsp",
        "Start a live WebRTC video stream from any RTSP URL (any IP camera, NVR, Frigate go2rtc, etc.) and display it inside HAL's orb. Use when the user gives an explicit rtsp:// or rtsps:// URL, or asks to 'stream the RTSP at <url>'. The stream stays up to duration_s seconds (default 300 = 5 minutes); end early with 'stop streaming'. Replaces any active snapshot, image, or stream. RTSP credentials may be inline in the URL (rtsp://user:pass@host/path).",
        {
            "type": "object",
            "properties": {
                "rtsp_url": {"type": "string", "description": "RTSP URL, e.g. rtsp://user:pass@10.0.0.20:554/stream1"},
                "duration_s": {"type": "integer", "minimum": 10, "maximum": 1800, "default": 300, "description": "Maximum stream duration in seconds (safety auto-stop)"},
            },
            "required": ["rtsp_url"],
        },
        stream_rtsp,
    )

    async def play_video(args: dict) -> str:
        url = (args.get("url") or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            return "url must start with http:// or https://"
        try:
            duration_s = args.get("duration_s")
            duration_s = int(duration_s) if duration_s is not None else None
        except (TypeError, ValueError):
            duration_s = None
        if duration_s is not None:
            duration_s = max(1, min(7200, duration_s))
        loop = bool(args.get("loop", False))
        muted = bool(args.get("muted", False))
        ok = await _dispatch_play_video(
            state, url, duration_s=duration_s, loop=loop, muted=muted
        )
        if not ok:
            return "RPi not connected — kiosk did not receive the play_video message"
        bits = []
        if loop:
            bits.append("looping")
        if muted:
            bits.append("muted")
        if duration_s:
            bits.append(f"max {duration_s}s")
        suffix = f" ({', '.join(bits)})" if bits else ""
        return f"Playing video on the orb{suffix} — say 'stop streaming' to dismiss"

    tools.register(
        "play_video",
        "Play an HTTP(S) video (MP4, WebM, or HLS .m3u8 playlist) inside HAL's orb. Use when the user asks to 'play <video>', 'show this video', 'put on a movie'. Plays once and auto-stops on the video's end event unless `loop=true`. Optional `duration_s` is a hard cap. Audio plays unless `muted=true`; HAL's TTS auto-ducks the video while speaking. Replaces any active snapshot, image, or live stream. End early with stop_streaming.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Public http(s) URL of the video file or HLS playlist"},
                "duration_s": {"type": "integer", "minimum": 1, "maximum": 7200, "description": "Optional hard cap on playback duration"},
                "loop": {"type": "boolean", "default": False, "description": "Loop forever until stopped"},
                "muted": {"type": "boolean", "default": False, "description": "Start muted"},
            },
            "required": ["url"],
        },
        play_video,
    )

    async def stop_streaming(_args: dict) -> str:
        # Always tell the kiosk to clear video too — videos are kiosk-only
        # state, so state.active_stream tells us nothing about them.
        had_stream = state.active_stream is not None
        label = ""
        if state.active_stream:
            label = state.active_stream.get("entity_id") or state.active_stream.get("rtsp_url") or ""
        await _stop_active_stream(state)
        await _stop_active_video(state)
        if had_stream:
            return f"Stopped streaming {label}" if label else "Stopped video stream"
        return "Stopped any active video on the orb"

    tools.register(
        "stop_streaming",
        "Stop the live camera stream currently playing inside HAL's orb. Call this when the user says any of: 'stop streaming', 'stop the video', 'stop the camera', 'don't show the video anymore', 'hide the live view', 'turn off the camera feed', or anything else that asks to dismiss the live feed.",
        {"type": "object", "properties": {}},
        stop_streaming,
    )

    async def show_calendar_tool(args: dict) -> str:
        view = (args.get("view") or "month").lower().strip()
        if view not in _VALID_CAL_VIEWS:
            return f"view must be one of {_VALID_CAL_VIEWS}"
        calendar_name = (args.get("calendar_name") or "").strip()
        anchor_date = (args.get("anchor_date") or "").strip()
        duration_s = args.get("duration_s")
        try:
            duration_s = int(duration_s) if duration_s is not None else None
        except (TypeError, ValueError):
            duration_s = None
        payload = await _show_calendar(
            state,
            view=view,
            calendar_name=calendar_name,
            duration_s=duration_s,
            anchor_date=anchor_date,
        )
        n = len(payload.get("events") or [])
        src = payload.get("source_label") or "all calendars"
        return f"Showing {view} calendar from {src} for {payload.get('title','?')} ({n} events, {payload['duration_s']}s)"

    tools.register(
        "show_calendar",
        (
            "Display a full-screen calendar overlay on the kiosk (month, week, or "
            "day view) and pull events from Home Assistant. "
            "Use when the user asks 'show me my <name> calendar', 'show the calendar', "
            "'what's on this week', 'show me tomorrow's calendar', "
            "'show me my Family calendar for next Friday', 'what does May 25 look "
            "like', 'show last Monday', etc. "
            "Pass `calendar_name` with the source name (e.g. 'Family', 'Work') — "
            "partial / case-insensitive match against HA calendar entities. "
            "Empty `calendar_name` shows all calendars merged. "
            "Pass `anchor_date` as an ISO date string (YYYY-MM-DD) when the user "
            "names a specific day, week, or month other than today — e.g. for "
            "'tomorrow' compute tomorrow's date from the current date in your "
            "system prompt; for 'next Friday' or 'May 25' compute the absolute "
            "date. With view=day, the overlay shows that single day; with "
            "view=week, the Mon-Sun week containing that date; with view=month, "
            "the whole month containing that date. Omit `anchor_date` for "
            "today / this week / this month. "
            "The overlay rotates back to the orb after duration_s (default from "
            "runtime_config) and is preempted by show_camera / show_image / "
            "play_video / stream_camera."
        ),
        {
            "type": "object",
            "properties": {
                "view": {"type": "string", "enum": list(_VALID_CAL_VIEWS), "default": "month", "description": "Calendar view"},
                "calendar_name": {"type": "string", "description": "Optional source calendar name. Empty = merge all HA calendars."},
                "anchor_date": {"type": "string", "description": "Optional ISO date (YYYY-MM-DD) to anchor a specific day/week/month instead of today. Use this for 'tomorrow', 'next Friday', 'May 25', 'last week', etc."},
                "duration_s": {"type": "integer", "minimum": 5, "maximum": 600, "description": "How long to show before auto-dismissing"},
            },
        },
        show_calendar_tool,
    )

    async def hide_calendar_tool(_args: dict) -> str:
        await _hide_calendar(state)
        return "Calendar hidden"

    tools.register(
        "hide_calendar",
        "Dismiss the calendar overlay if it is currently shown on the kiosk. Use when the user says 'hide the calendar', 'close the calendar', 'go back to the orb', etc.",
        {"type": "object", "properties": {}},
        hide_calendar_tool,
    )

    async def show_photo_frame_tool(args: dict) -> str:
        from .photo_frame import start_photo_frame
        entity_id = (args.get("entity_id") or "").strip()
        result = await start_photo_frame(state, entity_id=entity_id)
        status = result.get("status")
        if status == "not_configured":
            return (
                "Photo frame entity is not configured. Set photo_frame_entity in "
                "the HAL device's Configuration (or pass entity_id) to use this."
            )
        if status == "invalid_entity":
            return f"{entity_id!r} is not an image.* / camera.* entity — photo frame skipped."
        if status == "fetch_failed":
            return "I couldn't fetch that image from Home Assistant."
        if status == "already_active":
            return "Photo frame was already showing — refreshed the image."
        return "Photo frame shown on the kiosk."

    tools.register(
        "show_photo_frame",
        (
            "Display an ambient full-screen photo frame on the kiosk: a slow "
            "Ken-Burns image from a configurable Home Assistant image.* entity "
            "with the wall clock overlaid in white. Use when the user asks to "
            "'show the photo frame', 'start the photo display', 'show my "
            "pictures', 'put up the slideshow'. Pass `entity_id` only if the "
            "user names a specific image entity; otherwise omit it and the "
            "server uses the configured default (text.<id>_config_photo_frame_entity). "
            "If neither is set, the tool no-ops and tells you so — do NOT "
            "treat that as an error to the user, just explain the entity isn't "
            "configured. The photo frame dismisses itself automatically the "
            "moment ANY kiosk action happens (wake word, PTT, volume, touch, "
            "another overlay), so it's safe to leave running indefinitely."
        ),
        {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "Optional HA image.* or camera.* entity_id. Omit to use the configured default.",
                },
            },
        },
        show_photo_frame_tool,
    )

    async def hide_photo_frame_tool(_args: dict) -> str:
        from .photo_frame import stop_photo_frame
        result = await stop_photo_frame(state, reason="explicit")
        if result.get("status") == "not_active":
            return "Photo frame wasn't showing — nothing to hide."
        return "Photo frame hidden."

    tools.register(
        "hide_photo_frame",
        "Dismiss the photo frame if it is currently shown on the kiosk. Use when the user says 'hide the photo frame', 'stop the slideshow', 'close the pictures', etc.",
        {"type": "object", "properties": {}},
        hide_photo_frame_tool,
    )

    return tools


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize all services on startup, clean up on shutdown."""
    state = AppState()
    app.state.hal = state

    log.info("Initializing HAL voice server...")

    # Generate wake word chime
    # Plug-in theme registry: scan the mounted themes dir.
    themes_dir = os.environ.get("THEMES_DIR", "/app/themes")
    state.themes = ThemeRegistry(themes_dir)
    state.themes.scan()
    log.info(f"Loaded {len(state.themes.themes)} themes from {themes_dir}: {state.themes.names}")

    # Live config: file wins over env vars; bootstrapped from env on first run
    runtime_config_path = os.environ.get("RUNTIME_CONFIG_PATH", "/app/runtime/config.json")
    state.runtime_config = RuntimeConfig(runtime_config_path)
    cfg = state.runtime_config.load()
    log.info(f"Runtime config loaded from {runtime_config_path}: {cfg}")
    state.theme_day = cfg.get("theme_day", "birch")
    state.theme_night = cfg.get("theme_night", "dark")
    state.auto_theme = bool(cfg.get("auto_theme", True))
    state.current_theme = state.theme_night  # start safe (dark)
    state.sendspin_player_entity = os.environ.get("SENDSPIN_PLAYER_ENTITY", "").strip()
    state.sendspin_pause_during_tts = (
        os.environ.get("SENDSPIN_PAUSE_DURING_TTS", "false").lower() in ("true", "1", "yes")
    )
    state.wake_chime = _generate_chime()
    log.info(f"Wake chime generated ({len(state.wake_chime)} bytes)")

    # MCP servers — load from config file, fall back to env var
    state.mcp_client = MultiMCPClient()
    mcp_config_path = os.environ.get("MCP_SERVERS_FILE", "/app/mcp_servers.json")
    servers_loaded = False

    if os.path.isfile(mcp_config_path):
        try:
            with open(mcp_config_path) as f:
                servers = json.load(f)
            for entry in servers:
                name = entry.get("name", "unnamed")
                url = entry.get("url", "")
                command = entry.get("command", "")
                args = entry.get("args", [])
                env = entry.get("env", {})
                if url or command:
                    await state.mcp_client.add_server(name, url=url, command=command, args=args, env=env)
                    servers_loaded = True
        except Exception as e:
            log.error(f"Failed to load MCP servers config: {e}")

    # Backward compat: fall back to MCP_SERVER_URL env var
    if not servers_loaded:
        mcp_url = os.environ.get("MCP_SERVER_URL", "")
        if mcp_url:
            await state.mcp_client.add_server("home-assistant", mcp_url)
        else:
            log.warning("No MCP servers configured")

    # Register local tools (theme, volume, mute, sun-time helpers)
    state.local_tools = _build_local_tools(state)
    state.mcp_client.register_client("hal-local", state.local_tools)

    log.info(f"Total tools available: {len(state.mcp_client.tool_names)}")

    # Wyoming TTS
    tts_host = os.environ.get("WYOMING_TTS_HOST", "localhost")
    tts_port = int(os.environ.get("WYOMING_TTS_PORT", "10200"))
    tts_voice = os.environ.get("WYOMING_TTS_VOICE", "")
    state.tts_engine = TTSEngine(host=tts_host, port=tts_port, voice=tts_voice)
    await state.tts_engine.initialize()

    # Long-term memory (Shodh)
    memory_url = os.environ.get("MEMORY_URL", "http://shodh-memory:3030")
    memory_user = os.environ.get("MEMORY_USER_ID", "hal-default")
    memory_api_key = os.environ.get("MEMORY_API_KEY", "")
    state.memory_client = MemoryClient(base_url=memory_url, user_id=memory_user, api_key=memory_api_key)
    await state.memory_client.initialize()

    # Audio pipeline (VAD + transcription + speaker filter)
    sample_rate = int(os.environ.get("SAMPLE_RATE", "48000"))
    stt_engine = os.environ.get("STT_ENGINE", "whisper")
    stt_model = os.environ.get("STT_MODEL", "")
    state.pipeline = AudioPipeline(sample_rate=sample_rate, stt_engine=stt_engine, stt_model=stt_model)
    await state.pipeline.initialize()

    # Conversation manager
    system_prompt = ""
    prompt_file = os.environ.get("SYSTEM_PROMPT_FILE", "/app/system_prompt.txt")
    if os.path.isfile(prompt_file):
        with open(prompt_file, "r") as f:
            system_prompt = f.read().strip()
        log.info(f"Loaded system prompt from {prompt_file} ({len(system_prompt)} chars)")
    else:
        log.info("No system prompt file found, using default")
    state.conversation = ConversationManager(
        wake_word=cfg.get("wake_word", "hey hal"),
        ollama_host=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
        ollama_model=cfg.get("ollama_model", "llama3.2"),
        mcp_client=state.mcp_client,
        tts_engine=state.tts_engine,
        memory_client=state.memory_client,
        system_prompt=system_prompt,
        num_ctx=int(cfg.get("num_ctx", os.environ.get("OLLAMA_NUM_CTX", "32768")) or 32768),
        num_predict=int(os.environ.get("OLLAMA_NUM_PREDICT", "512")),
        fallback_ollama_model=str(cfg.get("fallback_ollama_model", "") or ""),
    )

    # Apply tts_voice from runtime config (overrides whatever the engine
    # was initialized with from env).
    voice = cfg.get("tts_voice", "")
    if state.tts_engine is not None:
        state.tts_engine.voice = (voice or "").strip()

    # Discover Wyoming voices and Ollama models for the HA select dropdowns.
    if state.tts_engine is not None:
        try:
            state.voice_options = await state.tts_engine.list_voices()
        except Exception as e:
            log.debug(f"voice listing failed: {e}")
            state.voice_options = []
    state.model_options = await _fetch_ollama_tags(
        os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    )

    # Daily dusk/dawn theme scheduler
    if state.auto_theme:
        state.theme_scheduler_task = asyncio.create_task(_theme_scheduler(state))
    else:
        state.theme_scheduler_task = None

    # Theme registry: poll for added/removed plug-ins. The on_change
    # callback is wired further down (after the MQTT bridge is up) so
    # both the bridge republish and the kiosk notification can fire.
    poll_interval = float(os.environ.get("THEMES_POLL_INTERVAL_S", "10"))
    await state.themes.start_polling(poll_interval)

    # MQTT bridge → expose HAL as a HA device
    mqtt_host = os.environ.get("MQTT_BROKER_HOST", "")
    if mqtt_host:
        bridge = MQTTBridge(
            host=mqtt_host,
            port=int(os.environ.get("MQTT_BROKER_PORT", "1883")),
            username=os.environ.get("MQTT_USERNAME", ""),
            password=os.environ.get("MQTT_PASSWORD", ""),
            device_id=os.environ.get("HAL_DEVICE_ID", "hal-default"),
            device_name=os.environ.get("HAL_DEVICE_NAME", "HAL"),
        )
        # Wire MQTT commands to existing helpers
        async def _mqtt_volume(level: float):
            await _push_to_rpi(state, {"type": "volume", "level": level})
            state.tts_volume = level
            await bridge.publish_volume(level)

        async def _mqtt_mute(target: bool):
            # Only toggle if the cached state differs (avoid flapping)
            if state.mic_muted != target:
                await _push_to_rpi(state, {"type": "mute_toggle"})

        async def _mqtt_theme(theme: str):
            await apply_theme(state, theme)

        async def _mqtt_speak(text: str):
            if not text.strip() or not state.tts_engine:
                return
            ws = state.audio_websocket
            if not ws:
                log.warning("MQTT speak ignored — RPi not connected")
                return
            audio_bytes = await state.tts_engine.synthesize(text)
            if not audio_bytes:
                return
            try:
                # Show in the transcript scroll (persistent history)
                transcript_msg = {
                    "type": "transcription",
                    "text": text,
                    "is_partial": False,
                    "speaker": "ai",
                }
                await ws.send_json(transcript_msg)
                await broadcast_to_ui(state, transcript_msg)
                # And in the response panel (large, transient)
                msg = {"type": "response", "text": text}
                await ws.send_json(msg)
                await broadcast_to_ui(state, msg)
                await state.mqtt_bridge.publish_last_response(text)
                await ws.send_json({"type": "tts_start", "size": len(audio_bytes)})
                if state.pipeline:
                    state.pipeline.set_ai_speaking(True)
                chunk_size = 8192
                for i in range(0, len(audio_bytes), chunk_size):
                    await ws.send_bytes(audio_bytes[i : i + chunk_size])
                await ws.send_json({"type": "tts_end"})
            except Exception as e:
                log.error(f"MQTT speak failed: {e}")
                if state.pipeline:
                    state.pipeline.set_ai_speaking(False)

        async def _mqtt_image_set(payload):
            try:
                result = await _push_image_payload(state, payload, default_duration=60)
                log.info(f"MQTT image: {result}")
            except Exception as e:
                log.error(f"MQTT image dispatch failed: {e}")

        async def _mqtt_rtsp_set(payload: str):
            text = payload.strip()
            if not text:
                return
            url = ""
            duration_s = 300
            if text.startswith("{"):
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as e:
                    log.warning(f"MQTT rtsp/set: invalid JSON: {e}")
                    return
                url = str(data.get("url") or data.get("rtsp_url") or "")
                try:
                    duration_s = int(data.get("duration_s", 300))
                except (TypeError, ValueError):
                    pass
            elif text.startswith("rtsp://") or text.startswith("rtsps://"):
                url = text
            else:
                log.warning("MQTT rtsp/set: payload is neither rtsp URL nor JSON")
                return
            if not state.local_tools:
                return
            result = await state.local_tools.call_tool(
                "stream_rtsp", {"rtsp_url": url, "duration_s": duration_s}
            )
            log.info(f"MQTT rtsp: {result}")

        async def _mqtt_camera_set(payload: str):
            text = payload.strip()
            if not text:
                return
            entity_id = ""
            live = False
            duration_s: int | None = None
            if text.startswith("{"):
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as e:
                    log.warning(f"MQTT camera/set: invalid JSON: {e}")
                    return
                entity_id = str(data.get("entity_id") or "").strip()
                live = bool(data.get("live", False))
                if "duration_s" in data:
                    try:
                        duration_s = int(data["duration_s"])
                    except (TypeError, ValueError):
                        pass
            elif text.startswith("camera.") or text.startswith("image."):
                entity_id = text
            else:
                log.warning("MQTT camera/set: payload is neither camera.*/image.* entity_id nor JSON")
                return
            if not state.local_tools:
                return
            if not (entity_id.startswith("camera.") or entity_id.startswith("image.")):
                return
            # Live streaming only makes sense for camera.* — ignore the
            # `live` flag silently for image.* entities and fall back to
            # the snapshot path.
            if live and entity_id.startswith("image."):
                log.info(f"MQTT camera/set: ignoring live=true for image entity {entity_id}")
                live = False
            tool = "stream_camera" if live else "show_camera"
            args: dict = {"entity_id": entity_id}
            if duration_s is not None:
                args["duration_s"] = duration_s
            result = await state.local_tools.call_tool(tool, args)
            log.info(f"MQTT camera ({tool}): {result}")

        async def _mqtt_video_set(payload: str):
            text = payload.strip()
            if not text:
                return
            args: dict = {}
            if text.startswith("{"):
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as e:
                    log.warning(f"MQTT video/set: invalid JSON: {e}")
                    return
                args["url"] = str(data.get("url") or "")
                if "duration_s" in data:
                    args["duration_s"] = data["duration_s"]
                if "loop" in data:
                    args["loop"] = data["loop"]
                if "muted" in data:
                    args["muted"] = data["muted"]
            elif text.startswith("http://") or text.startswith("https://"):
                args["url"] = text
            else:
                log.warning("MQTT video/set: payload is neither http(s) URL nor JSON")
                return
            if not state.local_tools or not args.get("url"):
                return
            result = await state.local_tools.call_tool("play_video", args)
            log.info(f"MQTT video: {result}")

        async def _mqtt_command(text: str):
            text = text.strip()
            conversation = state.conversation
            if not text or not conversation:
                return
            log.info(f"MQTT command: {text!r}")
            transcription_msg = {
                "type": "transcription",
                "text": text,
                "is_partial": False,
                "speaker": "human",
            }
            await broadcast_to_ui(state, transcription_msg)
            if state.audio_websocket:
                try:
                    await state.audio_websocket.send_json(transcription_msg)
                except Exception:
                    pass
            conversation._command_buffer.append(text)
            conversation._wake_detected = True
            asyncio.create_task(conversation.on_silence())

        # --- Live runtime-config handlers ---

        async def _persist(key: str, value):
            if state.runtime_config is not None:
                state.runtime_config.set(key, value)

        async def _re_evaluate_active_theme():
            """Recompute which theme should be active and push if changed."""
            if not state.auto_theme:
                return
            sun_state = await _query_sun_state(state)
            target = state.theme_night if sun_state == "below_horizon" else state.theme_day
            if target and target != state.current_theme:
                await apply_theme(state, target)

        async def _cfg_theme_day(name: str):
            if not _is_valid_theme(state, name):
                log.warning(f"config theme_day rejected: {name!r}")
                return
            state.theme_day = name
            await _persist("theme_day", name)
            await bridge.publish_config_theme_day(name)
            await _re_evaluate_active_theme()

        async def _cfg_theme_night(name: str):
            if not _is_valid_theme(state, name):
                log.warning(f"config theme_night rejected: {name!r}")
                return
            state.theme_night = name
            await _persist("theme_night", name)
            await bridge.publish_config_theme_night(name)
            await _re_evaluate_active_theme()

        async def _cfg_tts_voice(name: str):
            if state.voice_options and name not in state.voice_options:
                log.warning(f"config tts_voice rejected: {name!r} not in {state.voice_options}")
                return
            if state.tts_engine is not None:
                state.tts_engine.voice = name
            await _persist("tts_voice", name)
            await bridge.publish_config_tts_voice(name)

        async def _cfg_ollama_model(name: str):
            if state.model_options and name not in state.model_options:
                log.warning(f"config ollama_model rejected: {name!r} not in {state.model_options}")
                return
            if state.conversation is not None:
                state.conversation.ollama_model = name
            await _persist("ollama_model", name)
            await bridge.publish_config_ollama_model(name)
            # Refresh the num_ctx ceiling so the HA Number entity reflects
            # the new model's true context capacity (republishes discovery).
            ctx_max = await _fetch_ollama_context_length(
                os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
                name,
            )
            if ctx_max:
                await bridge.update_num_ctx_max(ctx_max)

        async def _cfg_fallback_ollama_model(name: str):
            # Empty string = no fallback. Anything non-empty must be in
            # the discovered model list (when we have one).
            name = (name or "").strip()
            if name and state.model_options and name not in state.model_options:
                log.warning(
                    f"config fallback_ollama_model rejected: {name!r} "
                    f"not in {state.model_options}"
                )
                return
            if state.conversation is not None:
                state.conversation.fallback_ollama_model = name
            await _persist("fallback_ollama_model", name)
            await bridge.publish_config_fallback_ollama_model(name)

        async def _cfg_num_ctx(value: int):
            try:
                n = int(value)
            except (TypeError, ValueError):
                n = 32768
            n = max(2048, min(int(bridge.num_ctx_max), n))
            if state.conversation is not None:
                state.conversation.num_ctx = n
            await _persist("num_ctx", n)
            await bridge.publish_config_num_ctx(n)

        async def _cfg_wake_word(value: str):
            value = (value or "").strip()
            if state.conversation is not None:
                state.conversation.wake_word = value.lower()
            await _persist("wake_word", value)
            await bridge.publish_config_wake_word(value)

        async def _cfg_auto_theme(enabled: bool):
            state.auto_theme = bool(enabled)
            await _persist("auto_theme", state.auto_theme)
            await bridge.publish_config_auto_theme(state.auto_theme)
            # Start/stop the scheduler to match the new value.
            existing = getattr(state, "theme_scheduler_task", None)
            if state.auto_theme and (existing is None or existing.done()):
                state.theme_scheduler_task = asyncio.create_task(_theme_scheduler(state))
            elif not state.auto_theme and existing is not None and not existing.done():
                existing.cancel()
                state.theme_scheduler_task = None

        bridge.voice_options = list(state.voice_options)
        bridge.model_options = list(state.model_options)
        # Theme options come straight from the plug-in registry. Order
        # them so dark themes sit before light themes; alphabetic within
        # each kind.
        def _ordered_theme_names() -> list[str]:
            if not isinstance(state.themes, ThemeRegistry):
                return sorted(VALID_THEMES)
            dark = sorted(t.name for t in state.themes.themes if t.kind == "dark")
            light = sorted(t.name for t in state.themes.themes if t.kind != "dark")
            return dark + light

        bridge.theme_options = _ordered_theme_names()

        async def _on_themes_changed(_themes_list):
            """Refresh bridge options + republish discovery + tell kiosks."""
            bridge.theme_options = _ordered_theme_names()
            try:
                await bridge.publish_discovery()
            except Exception as e:
                log.warning(f"themes: republish discovery failed: {e}")
            msg = {"type": "themes_changed"}
            ws = state.audio_websocket
            if ws:
                try:
                    await ws.send_json(msg)
                except Exception:
                    pass
            await broadcast_to_ui(state, msg)

        if isinstance(state.themes, ThemeRegistry):
            state.themes.add_listener(_on_themes_changed)
        # Seed the bridge's caches with the current values so the first
        # publish on connect reflects reality.
        bridge._cached_config_theme_day = state.theme_day
        bridge._cached_config_theme_night = state.theme_night
        bridge._cached_config_tts_voice = (
            state.tts_engine.voice if state.tts_engine is not None else ""
        )
        bridge._cached_config_wake_word = (
            state.conversation.wake_word if state.conversation is not None else ""
        )
        bridge._cached_config_ollama_model = (
            state.conversation.ollama_model if state.conversation is not None else ""
        )
        bridge._cached_config_fallback_ollama_model = (
            state.conversation.fallback_ollama_model if state.conversation is not None else ""
        )
        bridge._cached_config_num_ctx = int(
            state.conversation.num_ctx if state.conversation is not None
            else state.runtime_config.get("num_ctx", 32768) or 32768
        )
        # Seed num_ctx_max from the current model's native ceiling so the
        # first publish_discovery() uses the right `max`.
        current_model = (
            state.conversation.ollama_model
            if state.conversation is not None else ""
        )
        ctx_max = await _fetch_ollama_context_length(
            os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
            current_model,
        )
        if ctx_max:
            bridge.num_ctx_max = ctx_max
            # Clamp the cached current value to the ceiling we just learned.
            if bridge._cached_config_num_ctx > ctx_max:
                bridge._cached_config_num_ctx = ctx_max
                if state.conversation is not None:
                    state.conversation.num_ctx = ctx_max
                await _persist("num_ctx", ctx_max)
        bridge._cached_config_auto_theme = state.auto_theme
        bridge._cached_config_calendar_default_source = str(
            state.runtime_config.get("calendar_default_source", "") or ""
        )
        bridge._cached_config_calendar_dismiss_seconds = int(
            state.runtime_config.get("calendar_dismiss_seconds", 30) or 30
        )
        bridge._cached_config_start_muted = bool(
            state.runtime_config.get("start_muted", False)
        )
        bridge._cached_config_photo_frame_entity = str(
            state.runtime_config.get("photo_frame_entity", "") or ""
        )

        async def _mqtt_calendar_show(args: dict):
            view = (args.get("view") or "month").lower()
            calendar_name = (args.get("calendar_name") or "").strip()
            anchor_date = (args.get("anchor_date") or "").strip()
            duration_s = args.get("duration_s")
            try:
                duration_s = int(duration_s) if duration_s is not None else None
            except (TypeError, ValueError):
                duration_s = None
            await _show_calendar(
                state,
                view=view,
                calendar_name=calendar_name,
                duration_s=duration_s,
                anchor_date=anchor_date,
            )

        async def _mqtt_calendar_hide():
            await _hide_calendar(state)

        async def _mqtt_photo_frame_show(args: dict):
            from .photo_frame import start_photo_frame
            entity_id = (args.get("entity_id") or "").strip()
            await start_photo_frame(state, entity_id=entity_id)

        async def _mqtt_photo_frame_hide():
            from .photo_frame import stop_photo_frame
            await stop_photo_frame(state, reason="mqtt")

        async def _cfg_photo_frame_entity(value: str):
            value = (value or "").strip()
            await _persist("photo_frame_entity", value)
            await bridge.publish_config_photo_frame_entity(value)

        async def _cfg_calendar_default_source(value: str):
            value = (value or "").strip()
            await _persist("calendar_default_source", value)
            await bridge.publish_config_calendar_default_source(value)

        async def _cfg_calendar_dismiss_seconds(seconds: int):
            try:
                seconds = max(5, min(600, int(seconds)))
            except (TypeError, ValueError):
                seconds = 30
            await _persist("calendar_dismiss_seconds", seconds)
            await bridge.publish_config_calendar_dismiss_seconds(seconds)

        async def _cfg_start_muted(enabled: bool):
            await _persist("start_muted", bool(enabled))
            await bridge.publish_config_start_muted(bool(enabled))
            # Apply right now too: push the desired state to the RPi if
            # connected, so flipping the HA switch takes effect without
            # waiting for the next reconnect.
            ws = state.audio_websocket
            if ws:
                try:
                    await ws.send_json({"type": "mute_set", "muted": bool(enabled)})
                except Exception as e:
                    log.warning(f"start_muted: could not push mute_set to RPi: {e}")

        bridge.on_volume_set = _mqtt_volume
        bridge.on_mute_set = _mqtt_mute
        bridge.on_theme_set = _mqtt_theme
        bridge.on_speak = _mqtt_speak
        bridge.on_command = _mqtt_command
        bridge.on_image_set = _mqtt_image_set
        bridge.on_rtsp_set = _mqtt_rtsp_set
        bridge.on_video_set = _mqtt_video_set
        bridge.on_camera_set = _mqtt_camera_set
        bridge.on_config_theme_day = _cfg_theme_day
        bridge.on_config_theme_night = _cfg_theme_night
        bridge.on_config_tts_voice = _cfg_tts_voice
        bridge.on_config_ollama_model = _cfg_ollama_model
        bridge.on_config_fallback_ollama_model = _cfg_fallback_ollama_model
        bridge.on_config_num_ctx = _cfg_num_ctx
        bridge.on_config_wake_word = _cfg_wake_word
        bridge.on_config_auto_theme = _cfg_auto_theme
        bridge.on_calendar_show = _mqtt_calendar_show
        bridge.on_calendar_hide = _mqtt_calendar_hide
        bridge.on_photo_frame_show = _mqtt_photo_frame_show
        bridge.on_photo_frame_hide = _mqtt_photo_frame_hide
        bridge.on_config_photo_frame_entity = _cfg_photo_frame_entity
        bridge.on_config_calendar_default_source = _cfg_calendar_default_source
        bridge.on_config_calendar_dismiss_seconds = _cfg_calendar_dismiss_seconds
        bridge.on_config_start_muted = _cfg_start_muted

        async def _mqtt_ptt_start():
            from .ptt import start_ptt
            await start_ptt(state)

        async def _mqtt_ptt_end():
            from .ptt import end_ptt
            await end_ptt(state)

        async def _mqtt_ptt_cancel():
            from .ptt import end_ptt
            await end_ptt(state, cancel=True)

        bridge.on_ptt_start = _mqtt_ptt_start
        bridge.on_ptt_end = _mqtt_ptt_end
        bridge.on_ptt_cancel = _mqtt_ptt_cancel

        await bridge.start()
        state.mqtt_bridge = bridge
        log.info(f"MQTT bridge started for broker {mqtt_host}")
    else:
        log.info("MQTT bridge disabled (set MQTT_BROKER_HOST to enable)")

    log.info("HAL voice server ready.")

    yield

    # Shutdown
    if getattr(state, "theme_scheduler_task", None):
        state.theme_scheduler_task.cancel()
    if state.active_stream:
        await _stop_active_stream(state, notify_kiosk=False)
    if isinstance(state.ha_ws, HAWSClient):
        await state.ha_ws.disconnect()
    if state.mqtt_bridge:
        await state.mqtt_bridge.stop()
    if state.mcp_client:
        await state.mcp_client.disconnect_all()


app = FastAPI(title="HAL Voice Server", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _get_state(app: FastAPI) -> AppState:
    """Retrieve HAL state from the FastAPI app."""
    return app.state.hal


async def broadcast_to_ui(state: AppState, msg: dict):
    """Send a message to all connected UI WebSocket clients."""
    data = json.dumps(msg)
    dead = set()
    for ws in state.ui_clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.add(ws)
    state.ui_clients.difference_update(dead)


@app.websocket("/ws/audio")
async def audio_endpoint(websocket: WebSocket):
    """
    Main audio WebSocket for the RPi audio streamer.

    Receives: raw PCM 16-bit LE audio chunks
    Sends:    JSON messages (transcription, state) and binary (TTS audio)
    """
    state = _get_state(websocket.app)
    pipeline = state.pipeline
    conversation = state.conversation

    await websocket.accept()
    state.audio_websocket = websocket
    log.info("Audio client connected")

    # Push the configured "start muted" state so a freshly-booted
    # audio_streamer (which always inits mic_muted=False) lands in the
    # state the user configured rather than always unmuted.
    try:
        start_muted = bool(state.runtime_config.get("start_muted", False))
        await websocket.send_json({"type": "mute_set", "muted": start_muted})
        log.info(f"Pushed initial mute state to RPi: {start_muted}")
    except Exception as e:
        log.warning(f"Could not push initial mute state to RPi: {e}")

    async def on_wake_word():
        """Callback: play chime on RPi and flash the UI."""
        try:
            await broadcast_to_ui(state, {"type": "wake"})
            await websocket.send_json({"type": "wake"})
            if state.wake_chime:
                await websocket.send_json({"type": "chime_start", "size": len(state.wake_chime)})
                await websocket.send_bytes(state.wake_chime)
                await websocket.send_json({"type": "chime_end"})
        except Exception as e:
            log.error(f"Error sending wake chime: {e}")

    async def on_response(text: str, audio_bytes: bytes | None):
        """Callback: send LLM response + TTS audio back to RPi."""
        try:
            await websocket.send_json({"type": "response", "text": text})
            await broadcast_to_ui(state, {"type": "response", "text": text})
            if state.mqtt_bridge:
                await state.mqtt_bridge.publish_last_response(text)
            if audio_bytes:
                await websocket.send_json({"type": "tts_start", "size": len(audio_bytes)})
                pipeline.set_ai_speaking(True)
                log.info(f"AI speaking: True (sending {len(audio_bytes)} bytes TTS)")
                chunk_size = 8192
                for i in range(0, len(audio_bytes), chunk_size):
                    await websocket.send_bytes(audio_bytes[i : i + chunk_size])
                await websocket.send_json({"type": "tts_end"})
        except Exception as e:
            log.error(f"Error sending response: {e}")
            # Ensure ai_speaking is cleared even on send failure
            pipeline.set_ai_speaking(False)
            log.info("AI speaking: False (error recovery)")

    async def on_state_change(new_state: str):
        """Callback: broadcast state changes to RPi, UI and MQTT."""
        msg = {"type": "state", "state": new_state}
        try:
            await websocket.send_json(msg)
        except Exception:
            pass
        await broadcast_to_ui(state, msg)
        if state.mqtt_bridge:
            await state.mqtt_bridge.publish_state(new_state)
        # Optional Shape C: explicitly pause/resume the Sendspin MA
        # player around TTS instead of relying on Pulse role-ducking.
        # Only resumes if we were the ones who paused it — never
        # overrides a user's manual pause.
        if state.sendspin_pause_during_tts and state.sendspin_player_entity:
            if new_state == "speaking":
                asyncio.create_task(_ma_pause_if_playing(state))
            elif new_state == "idle":
                asyncio.create_task(_ma_resume_if_we_paused(state))

    async def on_metrics(metrics: dict):
        if state.mqtt_bridge:
            await state.mqtt_bridge.publish_task_metrics(metrics)

    conversation.on_response = on_response
    conversation.on_wake_word = on_wake_word
    conversation.on_state_change = on_state_change
    conversation.on_metrics = on_metrics

    # Keepalive: ping RPi every 15s, detect dead connections
    last_pong = time.monotonic()
    ping_interval = 15.0
    pong_timeout = 45.0  # 3 missed pongs = dead

    async def _keepalive():
        nonlocal last_pong
        while True:
            await asyncio.sleep(ping_interval)
            try:
                await websocket.send_json({"type": "ping"})
                elapsed = time.monotonic() - last_pong
                if elapsed > pong_timeout:
                    log.error(f"No pong from RPi in {elapsed:.0f}s — connection likely dead")
            except Exception as e:
                log.error(f"Keepalive send failed: {e}")
                break

    # Watchdog: independent task that proves the event loop is alive
    chunk_at_last_watchdog = 0
    async def _watchdog():
        nonlocal chunk_at_last_watchdog
        while True:
            await asyncio.sleep(30)
            current_chunks = pipeline._chunk_count
            chunks_delta = current_chunks - chunk_at_last_watchdog
            chunk_at_last_watchdog = current_chunks
            log.info(f"Watchdog: event_loop=alive, chunks_delta={chunks_delta}/30s, "
                      f"ai_speaking={pipeline._ai_speaking}, "
                      f"vad_executor_alive={not pipeline._vad_executor._shutdown}, "
                      f"reload_pending={pipeline._reload_pending}")

    keepalive_task = asyncio.create_task(_keepalive())
    watchdog_task = asyncio.create_task(_watchdog())

    chunk_count = 0

    try:
        while True:
            log.debug("Waiting for WebSocket data...")
            data = await websocket.receive()
            chunk_count += 1

            if "bytes" in data:
                audio_chunk = data["bytes"]
                t0 = time.monotonic()
                try:
                    result = await asyncio.wait_for(
                        pipeline.process_chunk(audio_chunk),
                        timeout=10.0,
                    )
                except asyncio.TimeoutError:
                    log.error(f"process_chunk timed out (10s) at chunk #{chunk_count} — skipping")
                    continue

                elapsed = time.monotonic() - t0
                if elapsed > 1.0:
                    log.warning(f"process_chunk slow: {elapsed:.2f}s at chunk #{chunk_count}")

                if result:
                    transcription_msg = {
                        "type": "transcription",
                        "text": result["text"],
                        "is_partial": result.get("is_partial", False),
                        "speaker": result.get("speaker", "unknown"),
                    }
                    await broadcast_to_ui(state, transcription_msg)
                    await websocket.send_json(transcription_msg)

                    if not result.get("is_partial", False) and result.get("speaker") != "ai":
                        await conversation.process_text(result["text"])

                    if result.get("silence_after", False):
                        asyncio.create_task(conversation.on_silence())

            elif "text" in data:
                msg = json.loads(data["text"])
                msg_type = msg.get("type")
                if msg_type == "tts_finished":
                    log.info("AI speaking: False (tts_finished received)")
                    pipeline.set_ai_speaking(False)
                elif msg_type == "pong":
                    last_pong = time.monotonic()
                    log.debug("Pong received from RPi")
                elif msg_type == "mute_sync":
                    state.mic_muted = bool(msg.get("muted", False))
                    log.debug(f"Cached mute state: {state.mic_muted}")
                    if state.mqtt_bridge:
                        await state.mqtt_bridge.publish_mute(state.mic_muted)
                elif msg_type == "volume_sync":
                    try:
                        state.tts_volume = max(0.0, min(1.0, float(msg.get("level", 0.7))))
                        if state.mqtt_bridge:
                            await state.mqtt_bridge.publish_volume(state.tts_volume)
                    except (TypeError, ValueError):
                        pass
                elif msg_type == "snapshot":
                    # RPi pushed a JPEG snapshot — forward to MQTT
                    pass  # snapshot is binary; handled via dedicated message below
                elif msg_type == "ma_volume_adjust":
                    try:
                        step = float(msg.get("step", 0.0))
                    except (TypeError, ValueError):
                        step = 0.0
                    if step != 0.0:
                        asyncio.create_task(_ma_volume_step(state, step))
                elif msg_type == "photo_frame_dismissed":
                    # Kiosk auto-dismissed (state change / volume / pointer /
                    # preempt). Tear down the HA subscription so we don't
                    # keep receiving state_changed events for nothing.
                    from .photo_frame import stop_photo_frame
                    reason = str(msg.get("reason") or "kiosk")
                    await stop_photo_frame(state, reason=reason)

                elif msg_type == "webrtc_signal":
                    # Kiosk → server signaling for an active stream session.
                    # offer is sent on stream start; candidate is sent during ICE.
                    session_id = str(msg.get("session_id", ""))
                    kind = msg.get("kind")
                    if kind == "offer" and msg.get("sdp"):
                        if state.active_stream and state.active_stream.get("session_id") == session_id:
                            stream_kind = state.active_stream.get("kind", "ha")
                            if stream_kind == "rtsp":
                                asyncio.create_task(
                                    _negotiate_rtsp_offer(state, session_id, msg["sdp"])
                                )
                            else:
                                entity_id = state.active_stream.get("entity_id", "")
                                asyncio.create_task(
                                    _start_webrtc_stream(state, entity_id, session_id, msg["sdp"])
                                )
                    elif kind == "candidate":
                        # Only HA streams use trickle ICE; go2rtc bundles candidates
                        # into the offer/answer so kiosk-side trickle is dropped.
                        if (
                            state.active_stream
                            and state.active_stream.get("session_id") == session_id
                            and state.active_stream.get("kind") != "rtsp"
                        ):
                            asyncio.create_task(_forward_kiosk_candidate(state, session_id, msg))

    except (WebSocketDisconnect, RuntimeError):
        log.info("Audio client disconnected")
    except Exception as e:
        log.error(f"Audio WebSocket error: {e}", exc_info=True)
    finally:
        state.audio_websocket = None
        keepalive_task.cancel()
        watchdog_task.cancel()


@app.websocket("/ws/ptt")
async def ptt_endpoint(websocket: WebSocket):
    """Persistent WebSocket for low-latency Push-to-Talk triggers.

    Apps that press the button repeatedly (e.g. a desktop client) keep
    one connection open and send JSON messages `{"type":"start"}`,
    `{"type":"end"}`, or `{"type":"cancel"}`. Each maps onto the same
    `start_ptt` / `end_ptt` server functions that drive the HTTP +
    MQTT trigger surfaces, so behaviour is identical."""
    from .ptt import start_ptt, end_ptt
    state = _get_state(websocket.app)
    await websocket.accept()
    log.info("PTT WebSocket client connected")
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = (msg.get("type") or "").lower()
            if t == "start":
                result = await start_ptt(state)
            elif t == "end":
                result = await end_ptt(state)
            elif t == "cancel":
                result = await end_ptt(state, cancel=True)
            else:
                result = {"status": "unknown_type", "type": t}
            try:
                await websocket.send_json(result)
            except Exception:
                pass
    except WebSocketDisconnect:
        log.info("PTT WebSocket client disconnected")
    except Exception as e:
        log.warning(f"PTT WebSocket error: {e}")


@app.websocket("/ws/ui")
async def ui_endpoint(websocket: WebSocket):
    """WebSocket for the web UI to receive live transcription and state updates."""
    state = _get_state(websocket.app)

    await websocket.accept()
    state.ui_clients.add(websocket)
    log.info(f"UI client connected (total: {len(state.ui_clients)})")

    if state.conversation:
        await websocket.send_json({
            "type": "state",
            "state": state.conversation.state,
            "wake_word": state.conversation.wake_word,
        })

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            if msg.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        state.ui_clients.discard(websocket)
        log.info(f"UI client disconnected (total: {len(state.ui_clients)})")


@app.get("/health")
async def health():
    state = _get_state(app)
    return {
        "status": "ok",
        "pipeline_ready": state.pipeline is not None,
        "mcp_connected": state.mcp_client is not None and len(state.mcp_client.tool_names) > 0,
        "tts_available": state.tts_engine is not None,
        "memory_available": state.memory_client is not None and state.memory_client.available,
    }


class CommandRequest(BaseModel):
    text: str


@app.post("/api/ptt/start")
async def post_ptt_start():
    """Open a Push-to-Talk session. See server/app/ptt.py for semantics."""
    from .ptt import start_ptt
    state = _get_state(app)
    return await start_ptt(state)


@app.post("/api/ptt/end")
async def post_ptt_end():
    """Close the active PTT session and run the LLM on whatever was captured."""
    from .ptt import end_ptt
    state = _get_state(app)
    return await end_ptt(state)


@app.post("/api/ptt/cancel")
async def post_ptt_cancel():
    """Close the active PTT session and DISCARD whatever was captured. Used
    when the trigger party knows the press was an accident."""
    from .ptt import end_ptt
    state = _get_state(app)
    return await end_ptt(state, cancel=True)


class PhotoFrameStartRequest(BaseModel):
    entity_id: str = ""


@app.post("/api/photo_frame/start")
async def post_photo_frame_start(req: PhotoFrameStartRequest | None = None):
    """Open the photo frame on the kiosk. Optional body overrides the
    configured default: `{"entity_id": "image.weather_radar"}`."""
    from .photo_frame import start_photo_frame
    state = _get_state(app)
    entity_id = (req.entity_id if req else "") or ""
    return await start_photo_frame(state, entity_id=entity_id)


@app.post("/api/photo_frame/end")
async def post_photo_frame_end():
    """Dismiss the active photo frame. No-op if none is open."""
    from .photo_frame import stop_photo_frame
    state = _get_state(app)
    return await stop_photo_frame(state, reason="explicit")


@app.post("/api/command")
async def post_command(req: CommandRequest):
    """
    Direct text command to the LLM, bypassing STT.

    The text is shown on the UI as a transcription and processed by
    the LLM without requiring a wake word. The TTS response plays
    on the RPi like any voice command.
    """
    state = _get_state(app)
    conversation = state.conversation
    text = req.text.strip()

    if not text:
        return {"status": "error", "message": "Empty text"}

    if not conversation:
        return {"status": "error", "message": "Server not ready"}

    log.info(f"REST command received: '{text[:80]}'")

    # Show on UI as transcription (send to both direct UI clients and RPi)
    transcription_msg = {
        "type": "transcription",
        "text": text,
        "is_partial": False,
        "speaker": "human",
    }
    await broadcast_to_ui(state, transcription_msg)
    if state.audio_websocket:
        try:
            await state.audio_websocket.send_json(transcription_msg)
        except Exception:
            pass

    # Feed directly to conversation manager (skip wake word)
    conversation._command_buffer.append(text)
    conversation._wake_detected = True  # bypass wake word check in on_silence

    # Process immediately as a background task (like on_silence)
    asyncio.create_task(conversation.on_silence())

    return {"status": "ok", "message": "Command received"}


class VolumeRequest(BaseModel):
    direction: str  # "up" or "down"
    step: float = 0.1


@app.post("/api/volume")
async def post_volume(req: VolumeRequest):
    """Adjust RPi volume up or down."""
    state = _get_state(app)
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


@app.post("/api/snapshot")
async def post_snapshot(request: Request):
    """Receive a JPEG snapshot from the RPi and forward to MQTT."""
    state = _get_state(app)
    body = await request.body()
    if not body:
        return {"status": "error", "message": "Empty body"}

    # Cache for GET /api/snapshot.jpg
    state.last_snapshot = body

    # Forward to MQTT for HA camera entity
    if state.mqtt_bridge:
        await state.mqtt_bridge.publish_snapshot(body)

    return {"status": "ok", "size": len(body)}


@app.get("/api/snapshot.jpg")
async def get_snapshot():
    """Return the most recent JPEG snapshot."""
    state = _get_state(app)
    if not state.last_snapshot:
        return Response(status_code=404)
    return Response(content=state.last_snapshot, media_type="image/jpeg")


class SpeakRequest(BaseModel):
    text: str


@app.post("/api/speak")
async def post_speak(req: SpeakRequest):
    """
    Speak text verbatim through the RPi speaker — bypasses the LLM.

    Useful for announcements, notifications, or anything that should be
    vocalized exactly without going through Steve's persona.
    """
    state = _get_state(app)
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


@app.post("/api/mute")
async def post_mute_toggle():
    """Toggle RPi mic mute. Returns the new mute state."""
    state = _get_state(app)
    ws = state.audio_websocket
    if not ws:
        return {"status": "error", "message": "RPi not connected"}

    try:
        await ws.send_json({"type": "mute_toggle"})
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/mute")
async def get_mute_status():
    """Get current RPi mic mute state."""
    state = _get_state(app)
    return {"muted": state.mic_muted}


# --- Theme plug-in endpoints -------------------------------------------------

@app.get("/api/themes")
async def list_themes_endpoint():
    """JSON list of installed themes. Kiosk fetches this on startup
    and on `themes_changed` to build the picker dropdown."""
    state = _get_state(app)
    if not isinstance(state.themes, ThemeRegistry):
        return {"themes": []}
    return {"themes": [t.to_public() for t in state.themes.themes]}


@app.get("/themes/{name}/{filename}")
async def get_theme_file(name: str, filename: str):
    """Serve a theme's static asset (theme.css or effect.js)."""
    from fastapi.responses import FileResponse, Response
    state = _get_state(app)
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
