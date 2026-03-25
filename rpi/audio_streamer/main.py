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


def _design_lowpass_fir(cutoff: float, num_taps: int = 63) -> np.ndarray:
    """Design a windowed-sinc low-pass FIR filter with Kaiser window."""
    if num_taps % 2 == 0:
        num_taps += 1
    n = np.arange(num_taps)
    mid = num_taps // 2

    # Sinc function
    sinc = np.sinc(2 * cutoff * (n - mid))
    # Kaiser window (beta=5 gives ~44dB stopband attenuation)
    beta = 5.0
    window = np.i0(beta * np.sqrt(1 - ((n - mid) / mid) ** 2)) / np.i0(beta)
    fir = sinc * window
    fir /= fir.sum()
    return fir


# Pre-compute filters for common sample rate conversions
_fir_cache: dict[tuple[int, int], np.ndarray] = {}


def _get_downsample_filter(from_rate: int, to_rate: int) -> np.ndarray:
    """Get or create a cached anti-aliasing FIR filter for downsampling."""
    key = (from_rate, to_rate)
    if key not in _fir_cache:
        ratio = from_rate / to_rate
        cutoff = to_rate / (2.0 * from_rate)  # Nyquist of target rate
        # More taps = sharper cutoff. 64*ratio gives excellent stopband rejection.
        num_taps = int(ratio * 64) + 1
        if num_taps < 31:
            num_taps = 31
        _fir_cache[key] = _design_lowpass_fir(cutoff, num_taps)
        log.info(f"Designed {num_taps}-tap FIR filter for {from_rate}→{to_rate} Hz")
    return _fir_cache[key]


def _resample_audio(samples: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """
    Resample int16 audio with proper anti-aliasing.

    For downsampling: applies a Kaiser-windowed sinc low-pass FIR filter
    then decimates. For integer ratios (e.g. 48k→16k = 3:1), uses exact
    decimation for zero interpolation error.
    For upsampling: uses linear interpolation (no aliasing risk).
    """
    if from_rate == to_rate:
        return samples

    float_samples = samples.astype(np.float32)

    if from_rate > to_rate:
        # Downsampling: filter then decimate
        fir = _get_downsample_filter(from_rate, to_rate)
        filtered = np.convolve(float_samples, fir, mode="same")

        # Check for integer ratio (e.g. 48000/16000 = 3)
        ratio = from_rate / to_rate
        int_ratio = round(ratio)
        if abs(ratio - int_ratio) < 0.001:
            # Exact decimation — no interpolation artifacts
            resampled = filtered[::int_ratio]
        else:
            # Non-integer ratio — use interpolation
            new_len = int(len(filtered) * to_rate / from_rate)
            x_old = np.linspace(0, 1, len(filtered))
            x_new = np.linspace(0, 1, new_len)
            resampled = np.interp(x_new, x_old, filtered)
    else:
        # Upsampling: linear interpolation (no aliasing risk)
        new_len = int(len(float_samples) * to_rate / from_rate)
        x_old = np.linspace(0, 1, len(float_samples))
        x_new = np.linspace(0, 1, new_len)
        resampled = np.interp(x_new, x_old, float_samples)

    return resampled.astype(np.int16)


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

# Volume level (0.0 - 1.0)
tts_volume = 0.7

# Mute state
mic_muted = False


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


def find_supported_sample_rate(device_index: int | None, desired_rate: int, for_output: bool = False) -> int:
    """Find a sample rate the device supports, preferring the desired rate."""
    candidates = [desired_rate, 48000, 44100, 32000, 16000, 8000]
    for rate in candidates:
        try:
            if for_output:
                supported = pa.is_format_supported(
                    rate,
                    output_device=device_index,
                    output_channels=CHANNELS,
                    output_format=pyaudio.paInt16,
                )
            else:
                supported = pa.is_format_supported(
                    rate,
                    input_device=device_index,
                    input_channels=CHANNELS,
                    input_format=pyaudio.paInt16,
                )
            if supported:
                log.info(f"Device supports {rate} Hz ({'output' if for_output else 'input'})")
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
    """Play TTS WAV audio through the speaker, resampling if needed."""
    global tts_playing
    tts_playing = True

    try:
        # Parse WAV data
        buf = io.BytesIO(audio_data)
        with wave.open(buf, "rb") as wf:
            wav_rate = wf.getframerate()
            wav_channels = wf.getnchannels()
            wav_width = wf.getsampwidth()
            frames = wf.readframes(wf.getnframes())

        output_device = find_output_device()

        # Probe output-specific sample rate support
        play_rate = find_supported_sample_rate(output_device, wav_rate, for_output=True)

        # Resample if the device doesn't support the WAV rate
        if play_rate != wav_rate:
            samples = np.frombuffer(frames, dtype=np.int16)
            if wav_channels > 1:
                samples = samples.reshape(-1, wav_channels)
                resampled_channels = []
                for ch in range(wav_channels):
                    resampled_channels.append(_resample_audio(samples[:, ch], wav_rate, play_rate))
                frames = np.column_stack(resampled_channels).tobytes()
            else:
                frames = _resample_audio(samples, wav_rate, play_rate).tobytes()
            log.info(f"Resampled TTS audio from {wav_rate}Hz to {play_rate}Hz")

        # Apply volume
        if tts_volume < 0.99:
            samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
            samples *= tts_volume
            frames = np.clip(samples, -32768, 32767).astype(np.int16).tobytes()

        stream = pa.open(
            format=pa.get_format_from_width(wav_width),
            channels=wav_channels,
            rate=play_rate,
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


def get_device_channels(device_index: int | None) -> int:
    """Get the number of input channels the device supports."""
    if device_index is not None:
        info = pa.get_device_info_by_index(device_index)
        channels = int(info.get("maxInputChannels", 1))
        return max(1, channels)
    return CHANNELS


def wait_for_audio_device(device_index: int | None, rate: int, channels: int) -> pyaudio.Stream:
    """Block until the audio input device is available, retrying every 5s."""
    global pa
    while True:
        try:
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=device_index,
                frames_per_buffer=CHUNK_SIZE,
            )
            log.info(f"Audio input opened at {rate} Hz, {channels}ch")
            return stream
        except Exception as e:
            log.warning(f"Audio device not ready: {e}. Retrying in 5s...")
            # Re-initialize PyAudio to pick up hotplugged devices
            pa.terminate()
            import time
            time.sleep(5)
            pa = pyaudio.PyAudio()
            # Re-scan for device
            device_index = find_audio_device()


async def audio_stream_handler():
    """Main loop: capture audio and stream to AI server."""
    global tts_buffer, tts_receiving

    device_index = find_audio_device()
    device_rate = find_supported_sample_rate(device_index, SAMPLE_RATE)
    device_channels = get_device_channels(device_index)
    needs_downmix = device_channels > 1
    log.info(f"Device config: {device_rate} Hz, {device_channels}ch (sending raw to server)")
    if needs_downmix:
        log.info(f"Will downmix from {device_channels}ch to mono")

    # Wait until audio device is actually available before connecting
    log.info("Waiting for audio device...")
    loop = asyncio.get_event_loop()
    stream = await loop.run_in_executor(None, wait_for_audio_device, device_index, device_rate, device_channels)

    uri = f"ws://{AI_SERVER_HOST}:8765/ws/audio"

    while True:
        try:
            log.info(f"Connecting to AI server at {uri}...")
            async with websockets.connect(uri, max_size=16 * 1024 * 1024) as ws:
                log.info("Connected to AI server")

                async def send_audio():
                    """Continuously capture and send audio."""
                    loop = asyncio.get_event_loop()
                    while True:
                        if tts_playing or mic_muted:
                            # Don't capture while playing TTS or muted
                            await asyncio.sleep(0.1)
                            continue

                        # Read audio in a thread to avoid blocking
                        audio_data = await loop.run_in_executor(
                            None, stream.read, CHUNK_SIZE, False
                        )

                        if needs_downmix:
                            samples = np.frombuffer(audio_data, dtype=np.int16)
                            samples = samples.reshape(-1, device_channels).mean(axis=1).astype(np.int16)
                            audio_data = samples.tobytes()

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
                                await broadcast_to_ui({"type": "state", "state": "speaking"})

                            elif msg_type == "tts_end":
                                tts_receiving = False
                                if tts_buffer:
                                    audio_data = bytes(tts_buffer)
                                    tts_buffer = bytearray()
                                    await play_tts_audio(audio_data)
                                await broadcast_to_ui({"type": "state", "state": "idle"})
                                await ws.send(json.dumps({"type": "tts_finished"}))

                            elif msg_type in ("transcription", "response", "wake"):
                                # Forward to web UI clients
                                await broadcast_to_ui(msg)

                # Run send and receive concurrently
                await asyncio.gather(send_audio(), receive_messages())

        except (websockets.ConnectionClosed, ConnectionRefusedError, OSError) as e:
            log.warning(f"Connection lost: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)
            # Re-open audio device in case it was the source of the error
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            stream = await loop.run_in_executor(None, wait_for_audio_device, device_index, device_rate, device_channels)
        except Exception as e:
            log.error(f"Unexpected error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            stream = await loop.run_in_executor(None, wait_for_audio_device, device_index, device_rate, device_channels)


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
                elif data.get("type") == "volume":
                    global tts_volume
                    tts_volume = max(0.0, min(1.0, float(data.get("level", 0.7))))
                    log.info(f"Volume set to {tts_volume:.0%}")
                elif data.get("type") == "mute":
                    global mic_muted
                    mic_muted = bool(data.get("muted", False))
                    log.info(f"Mic {'muted' if mic_muted else 'unmuted'}")
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


async def hid_mute_listener():
    """Listen for physical mute button on USB speakerphone via evdev."""
    global mic_muted

    try:
        import evdev
        from evdev import ecodes
    except ImportError:
        log.info("evdev not available, hardware mute button disabled")
        return

    while True:
        try:
            # Find the Anker input device
            devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
            anker_dev = None
            for dev in devices:
                name = dev.name.lower()
                if "anker" in name or "powerconf" in name:
                    anker_dev = dev
                    log.info(f"Found HID device: {dev.name} ({dev.path})")
                    break

            if anker_dev is None:
                log.info("No Anker HID device found, retrying in 10s...")
                await asyncio.sleep(10)
                continue

            # Read events
            async for event in anker_dev.async_read_loop():
                # KEY_MUTE (113) or KEY_MICMUTE (248)
                if event.type == ecodes.EV_KEY and event.value == 1:  # key press
                    if event.code in (ecodes.KEY_MUTE, ecodes.KEY_MICMUTE, 248):
                        mic_muted = not mic_muted
                        log.info(f"Hardware mute button: {'muted' if mic_muted else 'unmuted'}")
                        # Sync to all UI clients
                        await broadcast_to_ui({"type": "mute_sync", "muted": mic_muted})

        except Exception as e:
            log.warning(f"HID listener error: {e}. Retrying in 10s...")
            await asyncio.sleep(10)


async def main():
    """Start all services."""
    log.info("Starting HAL RPi audio streamer...")
    await start_web_server()
    # Run audio handler and HID listener concurrently
    await asyncio.gather(
        audio_stream_handler(),
        hid_mute_listener(),
    )


if __name__ == "__main__":
    asyncio.run(main())
