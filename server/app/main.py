"""HAL — Main FastAPI server coordinating audio pipeline."""

import asyncio
import io
import json
import logging
import math
import os
import struct
import wave

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .audio_pipeline import AudioPipeline
from .conversation import ConversationManager
from .mcp_client import MCPClient
from .memory import MemoryClient
from .tts import TTSEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("hal")

app = FastAPI(title="HAL Voice Server")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Global state
pipeline: AudioPipeline | None = None
conversation: ConversationManager | None = None
tts_engine: TTSEngine | None = None
mcp_client: MCPClient | None = None
memory_client: MemoryClient | None = None

# Connected UI clients (for broadcasting transcription)
ui_clients: set[WebSocket] = set()

# Pre-generated wake word chime (created once at startup)
_wake_chime: bytes | None = None


def _generate_chime() -> bytes:
    """Generate a short two-tone chime as WAV bytes."""
    sample_rate = 22050
    duration = 0.15  # seconds per tone
    n_samples = int(sample_rate * duration)
    freqs = [880, 1320]  # A5 → E6 ascending two-tone

    samples = []
    for freq in freqs:
        for i in range(n_samples):
            t = i / sample_rate
            # Sine wave with a quick fade-in/out envelope
            envelope = min(1.0, i / 200) * min(1.0, (n_samples - i) / 200)
            sample = int(envelope * 12000 * math.sin(2 * math.pi * freq * t))
            samples.append(struct.pack("<h", max(-32768, min(32767, sample))))

    raw = b"".join(samples)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(raw)
    return buf.getvalue()


@app.on_event("startup")
async def startup():
    global pipeline, conversation, tts_engine, mcp_client, memory_client

    log.info("Initializing HAL voice server...")

    # Generate wake word chime
    _wake_chime = _generate_chime()
    log.info(f"Wake chime generated ({len(_wake_chime)} bytes)")

    # MCP client for Home Assistant
    mcp_url = os.environ.get("MCP_SERVER_URL", "")
    mcp_client = MCPClient(server_url=mcp_url)
    if mcp_url:
        try:
            await mcp_client.connect()
        except Exception as e:
            log.error(f"Failed to connect to MCP server: {e}")
            log.warning("HAL will run without Home Assistant integration")
            # Reset to a clean unconnected client
            mcp_client = MCPClient(server_url="")
    else:
        log.warning("MCP_SERVER_URL not set — Home Assistant integration disabled")

    # Wyoming TTS
    tts_host = os.environ.get("WYOMING_TTS_HOST", "localhost")
    tts_port = int(os.environ.get("WYOMING_TTS_PORT", "10200"))
    tts_voice = os.environ.get("WYOMING_TTS_VOICE", "")
    tts_engine = TTSEngine(host=tts_host, port=tts_port, voice=tts_voice)
    await tts_engine.initialize()

    # Long-term memory (Shodh)
    memory_url = os.environ.get("MEMORY_URL", "http://shodh-memory:3030")
    memory_user = os.environ.get("MEMORY_USER_ID", "hal-default")
    memory_client = MemoryClient(base_url=memory_url, user_id=memory_user)
    await memory_client.initialize()

    # Audio pipeline (VAD + transcription + speaker filter)
    sample_rate = int(os.environ.get("SAMPLE_RATE", "16000"))
    stt_engine = os.environ.get("STT_ENGINE", "whisper")
    stt_model = os.environ.get("STT_MODEL", "")
    pipeline = AudioPipeline(sample_rate=sample_rate, stt_engine=stt_engine, stt_model=stt_model)
    await pipeline.initialize()

    # Conversation manager
    system_prompt = ""
    prompt_file = os.environ.get("SYSTEM_PROMPT_FILE", "/app/system_prompt.txt")
    if os.path.isfile(prompt_file):
        with open(prompt_file, "r") as f:
            system_prompt = f.read().strip()
        log.info(f"Loaded system prompt from {prompt_file} ({len(system_prompt)} chars)")
    else:
        log.info("No system prompt file found, using default")
    conversation = ConversationManager(
        wake_word=os.environ.get("WAKE_WORD", "hey hal"),
        ollama_host=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
        ollama_model=os.environ.get("OLLAMA_MODEL", "llama3.2"),
        mcp_client=mcp_client,
        tts_engine=tts_engine,
        memory_client=memory_client,
        system_prompt=system_prompt,
    )

    log.info("HAL voice server ready.")


@app.on_event("shutdown")
async def shutdown():
    if mcp_client:
        await mcp_client.disconnect()


async def broadcast_to_ui(msg: dict):
    """Send a message to all connected UI WebSocket clients."""
    data = json.dumps(msg)
    dead = set()
    for ws in ui_clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.add(ws)
    ui_clients.difference_update(dead)


@app.websocket("/ws/audio")
async def audio_endpoint(websocket: WebSocket):
    """
    Main audio WebSocket for the RPi audio streamer.

    Receives: raw PCM 16-bit LE audio chunks
    Sends:    JSON messages (transcription, state) and binary (TTS audio)
    """
    await websocket.accept()
    log.info("Audio client connected")

    async def on_wake_word():
        """Callback: play chime on RPi and flash the UI."""
        try:
            await broadcast_to_ui({"type": "wake"})
            await websocket.send_json({"type": "wake"})
            if _wake_chime:
                await websocket.send_json({"type": "chime_start", "size": len(_wake_chime)})
                await websocket.send_bytes(_wake_chime)
                await websocket.send_json({"type": "chime_end"})
        except Exception as e:
            log.error(f"Error sending wake chime: {e}")

    async def on_response(text: str, audio_bytes: bytes | None):
        """Callback: send LLM response + TTS audio back to RPi."""
        try:
            await websocket.send_json({"type": "response", "text": text})
            await broadcast_to_ui({"type": "response", "text": text})
            if audio_bytes:
                await websocket.send_json({"type": "tts_start", "size": len(audio_bytes)})
                pipeline.set_ai_speaking(True)
                chunk_size = 8192
                for i in range(0, len(audio_bytes), chunk_size):
                    await websocket.send_bytes(audio_bytes[i : i + chunk_size])
                await websocket.send_json({"type": "tts_end"})
        except Exception as e:
            log.error(f"Error sending response: {e}")

    conversation.on_response = on_response
    conversation.on_wake_word = on_wake_word

    try:
        while True:
            data = await websocket.receive()

            if "bytes" in data:
                audio_chunk = data["bytes"]
                result = await pipeline.process_chunk(audio_chunk)

                if result:
                    # Always broadcast transcription to UI (continuous display)
                    await broadcast_to_ui({
                        "type": "transcription",
                        "text": result["text"],
                        "is_partial": result.get("is_partial", False),
                        "speaker": result.get("speaker", "unknown"),
                    })

                    await websocket.send_json({
                        "type": "transcription",
                        "text": result["text"],
                        "is_partial": result.get("is_partial", False),
                        "speaker": result.get("speaker", "unknown"),
                    })

                    # Feed finalized human speech to conversation manager
                    # (it decides internally whether wake word was said)
                    if not result.get("is_partial", False) and result.get("speaker") != "ai":
                        await conversation.process_text(result["text"])

                # Silence after speech → conversation manager may trigger LLM
                if pipeline.silence_detected:
                    pipeline.silence_detected = False
                    await conversation.on_silence()

            elif "text" in data:
                msg = json.loads(data["text"])
                if msg.get("type") == "tts_finished":
                    pipeline.set_ai_speaking(False)

    except WebSocketDisconnect:
        log.info("Audio client disconnected")
    except Exception as e:
        log.error(f"Audio WebSocket error: {e}")


@app.websocket("/ws/ui")
async def ui_endpoint(websocket: WebSocket):
    """WebSocket for the web UI to receive live transcription and state updates."""
    await websocket.accept()
    ui_clients.add(websocket)
    log.info(f"UI client connected (total: {len(ui_clients)})")

    if conversation:
        await websocket.send_json({
            "type": "state",
            "state": conversation.state,
            "wake_word": conversation.wake_word,
        })

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            if msg.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        ui_clients.discard(websocket)
        log.info(f"UI client disconnected (total: {len(ui_clients)})")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "pipeline_ready": pipeline is not None,
        "mcp_connected": mcp_client is not None and len(mcp_client.tool_names) > 0,
        "tts_available": tts_engine is not None,
        "memory_available": memory_client is not None and memory_client.available,
    }
