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
from .local_tools_register import build_local_tools
from .runtime_config import RuntimeConfig
from .themes import ThemeRegistry
from .mcp_client import MCPClient, MultiMCPClient
from .media import (
    _detect_image_mime,
    _dispatch_play_audio,
    _dispatch_play_video,
    _dispatch_show_image,
    _fetch_image_url,
    _guess_mime_from_url,
    _handle_openclaw_media,
    _probe_mime,
    _push_image_payload,
    _resolve_media,
    _speak_proactively,
    _transcode_to_wav,
)
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
    # Active looping-video photo-frame session
    # (server/app/photo_frame.PhotoFrameVideoSession). Mutually exclusive
    # with photo_frame_session — only one frame mode is ever live.
    photo_frame_video_session: object | None = None
    # Live video-mode config + the sha256 of the currently cached loop
    # video. photo_frame_video_hash is "" when no video is cached; it gates
    # whether start_photo_frame picks the video path (see _video_available).
    photo_frame_video_url: str = ""
    photo_frame_video_mode: bool = False
    photo_frame_video_hash: str = ""
    # Display power (DPMS). state is "on" or "off"; auto_off_seconds
    # of 0 means "no idle blanking" (manual control only).
    display_state: str = "on"
    display_last_activity: float = 0.0
    display_auto_off_seconds: int = 0
    # Reports back whether any DPMS backend was found in the RPi
    # container. False = HA Display switch becomes a no-op and the
    # LLM tool returns "not supported on this kiosk".
    display_available: bool = True
    display_auto_off_task: object | None = None
    # Photo-frame idle auto-activation. Distinct from display idle:
    # bumped only by genuine user/system activity, NOT by the photo
    # frame itself opening (would re-arm forever). 0 = feature off.
    photo_frame_idle_minutes: int = 0
    user_last_activity: float = 0.0
    photo_frame_idle_task: object | None = None
    # OpenClaw integration
    openclaw_client: object | None = None  # OpenClawClient
    openclaw_enabled: bool = False
    openclaw_gateway_url: str = ""
    openclaw_workspace: str = ""
    # Display orientation
    display_orientation: str = "portrait"
    orb_side: str = "left"
    # Router model (Ollama-vs-OpenClaw fast-path decision)
    router_enabled: bool = False
    router_model: str = ""


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


from .display import (  # noqa: E402
    _dismiss_photo_frame_async,
    _display_auto_off_loop,
    _photo_frame_idle_loop,
    _record_kiosk_activity,
    _record_user_activity,
    _set_display,
)


# Photo-frame looping-video download/cache helpers live in photo_frame.py;
# re-exported here so existing callers (and tests) keep importing them from main.
from .photo_frame import (  # noqa: E402
    _clear_photo_frame_video_cache,
    _download_photo_frame_video,
    _load_photo_frame_video_hash,
    _photo_frame_video_cache_path,
    _photo_frame_video_sidecar_path,
    PHOTO_FRAME_VIDEO_MAX_BYTES,
)


# Sendspin media-player control lives in media_player.py; re-exported here so
# existing callers (and tests) keep importing it from main.
from .media_player import (  # noqa: E402
    _ma_call,
    _ma_pause_if_playing,
    _ma_query_state,
    _ma_resume_if_we_paused,
    _ma_volume_step,
)


# --- WebRTC streaming ---------------------------------------------------------

from .streaming import (  # noqa: E402
    _ensure_ha_ws,
    _ensure_go2rtc,
    _forward_kiosk_candidate,
    _negotiate_rtsp_offer,
    _on_ha_webrtc_event,
    _start_webrtc_stream,
    _stop_active_stream,
    _stop_active_video,
)


# Media dispatchers (_handle_openclaw_media, _speak_proactively, _dispatch_play_video,
# _dispatch_show_image, _dispatch_play_audio, _push_image_payload) and the media
# fetchers/transcoder live in .media — imported at the top of this file.


# --- Image push to orb -------------------------------------------------------

# Anything pushed to the kiosk uses the existing show_camera WS message. The
# kiosk doesn't care whether the bytes came from an HA camera, an MQTT topic,
# or an LLM tool — it just paints them inside the orb for duration_s seconds.

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


# Calendar overlay orchestration lives in calendar_ha.py; re-exported here so
# existing callers (and tests) keep importing them from main.
from .calendar_ha import (  # noqa: E402
    _calendar_range,
    _format_range_title,
    _hide_calendar,
    _parse_anchor_date,
    _show_calendar,
)


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
    state.local_tools = build_local_tools(state)
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
        router_enabled=bool(cfg.get("router_enabled", False)),
        router_model=str(cfg.get("router_model", "") or ""),
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

    # MCP server: expose HAL's tools to external MCP clients.
    from .mcp_server import build_mcp_server
    hal_mcp = build_mcp_server(state)
    app.mount("/mcp", hal_mcp.sse_app())
    log.info("MCP server mounted at /mcp")

    # OpenClaw: optional alternative conversation engine.
    # Client creation is deferred to avoid interfering with MCP client
    # startup. The MQTT callback or a background task will wire it up.
    state.openclaw_enabled = bool(cfg.get("openclaw_enabled", False))
    state.openclaw_gateway_url = str(cfg.get("openclaw_gateway_url", ""))
    state.openclaw_workspace = str(cfg.get("openclaw_workspace", ""))
    log.info(
        f"OpenClaw config: enabled={state.openclaw_enabled}, "
        f"url={state.openclaw_gateway_url or '(none)'}"
    )

    # Display orientation
    state.display_orientation = str(cfg.get("display_orientation", "portrait"))
    state.orb_side = str(cfg.get("orb_side", "left"))

    # Router model
    state.router_enabled = bool(cfg.get("router_enabled", False))
    state.router_model = str(cfg.get("router_model", "") or "")
    log.info(
        f"Router config: enabled={state.router_enabled}, "
        f"model={state.router_model or '(none)'}"
    )

    # Photo-frame looping video. Recover the cached file's hash from its
    # sidecar; if a URL is configured but the cache is missing (e.g. the
    # runtime volume was wiped), re-download it in the background so the
    # video is ready without blocking startup.
    state.photo_frame_video_url = str(cfg.get("photo_frame_video_url", "") or "")
    state.photo_frame_video_mode = bool(cfg.get("photo_frame_video_mode", False))
    _load_photo_frame_video_hash(state)
    if state.photo_frame_video_url and not state.photo_frame_video_hash:
        asyncio.create_task(_download_photo_frame_video(state, state.photo_frame_video_url))
    log.info(
        f"Photo-frame video config: mode={state.photo_frame_video_mode}, "
        f"url={'(set)' if state.photo_frame_video_url else '(none)'}, "
        f"cached={'yes' if state.photo_frame_video_hash else 'no'}"
    )

    # Daily dusk/dawn theme scheduler
    if state.auto_theme:
        state.theme_scheduler_task = asyncio.create_task(_theme_scheduler(state))
    else:
        state.theme_scheduler_task = None

    # Display auto-off loop — runs always. The poll itself is cheap; it
    # only acts when display_auto_off_seconds > 0.
    state.display_auto_off_task = asyncio.create_task(_display_auto_off_loop(state))

    # Photo-frame idle auto-activation. No-op when the setting is 0.
    state.photo_frame_idle_task = asyncio.create_task(_photo_frame_idle_loop(state))

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
        # Wire all MQTT command + config callbacks (extracted to
        # mqtt_callbacks.py to keep lifespan lean).
        from .mqtt_callbacks import wire as _wire_mqtt_callbacks
        await _wire_mqtt_callbacks(state, bridge)

        await bridge.start()
        state.mqtt_bridge = bridge
        log.info(f"MQTT bridge started for broker {mqtt_host}")
    else:
        log.info("MQTT bridge disabled (set MQTT_BROKER_HOST to enable)")

    # Deferred OpenClaw client creation (after MCP + MQTT are up)
    if state.openclaw_enabled and state.openclaw_gateway_url:
        from .openclaw_client import OpenClawClient
        hal_url = f"http://{os.environ.get('HAL_HOST', '0.0.0.0')}:{os.environ.get('PORT', '8765')}"
        state.openclaw_client = OpenClawClient(
            gateway_url=state.openclaw_gateway_url,
            hal_base_url=hal_url,
        )
        gw_pass = os.environ.get("OPENCLAW_GATEWAY_PASS", "")
        if gw_pass:
            state.openclaw_client.set_gateway_password(gw_pass)
        if state.conversation:
            state.conversation.openclaw_client = state.openclaw_client
            state.conversation.on_openclaw_media = lambda r: _handle_openclaw_media(state, r)
        log.info(f"OpenClaw client created → {state.openclaw_gateway_url}")

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
    if state.openclaw_client:
        await state.openclaw_client.close()


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

    try:
        await websocket.send_json({
            "type": "set_orientation",
            "orientation": state.display_orientation,
            "orb_side": state.orb_side,
        })
        log.info(f"Pushed initial orientation to RPi: {state.display_orientation}/{state.orb_side}")
    except Exception as e:
        log.warning(f"Could not push initial orientation to RPi: {e}")

    async def on_wake_word():
        """Callback: play chime on RPi and flash the UI."""
        # Wake the display first so the chime / listening cue actually
        # appears on the panel. Fire-and-forget — the chime audio is
        # already on its way.
        _record_user_activity(state)
        # Voice recognition is starting — dismiss the photo frame so
        # the listening cue / response panel has the screen.
        _dismiss_photo_frame_async(state, reason="wake_word")
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
        # If HAL is about to speak, the display must be on for the
        # response panel + state-video crossfade to be visible.
        _record_user_activity(state)
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
            model = metrics.get("model", "")
            engine = "openclaw" if model == "openclaw" else "ollama"
            await state.mqtt_bridge.publish_conversation_engine(
                engine,
                duration_s=metrics.get("llm_total_s", 0.0),
                model=model,
            )

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
                    # Zombie connection — TCP still open but no pongs.
                    # Close the WS so the handler's receive loop exits
                    # and the client sees ConnectionClosed and reconnects.
                    log.error(
                        f"No pong from RPi in {elapsed:.0f}s — closing zombie WS"
                    )
                    try:
                        await websocket.close(code=1011, reason="pong-timeout")
                    except Exception as ce:
                        log.warning(f"Keepalive: ws.close raised {ce}")
                    break
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
                    # Treat dismissal as user activity so the idle loop
                    # doesn't immediately re-arm the frame on the next tick.
                    _record_user_activity(state)

                elif msg_type == "photo_frame_video_error":
                    # The kiosk couldn't load/autoplay the looping video.
                    # Fall back to cycling photos for this activation.
                    from .photo_frame import handle_video_load_error
                    await handle_video_load_error(state)

                elif msg_type == "display_state":
                    # Confirmation from the RPi container after a
                    # display_set call (or a periodic probe). Keep our
                    # server-side state in sync and republish to HA so
                    # the switch reflects reality.
                    reported = (str(msg.get("state") or "")).lower()
                    available = bool(msg.get("available", True))
                    state.display_available = available
                    if reported in ("on", "off") and reported != state.display_state:
                        state.display_state = reported
                    if state.mqtt_bridge:
                        try:
                            await state.mqtt_bridge.publish_display_state(state.display_state)
                        except Exception as e:
                            log.debug(f"publish_display_state from upstream relay failed: {e}")

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
        "openclaw_enabled": state.openclaw_enabled,
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


class DisplayRequest(BaseModel):
    # "on", "off", or "toggle". Anything else → 422.
    state: str


@app.get("/api/display")
async def get_display():
    """Return the kiosk display power state and idle-blank timeout."""
    state = _get_state(app)
    return {
        "state": state.display_state,
        "auto_off_seconds": int(state.display_auto_off_seconds or 0),
        "available": bool(state.display_available),
    }


@app.post("/api/display")
async def post_display(req: DisplayRequest):
    """Turn the kiosk display on or off (DPMS). 'toggle' flips current state."""
    state = _get_state(app)
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


@app.get("/api/photo_frame/video")
async def get_photo_frame_video():
    """Serve the cached looping video. The RPi pulls this and stores it
    locally for the kiosk to play. 404 when no video is cached."""
    from fastapi.responses import FileResponse, Response
    cache = _photo_frame_video_cache_path()
    if not os.path.isfile(cache):
        return Response(status_code=404)
    return FileResponse(cache, media_type="video/mp4")


class PhotoFrameIdleRequest(BaseModel):
    # Minutes of inactivity before the photo frame auto-activates.
    # 0 disables the feature. Anything else clamped to 0..720.
    minutes: int


@app.get("/api/photo_frame/idle")
async def get_photo_frame_idle():
    """Return the photo-frame idle auto-activation setting (minutes)."""
    state = _get_state(app)
    return {
        "minutes": int(state.photo_frame_idle_minutes or 0),
        "active": (state.photo_frame_session is not None
                   or state.photo_frame_video_session is not None),
    }


@app.post("/api/photo_frame/idle")
async def post_photo_frame_idle(req: PhotoFrameIdleRequest):
    """Set the photo-frame idle auto-activation threshold in minutes.
    0 disables the feature."""
    state = _get_state(app)
    try:
        n = int(req.minutes)
    except (TypeError, ValueError):
        n = 0
    n = max(0, min(720, n))
    cb = getattr(state.mqtt_bridge, "on_config_photo_frame_idle_minutes", None) if state.mqtt_bridge else None
    if cb is not None:
        await cb(n)
    else:
        state.photo_frame_idle_minutes = n
        state.user_last_activity = time.time()
        if state.runtime_config is not None:
            state.runtime_config.set("photo_frame_idle_minutes", n)
    return {"status": "ok", "minutes": int(state.photo_frame_idle_minutes or 0)}


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

    _record_user_activity(state)
    _dismiss_photo_frame_async(state, reason="command")

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


@app.post("/api/openclaw/response")
async def post_openclaw_response(request: Request):
    """Callback endpoint for OpenClaw channel plugin to deliver agent responses."""
    state = _get_state(app)
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


@app.post("/api/openclaw/say")
async def post_openclaw_say(request: Request):
    """Proactive agent-initiated speech — no request_id, no pending turn.

    The OpenClaw channel's outbound path POSTs here when the agent wants
    to speak to the user unprompted (reminders, notifications). HAL
    synthesizes the text and plays it through the kiosk immediately.
    """
    state = _get_state(app)
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
