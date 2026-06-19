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
    # The kiosk (RPi audio client) — the PAL display unit, called the "hub"
    # externally (intercom directory / "call the hub"). Internal name: kiosk.
    audio_websocket: WebSocket | None = None  # RPi audio client / the hub
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
    intercom_sessions: dict = field(default_factory=dict)     # session_id -> {caller, callee, state, ...}
    current_theme: str = "dark"
    theme_day: str = "birch"
    theme_night: str = "dark"
    auto_theme: bool = True
    runtime_config: object | None = None  # RuntimeConfig
    voice_options: list = field(default_factory=list)
    model_options: list = field(default_factory=list)
    themes: object | None = None  # ThemeRegistry
    theme_scheduler_task: object | None = None
    # Weather under the clock: last-pushed weather_update message (for dedupe +
    # replay to (re)connecting clients) and the poller task.
    current_weather: dict | None = None
    weather_task: object | None = None
    # Face-aware Ken Burns: last photo_faces pushed to the KIOSK (for replay to
    # a (re)connecting kiosk/web client). None when no frame / no faces.
    current_photo_faces: dict | None = None
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
    # Last force-pushed visual (show_camera snapshot / play_video) so a
    # (re)connecting satellite can replay it: {"msg": <ws msg>, "expires":
    # monotonic-deadline | None}. Mobile websockets flap (background, VPN,
    # cellular), so without replay a phone that reconnects seconds after a
    # force action misses it entirely.
    active_visual: dict | None = None
    # Active calendar overlay, same shape — replayed to satellites on connect.
    active_calendar: dict | None = None
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
    # Cloud LLM override. `cloud_llm_enabled` ALWAYS boots False (never read
    # from runtime config) — cloud spend requires a deliberate flip each
    # restart. The chosen model persists; options are "provider/model-id"
    # entries from runtime/cloud_providers.json (keys never leave the server).
    cloud_llm_client: object | None = None  # cloud_llm.CloudLLMClient
    cloud_llm_enabled: bool = False
    cloud_llm_model: str = ""
    cloud_model_options: list = field(default_factory=list)
    # Persistent conversation log (conversation_log.ConversationLog) + the
    # injected event-logger wrapper (resolves satellite tokens → device names).
    conversation_log: object | None = None
    conversation_event_logger: object | None = None
    # Voice timers (timers.TimerManager) + the runtime templates that name
    # timers and word the finish announcement (HA-editable text config).
    timer_manager: object | None = None
    timer_name_template: str = "Timer {n}"
    timer_announce_template: str = "{name} is ready."
    intercom_announce_template: str = "Incoming call from {name}"
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

    # Push notifications (APNs/FCM) for paired devices whose app is closed.
    # No-ops until runtime/push_providers.json is configured. The signed-image-
    # URL HMAC secret is per-process — a restart invalidates outstanding (~10min)
    # image URLs, which is fine. No network at startup (tokens minted lazily).
    import secrets as _secrets
    state.push_signing_secret = _secrets.token_urlsafe(32)
    state.push = None
    try:
        from .push import PushService
        from .push_providers import PushProviderRegistry, DEFAULT_PUSH_PROVIDERS_PATH
        _preg = PushProviderRegistry(os.environ.get("PUSH_PROVIDERS_PATH", DEFAULT_PUSH_PROVIDERS_PATH))
        state.push = PushService(_preg, state.push_signing_secret,
                                 os.environ.get("HAL_GATEWAY_URL", "").strip())
        log.info(f"Push notifications ready (configured: {_preg.configured()})")
    except Exception as e:
        log.warning(f"Push init failed: {type(e).__name__}: {e}")

    # Persistent conversation log (postgres). connect() is backgrounded with
    # retry/backoff — a slow or absent postgres never blocks startup, and
    # writes degrade to warn+drop until it comes up.
    from .conversation_log import ConversationLog
    state.conversation_log = ConversationLog(os.environ.get("CONVERSATION_LOG_DSN", ""))
    asyncio.create_task(state.conversation_log.connect())

    async def _conversation_event_logger(kind: str, text: str, origin_token: str | None = None,
                                         source: str | None = None, meta: dict | None = None):
        """Injected into ConversationManager + the announcement sites. Resolves
        a satellite origin TOKEN to its non-secret device_name at write time —
        the raw token must never reach the log."""
        origin = source
        if origin is None and origin_token and state.pairing is not None:
            origin = state.pairing.device_name(origin_token)
        await state.conversation_log.log(kind, text, origin=origin, meta=meta)

    state.conversation_event_logger = _conversation_event_logger
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

    # Voice timers: templates from runtime config (HA-editable), manager
    # created after the MCP client so the HA mirror + startup resync can call
    # timer.start/cancel. In-memory: timers do NOT survive a restart, so the
    # resync cancels any ghost pool entities left running in HA.
    from .timers import TimerManager
    state.timer_name_template = str(cfg.get("timer_name_template", "Timer {n}") or "Timer {n}")
    state.timer_announce_template = str(
        cfg.get("timer_announce_template", "{name} is ready.") or "{name} is ready."
    )
    state.intercom_announce_template = str(
        cfg.get("intercom_announce_template", "Incoming call from {name}")
        or "Incoming call from {name}"
    )
    state.timer_manager = TimerManager(state)
    asyncio.create_task(state.timer_manager.resync_pool_on_startup())

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
        event_logger=_conversation_event_logger,
    )
    # Intent-guard probe: lets the conversation verify that a hinted tool was
    # actually invoked during the turn (LocalToolsClient.call_tool is the
    # single dispatch point for both the local tool loop and OpenClaw's MCP
    # path, so its call log sees every engine).
    state.conversation.tool_call_probe = state.local_tools.called_since

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

    # Cloud LLM override: restore the persisted MODEL choice but NEVER the
    # switch (always boots OFF). Prefetch the provider/model options for the
    # HA select — best-effort and only when a provider is actually configured,
    # so an unconfigured install doesn't stall startup.
    state.cloud_llm_model = str(cfg.get("cloud_llm_model", "") or "")
    state.cloud_llm_enabled = False
    try:
        from .cloud_llm import ProviderRegistry, CloudLLMClient, DEFAULT_PROVIDERS_PATH
        _reg = ProviderRegistry(os.environ.get("CLOUD_PROVIDERS_PATH", DEFAULT_PROVIDERS_PATH))
        if _reg.providers():
            _probe = CloudLLMClient(_reg)
            try:
                state.cloud_model_options = await asyncio.wait_for(
                    _probe.list_models(), timeout=8.0
                )
            finally:
                await _probe.close()
            log.info(f"Cloud LLM: {len(state.cloud_model_options)} models from "
                     f"{len(_reg.providers())} provider(s) (override boots OFF)")
    except Exception as e:
        log.warning(f"Cloud LLM model prefetch failed: {type(e).__name__}")

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

    # Weather under the clock — runs always; no-op (hidden) until a weather
    # entity is configured and the toggle is on.
    from . import weather as _weather
    state.weather_task = asyncio.create_task(_weather.weather_poller(state))

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
    if getattr(state, "weather_task", None):
        state.weather_task.cancel()
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


async def replay_visual_state(state: AppState, ws) -> None:
    """Catch a (re)connecting satellite up on the household's active visuals.

    Mobile websockets flap (backgrounding, VPN, cellular), so a force action
    fired during a gap would otherwise be lost. On connect we re-send whatever
    is still live: the active WebRTC stream (the phone negotiates its own peer),
    else the last un-expired snapshot/video, plus any active calendar overlay —
    with duration_s adjusted to the REMAINING time. Best-effort."""
    try:
        sess = state.active_stream
        if sess:
            if sess.get("kind") == "rtsp":
                await ws.send_json({
                    "type": "stream_start",
                    "session_id": sess.get("session_id", ""),
                    "rtsp_url": sess.get("rtsp_url", ""),
                    "mode": "non-trickle",
                })
            elif sess.get("kind") == "ha":
                await ws.send_json({
                    "type": "stream_start",
                    "session_id": sess.get("session_id", ""),
                    "entity_id": sess.get("entity_id", ""),
                    "mode": "trickle",
                })
        else:
            vis = state.active_visual
            if vis:
                expires = vis.get("expires")
                if expires is None or expires > time.monotonic():
                    msg = dict(vis["msg"])
                    if expires is not None and "duration_s" in msg:
                        msg["duration_s"] = max(5, int(expires - time.monotonic()))
                    await ws.send_json(msg)
                else:
                    state.active_visual = None
        cal = state.active_calendar
        if cal:
            expires = cal.get("expires")
            if expires is None or expires > time.monotonic():
                msg = dict(cal["msg"])
                if expires is not None and "duration_s" in msg:
                    msg["duration_s"] = max(5, int(expires - time.monotonic()))
                await ws.send_json(msg)
            else:
                state.active_calendar = None
    except Exception as e:
        log.debug(f"replay_visual_state failed: {e}")


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


async def push_announcement(state: AppState, text: str, kind: str = "speak") -> None:
    """Push a spoken announcement to paired devices whose app is closed.

    The single place speak/timer text reaches closed apps. Every force-speak
    path (announce_everywhere, the MQTT `speak` entity, /api/speak,
    speak_verbatim, OpenClaw proactive say) routes its push through here, so a
    closed phone is notified no matter which path produced the speech — and
    independent of whether the kiosk/TTS is available. Offline-targeting +
    fire-and-forget live in PushService.dispatch. No-op until push is
    configured; never raises."""
    if getattr(state, "push", None) is None:
        return
    text = (text or "").strip()
    if not text:
        return
    try:
        await state.push.dispatch(state, kind, text, category=kind)
    except Exception as e:
        log.debug(f"push_announcement failed: {type(e).__name__}: {e}")


async def announce_everywhere(state: AppState, text: str,
                              log_source: str | None = None,
                              alarm: bool = False) -> None:
    """Speak `text` on EVERY device — kiosk speaker + all satellites — and
    mirror it to web UIs, the MQTT last_response sensor, and (when log_source
    is given) the conversation log. Needs no active turn; used for proactive
    events like finished timers. With alarm=True a kitchen-timer beep pattern
    is prepended to the audio server-side (alarm.prepend_alarm), so every
    device hears it with no client changes — and even a TTS failure still
    produces the alarm sound. Best-effort throughout: a missing kiosk or TTS
    failure must never crash the caller."""
    text = (text or "").strip()
    if not text:
        return
    audio_bytes = None
    if state.tts_engine:
        try:
            audio_bytes = await state.tts_engine.synthesize(text)
        except Exception as e:
            log.error(f"announce TTS failed: {e}")
    if alarm:
        try:
            from .alarm import prepend_alarm
            audio_bytes = prepend_alarm(audio_bytes)
        except Exception as e:
            log.warning(f"announce alarm prepend failed: {e}")
    msg = {"type": "response", "text": text}
    ws = state.audio_websocket
    if ws:
        try:
            await ws.send_json(msg)
        except Exception:
            pass
    await broadcast_to_ui(state, msg)
    await speak_to_satellites(state, text, audio_bytes)
    if ws and audio_bytes:
        try:
            await ws.send_json({"type": "tts_start", "size": len(audio_bytes)})
            if state.pipeline:
                state.pipeline.set_ai_speaking(True)
            for i in range(0, len(audio_bytes), 8192):
                await ws.send_bytes(audio_bytes[i:i + 8192])
            await ws.send_json({"type": "tts_end"})
        except Exception as e:
            log.error(f"announce kiosk audio failed: {e}")
            if state.pipeline:
                state.pipeline.set_ai_speaking(False)
    if state.mqtt_bridge:
        try:
            await state.mqtt_bridge.publish_last_response(text)
        except Exception:
            pass
    if log_source and state.conversation_event_logger:
        try:
            await state.conversation_event_logger("announcement", text, source=log_source)
        except Exception:
            pass
    # Push to paired devices whose app is closed (speak + finished timers ride
    # this path; timers get their own category/channel).
    await push_announcement(state, text,
                            kind="timer" if (alarm or log_source == "timer") else "speak")


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
