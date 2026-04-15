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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .audio_pipeline import AudioPipeline
from .conversation import ConversationManager
from .mcp_client import MCPClient, MultiMCPClient
from .memory import MemoryClient
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize all services on startup, clean up on shutdown."""
    state = AppState()
    app.state.hal = state

    log.info("Initializing HAL voice server...")

    # Generate wake word chime
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

    log.info(f"Total MCP tools available: {len(state.mcp_client.tool_names)}")

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

    log.info("HAL voice server ready.")

    yield

    # Shutdown
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
        """Callback: broadcast state changes to RPi and UI."""
        msg = {"type": "state", "state": new_state}
        try:
            await websocket.send_json(msg)
        except Exception:
            pass
        await broadcast_to_ui(state, msg)

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
