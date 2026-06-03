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

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

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
    pairing: object | None = None  # pairing.PairingManager (mobile device tokens)
    # Satellite mode: a paired phone connected to /ws/ui with a valid token is a
    # "satellite" — its turns' outputs route ONLY to it (never the kiosk), and it
    # is excluded from the global broadcast_to_ui. The kiosk is reached via
    # audio_websocket (the RPi), not ui_clients, so it's unaffected.
    satellite_ws: dict = field(default_factory=dict)          # token -> WebSocket
    satellite_ws_tokens: dict = field(default_factory=dict)   # WebSocket -> token (O(1) cleanup)
    satellite_tts: dict = field(default_factory=dict)         # token -> {audio, mime, ts, seq}
    satellite_photo_sessions: dict = field(default_factory=dict)  # token -> PhotoFrameSession
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
    # Whether the clock/date overlay is shown while a photo/video frame is
    # up (user setting, default on). Pushed to the kiosk on connect and on
    # change as a set_photo_frame_clock message.
    photo_frame_show_clock: bool = True
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
    await broadcast_force_action(state, msg)  # web mirrors + satellites
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

    # Mobile device pairing (loads any persisted device tokens). Token auth is
    # only ENFORCED when HAL_REQUIRE_TOKEN is set; otherwise this is inert.
    from .pairing import PairingManager, require_token_enabled
    state.pairing = PairingManager()
    log.info(f"Pairing ready (token enforcement: {'ON' if require_token_enabled() else 'OFF'})")
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
    state.photo_frame_show_clock = bool(cfg.get("photo_frame_show_clock", True))
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


app = FastAPI(title="PAL Voice Server", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _get_state(app: FastAPI) -> AppState:
    """Retrieve HAL state from the FastAPI app."""
    return app.state.hal


async def broadcast_to_ui(state: AppState, msg: dict):
    """Send a message to all MIRROR UI clients (kiosk-mirroring web UIs).

    SATELLITE clients (paired phones) are excluded — they only receive messages
    targeted at them via send_to_device, so global/kiosk/voice turns never leak
    onto a phone. The kiosk itself is the RPi (audio_websocket), not a ui_client.
    """
    data = json.dumps(msg)
    dead = set()
    for ws in state.ui_clients:
        if ws in state.satellite_ws_tokens:
            continue  # satellites get only their own targeted messages
        try:
            await ws.send_text(data)
        except Exception:
            dead.add(ws)
    state.ui_clients.difference_update(dead)


async def send_to_device(state: AppState, token: str, msg: dict) -> bool:
    """Send a JSON message to ONE satellite device (by pairing token). Returns
    False if the device isn't connected; deregisters it on send failure."""
    ws = state.satellite_ws.get(token)
    if ws is None:
        return False
    try:
        await ws.send_text(json.dumps(msg))
        return True
    except Exception:
        _deregister_satellite(state, ws)
        return False


def _deregister_satellite(state: AppState, ws) -> None:
    """Drop a satellite's registry entries (idempotent). Photo-session teardown
    is handled by the /ws/ui disconnect handler."""
    token = state.satellite_ws_tokens.pop(ws, None)
    if token is not None and state.satellite_ws.get(token) is ws:
        state.satellite_ws.pop(token, None)


def cache_satellite_tts(state: AppState, token: str, audio: bytes, mime: str) -> int:
    """Cache a turn's TTS audio for a satellite device so it can fetch it from
    GET /api/satellite/tts. Returns a per-device monotonically-increasing seq the
    device can echo so a fetch for a superseded turn can be rejected."""
    prev = state.satellite_tts.get(token) or {}
    seq = int(prev.get("seq", 0)) + 1
    state.satellite_tts[token] = {
        "audio": audio, "mime": mime, "ts": time.monotonic(), "seq": seq,
    }
    return seq


async def send_to_satellites(state: AppState, msg: dict) -> int:
    """Fan a message out to every connected satellite. Returns the count that
    received it. Used for household-wide FORCE actions (proactive pushes the
    user/HA triggers — speak, theme, orb media), NOT per-turn command outputs
    (those stay isolated to their origin device via send_to_device)."""
    delivered = 0
    for token in list(state.satellite_ws.keys()):
        if await send_to_device(state, token, msg):
            delivered += 1
    return delivered


async def dismiss_satellite_photo_frames(state: AppState) -> None:
    """Tear down every satellite's idle photo frame so an incoming force action
    (spoken announcement, orb media, live stream) is actually visible instead of
    hidden behind the ambient photo. Idempotent / best-effort."""
    from .photo_frame import stop_photo_frame_for_device
    for token in list(getattr(state, "satellite_photo_sessions", {}).keys()):
        try:
            await stop_photo_frame_for_device(state, token, reason="force_action")
        except Exception as e:
            log.debug(f"dismiss satellite photo frame failed: {e}")


async def broadcast_force_action(state: AppState, msg: dict, *, dismiss_photo: bool = False) -> None:
    """Deliver a force/proactive action to ALL display surfaces: the kiosk-
    mirroring web UIs (broadcast_to_ui) AND every connected satellite.

    This is the satellite-inclusive counterpart to broadcast_to_ui. Use it only
    for household-wide proactive actions (theme, orb image/video); the kiosk
    itself is reached separately via the RPi socket (_push_to_rpi / the
    dispatcher's audio_websocket send), exactly as before.

    `dismiss_photo=True` first clears any satellite idle photo frame so visible
    content (image/video) isn't hidden behind it; cosmetic actions like a theme
    change leave the photo frame alone."""
    if dismiss_photo:
        await dismiss_satellite_photo_frames(state)
    await broadcast_to_ui(state, msg)
    await send_to_satellites(state, msg)


async def speak_to_satellites(state: AppState, text: str, audio_bytes: bytes | None) -> int:
    """Make every connected satellite show + speak a force-spoken line: the
    response text (drives the orb to 'speaking') plus, when audio is available,
    HAL's server voice via the per-device TTS cache + tts_play. Returns the
    number of satellites that received the audio cue."""
    # Clear any idle photo frame so the spoken text shows on the orb.
    await dismiss_satellite_photo_frames(state)
    spoken = 0
    for token in list(state.satellite_ws.keys()):
        if not await send_to_device(state, token, {"type": "response", "text": text}):
            continue
        if audio_bytes:
            seq = cache_satellite_tts(state, token, audio_bytes, "audio/wav")
            await send_to_device(state, token, {
                "type": "tts_play", "url": "/api/satellite/tts",
                "mime": "audio/wav", "seq": seq,
            })
            spoken += 1
    return spoken


# === Route registration ======================================================
# Routers are imported at the BOTTOM of this module so that main is fully
# defined first (app, _get_state, broadcast_to_ui, helpers). routes_ws and
# routes_http lazy-import from .main inside each handler, so importing them
# here — after everything they need exists — avoids a circular import.

from .routes_ws import router as _ws_router  # noqa: E402
from .routes_http import router as _http_router  # noqa: E402
from .pairing import router as _pairing_router  # noqa: E402

app.include_router(_ws_router)
app.include_router(_http_router)
app.include_router(_pairing_router)

# Re-export the moved WS + HTTP handlers and request models so existing
# callers/tests that do `from .main import <handler>` (or `srv.<handler>`)
# keep resolving after the split.
from .routes_ws import (  # noqa: E402,F401
    audio_endpoint,
    ptt_endpoint,
    ui_endpoint,
)
from .routes_http import (  # noqa: E402,F401
    CommandRequest,
    DisplayRequest,
    PhotoFrameStartRequest,
    PhotoFrameIdleRequest,
    VolumeRequest,
    SpeakRequest,
    health,
    post_ptt_start,
    post_ptt_end,
    post_ptt_cancel,
    get_display,
    post_display,
    post_photo_frame_start,
    post_photo_frame_end,
    get_photo_frame_video,
    get_photo_frame_idle,
    post_photo_frame_idle,
    post_command,
    post_openclaw_response,
    post_openclaw_say,
    post_volume,
    post_snapshot,
    get_snapshot,
    post_speak,
    post_mute_toggle,
    get_mute_status,
    list_themes_endpoint,
    get_theme_file,
)
