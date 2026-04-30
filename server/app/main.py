"""HAL — Main FastAPI server coordinating audio pipeline."""

import asyncio
import io
import json
import logging
import math
import os
import struct
import time
import wave
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .audio_pipeline import AudioPipeline
from .conversation import ConversationManager
from .local_tools import LocalToolsClient
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


VALID_THEMES = {"dark", "birch", "odyssey", "japandi"}


def _parse_iso(ts: str) -> "datetime | None":
    from datetime import datetime
    if not ts:
        return None
    try:
        # HA returns ISO with offset like 2026-04-15T18:42:00+00:00
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


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


async def apply_theme(state: AppState, theme: str) -> bool:
    """Update server-side theme and push to RPi web UI."""
    if theme not in VALID_THEMES:
        return False
    state.current_theme = theme
    msg = {"type": "set_theme", "name": theme}
    await broadcast_to_ui(state, msg)
    await _push_to_rpi(state, msg)
    if state.mqtt_bridge:
        await state.mqtt_bridge.publish_theme(theme)
    log.info(f"Theme set to: {theme}")
    return True


def _build_local_tools(state: AppState) -> LocalToolsClient:
    """Construct the LocalToolsClient with all tools wired to AppState."""
    tools = LocalToolsClient()

    async def set_theme(args: dict) -> str:
        name = (args.get("name") or "").lower().strip()
        if name not in VALID_THEMES:
            return f"Invalid theme '{name}'. Valid: {', '.join(sorted(VALID_THEMES))}"
        ok = await apply_theme(state, name)
        return f"Theme set to {name}" if ok else "Failed to apply theme"

    tools.register(
        "ui_set_theme",
        "Change the HAL web UI theme. Use 'dark' at night/dusk, 'birch' or 'japandi' for light rooms during the day, 'odyssey' for a sterile white look.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "enum": sorted(VALID_THEMES), "description": "Theme name"},
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

    return tools


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize all services on startup, clean up on shutdown."""
    state = AppState()
    app.state.hal = state

    log.info("Initializing HAL voice server...")

    # Generate wake word chime
    state.theme_day = os.environ.get("THEME_DAY", "birch")
    state.theme_night = os.environ.get("THEME_NIGHT", "dark")
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
        wake_word=os.environ.get("WAKE_WORD", "hey hal"),
        ollama_host=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
        ollama_model=os.environ.get("OLLAMA_MODEL", "llama3.2"),
        mcp_client=state.mcp_client,
        tts_engine=state.tts_engine,
        memory_client=state.memory_client,
        system_prompt=system_prompt,
        num_ctx=int(os.environ.get("OLLAMA_NUM_CTX", "32768")),
        num_predict=int(os.environ.get("OLLAMA_NUM_PREDICT", "512")),
    )

    # Daily dusk/dawn theme scheduler
    if os.environ.get("AUTO_THEME", "true").lower() in ("true", "1", "yes"):
        state.theme_scheduler_task = asyncio.create_task(_theme_scheduler(state))
    else:
        state.theme_scheduler_task = None

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

        bridge.on_volume_set = _mqtt_volume
        bridge.on_mute_set = _mqtt_mute
        bridge.on_theme_set = _mqtt_theme
        bridge.on_speak = _mqtt_speak
        bridge.on_command = _mqtt_command

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

    conversation.on_response = on_response
    conversation.on_wake_word = on_wake_word
    conversation.on_state_change = on_state_change

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

    except (WebSocketDisconnect, RuntimeError):
        log.info("Audio client disconnected")
    except Exception as e:
        log.error(f"Audio WebSocket error: {e}", exc_info=True)
    finally:
        state.audio_websocket = None
        keepalive_task.cancel()
        watchdog_task.cancel()


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
