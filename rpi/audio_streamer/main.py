"""RPi audio streamer: captures mic → WebSocket → AI server, plays back TTS."""

import asyncio
import json
import logging
import os
import struct
import sys
import wave
import io

import numpy as np
import pyaudio
import websockets
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("hal.rpi")

# Configuration
AI_SERVER_HOST = os.environ.get("AI_SERVER_HOST", "192.168.1.100")
AUDIO_DEVICE = os.environ.get("AUDIO_DEVICE", "default")
SAMPLE_RATE = int(os.environ.get("SAMPLE_RATE", "16000"))
CHANNELS = int(os.environ.get("CHANNELS", "1"))
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", "4096"))
WEB_PORT = 8080

# PyAudio setup
pa = pyaudio.PyAudio()

# Connected web UI clients
ui_clients: set[web.WebSocketResponse] = set()

# TTS playback state
tts_buffer = bytearray()
tts_receiving = False
tts_playing = False

# Chime playback state
chime_buffer = bytearray()
chime_receiving = False


def find_audio_device() -> int | None:
    """Find the Anker Powerconf S330 or use default."""
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        name = info.get("name", "").lower()
        if "anker" in name or "powerconf" in name:
            log.info(f"Found Anker device: {info['name']} (index {i})")
            log.info(f"  Default sample rate: {info.get('defaultSampleRate')}")
            log.info(f"  Max input channels: {info.get('maxInputChannels')}")
            return i
    log.info("Anker device not found, using default input")
    return None


def find_supported_sample_rate(device_index: int | None, desired_rate: int) -> int:
    """Find a sample rate the device supports, preferring the desired rate."""
    candidates = [desired_rate, 48000, 44100, 32000, 16000, 8000]
    for rate in candidates:
        try:
            supported = pa.is_format_supported(
                rate,
                input_device=device_index,
                input_channels=CHANNELS,
                input_format=pyaudio.paInt16,
            )
            if supported:
                log.info(f"Device supports {rate} Hz")
                return rate
        except ValueError:
            continue
    # Fallback: use device default
    if device_index is not None:
        info = pa.get_device_info_by_index(device_index)
        default_rate = int(info.get("defaultSampleRate", 44100))
        log.info(f"Falling back to device default: {default_rate} Hz")
        return default_rate
    return 44100


def find_output_device() -> int | None:
    """Find the output device (Anker speaker)."""
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        name = info.get("name", "").lower()
        if ("anker" in name or "powerconf" in name) and info.get("maxOutputChannels", 0) > 0:
            log.info(f"Found Anker output: {info['name']} (index {i})")
            return i
    log.info("Using default audio output")
    return None


async def play_tts_audio(audio_data: bytes):
    """Play TTS WAV audio through the speaker."""
    global tts_playing
    tts_playing = True

    try:
        # Parse WAV data
        buf = io.BytesIO(audio_data)
        with wave.open(buf, "rb") as wf:
            out_rate = wf.getframerate()
            out_channels = wf.getnchannels()
            out_width = wf.getsampwidth()
            frames = wf.readframes(wf.getnframes())

        output_device = find_output_device()

        stream = pa.open(
            format=pa.get_format_from_width(out_width),
            channels=out_channels,
            rate=out_rate,
            output=True,
            output_device_index=output_device,
        )

        # Play in chunks
        chunk = 4096
        for i in range(0, len(frames), chunk):
            stream.write(frames[i : i + chunk])
            await asyncio.sleep(0)  # Yield to event loop

        stream.stop_stream()
        stream.close()

    except Exception as e:
        log.error(f"TTS playback error: {e}")
    finally:
        tts_playing = False


async def audio_stream_handler():
    """Main loop: capture audio and stream to AI server."""
    global tts_buffer, tts_receiving

    device_index = find_audio_device()
    device_rate = find_supported_sample_rate(device_index, SAMPLE_RATE)
    needs_resample = device_rate != SAMPLE_RATE
    if needs_resample:
        log.info(f"Will resample from {device_rate} Hz to {SAMPLE_RATE} Hz")

    uri = f"ws://{AI_SERVER_HOST}:8765/ws/audio"

    while True:
        try:
            log.info(f"Connecting to AI server at {uri}...")
            async with websockets.connect(uri, max_size=16 * 1024 * 1024) as ws:
                log.info("Connected to AI server")

                # Open audio input stream at the device's supported rate
                stream = pa.open(
                    format=pyaudio.paInt16,
                    channels=CHANNELS,
                    rate=device_rate,
                    input=True,
                    input_device_index=device_index,
                    frames_per_buffer=CHUNK_SIZE,
                )

                async def send_audio():
                    """Continuously capture and send audio."""
                    loop = asyncio.get_event_loop()
                    while True:
                        if tts_playing:
                            # Don't capture while playing TTS (basic echo prevention)
                            await asyncio.sleep(0.1)
                            continue

                        # Read audio in a thread to avoid blocking
                        audio_data = await loop.run_in_executor(
                            None, stream.read, CHUNK_SIZE, False
                        )

                        if needs_resample:
                            # Resample from device rate to target rate (16kHz)
                            samples = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32)
                            ratio = SAMPLE_RATE / device_rate
                            new_len = int(len(samples) * ratio)
                            indices = np.arange(new_len) / ratio
                            indices = np.clip(indices.astype(np.int32), 0, len(samples) - 1)
                            resampled = samples[indices].astype(np.int16)
                            audio_data = resampled.tobytes()

                        await ws.send(audio_data)

                async def receive_messages():
                    """Handle messages from AI server."""
                    global tts_buffer, tts_receiving, chime_buffer, chime_receiving

                    async for message in ws:
                        if isinstance(message, bytes):
                            if chime_receiving:
                                chime_buffer.extend(message)
                            elif tts_receiving:
                                tts_buffer.extend(message)
                        else:
                            msg = json.loads(message)
                            msg_type = msg.get("type")

                            if msg_type == "chime_start":
                                chime_receiving = True
                                chime_buffer = bytearray()

                            elif msg_type == "chime_end":
                                chime_receiving = False
                                if chime_buffer:
                                    audio_data = bytes(chime_buffer)
                                    chime_buffer = bytearray()
                                    await play_tts_audio(audio_data)

                            elif msg_type == "tts_start":
                                tts_receiving = True
                                tts_buffer = bytearray()

                            elif msg_type == "tts_end":
                                tts_receiving = False
                                if tts_buffer:
                                    audio_data = bytes(tts_buffer)
                                    tts_buffer = bytearray()
                                    await play_tts_audio(audio_data)
                                    await ws.send(json.dumps({"type": "tts_finished"}))

                            elif msg_type in ("transcription", "response", "wake"):
                                # Forward to web UI clients
                                await broadcast_to_ui(msg)

                # Run send and receive concurrently
                await asyncio.gather(send_audio(), receive_messages())

        except (websockets.ConnectionClosed, ConnectionRefusedError, OSError) as e:
            log.warning(f"Connection lost: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)
        except Exception as e:
            log.error(f"Unexpected error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)


async def broadcast_to_ui(msg: dict):
    """Broadcast a message to all connected web UI clients."""
    data = json.dumps(msg)
    dead = set()
    for ws in ui_clients:
        try:
            await ws.send_str(data)
        except Exception:
            dead.add(ws)
    ui_clients.difference_update(dead)


async def websocket_handler(request):
    """Handle web UI WebSocket connections."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    ui_clients.add(ws)
    log.info(f"Web UI client connected (total: {len(ui_clients)})")

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get("type") == "ping":
                    await ws.send_json({"type": "pong"})
    finally:
        ui_clients.discard(ws)
        log.info(f"Web UI client disconnected (total: {len(ui_clients)})")

    return ws


async def start_web_server():
    """Start the aiohttp web server for serving UI and WebSocket."""
    app = web.Application()
    app.router.add_get("/ws", websocket_handler)
    app.router.add_static("/", "/app/web", show_index=True)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()
    log.info(f"Web UI available at http://0.0.0.0:{WEB_PORT}")


async def main():
    """Start all services."""
    log.info("Starting HAL RPi audio streamer...")
    await start_web_server()
    await audio_stream_handler()


if __name__ == "__main__":
    asyncio.run(main())
