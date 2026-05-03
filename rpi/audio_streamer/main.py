"""RPi audio streamer: captures mic -> WebSocket -> AI server, plays back TTS."""

import asyncio
import io
import json
import logging
import os
import time
import wave

import aiohttp
import numpy as np
import pyaudio
import websockets
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("hal.rpi")


def _resample_audio(samples: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """
    Resample int16 audio via linear interpolation (used for TTS upsampling).

    For upsampling (e.g. 22050->48000) linear interpolation is sufficient
    since there is no aliasing risk.
    """
    if from_rate == to_rate:
        return samples

    float_samples = samples.astype(np.float32)
    new_len = int(len(float_samples) * to_rate / from_rate)
    x_old = np.linspace(0, 1, len(float_samples))
    x_new = np.linspace(0, 1, new_len)
    resampled = np.interp(x_new, x_old, float_samples)
    return resampled.astype(np.int16)


class AudioManager:
    """Manages all audio state: capture, playback, volume, mute, device probing."""

    def __init__(
        self,
        ai_server_host: str,
        sample_rate: int = 16000,
        channels: int = 1,
        chunk_size: int = 4096,
    ):
        self.ai_server_host = ai_server_host
        self.target_sample_rate = sample_rate
        self.target_channels = channels
        self.chunk_size = chunk_size

        # PyAudio
        self.pa = pyaudio.PyAudio()

        # Device info (populated by probe_device)
        self.device_index: int | None = None
        self.device_rate: int = sample_rate
        self.device_channels: int = channels
        self.needs_downmix: bool = False

        # Output device (cached after first probe)
        self._output_device: int | None = None
        self._output_rate: int | None = None

        # Playback state
        self.tts_playing: bool = False
        self.tts_volume: float = 0.7
        self.mic_muted: bool = False

        # TTS receive buffers
        self.tts_buffer = bytearray()
        self.tts_receiving: bool = False
        self.chime_buffer = bytearray()
        self.chime_receiving: bool = False

        # UI clients
        self.ui_clients: set[web.WebSocketResponse] = set()

        # Sendspin coordination: when a Sendspin stream is active on
        # the local daemon, hardware volume buttons target the MA
        # player instead of HAL's TTS volume. Daemon hooks update
        # this via POST /api/music/state.
        self.music_playing: bool = False

        # Reference to the live server WebSocket (set by the audio
        # stream handler when connected). Used to forward
        # ma_volume_adjust messages.
        self._server_ws = None

    # --- Device probing ---

    def find_audio_device(self) -> int | None:
        """Find the Anker Powerconf S330 or use default input."""
        for i in range(self.pa.get_device_count()):
            info = self.pa.get_device_info_by_index(i)
            name = info.get("name", "").lower()
            if "anker" in name or "powerconf" in name:
                log.info(f"Found Anker device: {info['name']} (index {i})")
                log.info(f"  Default sample rate: {info.get('defaultSampleRate')}")
                log.info(f"  Max input channels: {info.get('maxInputChannels')}")
                return i
        log.info("Anker device not found, using default input")
        return None

    def find_supported_sample_rate(self, device_index: int | None, desired_rate: int, for_output: bool = False) -> int:
        """Find a sample rate the device supports, preferring the desired rate."""
        candidates = [desired_rate, 48000, 44100, 32000, 16000, 8000]
        for rate in candidates:
            try:
                if for_output:
                    self.pa.is_format_supported(
                        rate,
                        output_device=device_index,
                        output_channels=self.target_channels,
                        output_format=pyaudio.paInt16,
                    )
                else:
                    self.pa.is_format_supported(
                        rate,
                        input_device=device_index,
                        input_channels=self.target_channels,
                        input_format=pyaudio.paInt16,
                    )
                log.info(f"Device supports {rate} Hz ({'output' if for_output else 'input'})")
                return rate
            except ValueError:
                continue
        # Fallback: use device default
        if device_index is not None:
            info = self.pa.get_device_info_by_index(device_index)
            default_rate = int(info.get("defaultSampleRate", 44100))
            log.info(f"Falling back to device default: {default_rate} Hz")
            return default_rate
        return 44100

    def find_output_device(self) -> int | None:
        """Find the output device (Anker speaker). Caches result."""
        if self._output_device is not None:
            return self._output_device
        for i in range(self.pa.get_device_count()):
            info = self.pa.get_device_info_by_index(i)
            name = info.get("name", "").lower()
            if ("anker" in name or "powerconf" in name) and info.get("maxOutputChannels", 0) > 0:
                log.info(f"Found Anker output: {info['name']} (index {i})")
                self._output_device = i
                return i
        log.info("Using default audio output")
        return None

    def get_device_channels(self, device_index: int | None) -> int:
        """Get the number of input channels the device supports."""
        if device_index is not None:
            info = self.pa.get_device_info_by_index(device_index)
            channels = int(info.get("maxInputChannels", 1))
            return max(1, channels)
        return self.target_channels

    def get_output_rate(self) -> int:
        """Get the output sample rate. Caches result."""
        if self._output_rate is not None:
            return self._output_rate
        output_device = self.find_output_device()
        self._output_rate = self.find_supported_sample_rate(output_device, 48000, for_output=True)
        return self._output_rate

    def probe_device(self):
        """Probe and configure the audio input device."""
        self.device_index = self.find_audio_device()
        self.device_rate = self.find_supported_sample_rate(self.device_index, self.target_sample_rate)
        self.device_channels = self.get_device_channels(self.device_index)
        self.needs_downmix = self.device_channels > 1
        log.info(f"Device config: {self.device_rate} Hz, {self.device_channels}ch (sending raw to server)")
        if self.needs_downmix:
            log.info(f"Will downmix from {self.device_channels}ch to mono")

    # --- Audio stream management ---

    def open_input_stream(self) -> pyaudio.Stream:
        """Block until the audio input device is available, retrying every 5s."""
        while True:
            try:
                stream = self.pa.open(
                    format=pyaudio.paInt16,
                    channels=self.device_channels,
                    rate=self.device_rate,
                    input=True,
                    input_device_index=self.device_index,
                    frames_per_buffer=self.chunk_size,
                )
                log.info(f"Audio input opened at {self.device_rate} Hz, {self.device_channels}ch")
                return stream
            except Exception as e:
                log.warning(f"Audio device not ready: {e}. Retrying in 5s...")
                self.pa.terminate()
                time.sleep(5)
                self.pa = pyaudio.PyAudio()
                self.device_index = self.find_audio_device()

    def close_stream(self, stream: pyaudio.Stream):
        """Safely close an audio stream."""
        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass

    # --- Audio processing ---

    def downmix_to_mono(self, audio_data: bytes) -> bytes:
        """Downmix multi-channel int16 PCM to mono."""
        samples = np.frombuffer(audio_data, dtype=np.int16)
        samples = samples.reshape(-1, self.device_channels).mean(axis=1).astype(np.int16)
        return samples.tobytes()

    def apply_volume(self, frames: bytes) -> bytes:
        """Scale audio samples by the current volume level."""
        if self.tts_volume >= 0.99:
            return frames
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
        samples *= self.tts_volume
        return np.clip(samples, -32768, 32767).astype(np.int16).tobytes()

    # --- TTS playback ---

    async def play_tts_audio(self, audio_data: bytes):
        """Play TTS WAV audio through the speaker, resampling if needed."""
        self.tts_playing = True
        try:
            buf = io.BytesIO(audio_data)
            with wave.open(buf, "rb") as wf:
                wav_rate = wf.getframerate()
                wav_channels = wf.getnchannels()
                wav_width = wf.getsampwidth()
                frames = wf.readframes(wf.getnframes())

            output_device = self.find_output_device()
            play_rate = self.get_output_rate()

            # Resample if needed
            if play_rate != wav_rate:
                samples = np.frombuffer(frames, dtype=np.int16)
                if wav_channels > 1:
                    samples = samples.reshape(-1, wav_channels)
                    resampled = []
                    for ch in range(wav_channels):
                        resampled.append(_resample_audio(samples[:, ch], wav_rate, play_rate))
                    frames = np.column_stack(resampled).tobytes()
                else:
                    frames = _resample_audio(samples, wav_rate, play_rate).tobytes()
                log.info(f"Resampled TTS audio from {wav_rate}Hz to {play_rate}Hz")

            frames = self.apply_volume(frames)

            stream = self.pa.open(
                format=self.pa.get_format_from_width(wav_width),
                channels=wav_channels,
                rate=play_rate,
                output=True,
                output_device_index=output_device,
            )

            chunk = 4096
            for i in range(0, len(frames), chunk):
                stream.write(frames[i : i + chunk])
                await asyncio.sleep(0)

            stream.stop_stream()
            stream.close()

        except Exception as e:
            log.error(f"TTS playback error: {e}")
        finally:
            self.tts_playing = False

    # --- UI broadcasting ---

    async def broadcast_to_ui(self, msg: dict):
        """Send a message to all connected web UI clients."""
        data = json.dumps(msg)
        dead = set()
        for ws in self.ui_clients:
            try:
                await ws.send_str(data)
            except Exception:
                dead.add(ws)
        self.ui_clients.difference_update(dead)

    # --- WebSocket message handling ---

    async def handle_server_message(self, msg_type: str, msg: dict, ws):
        """Handle a JSON message from the AI server."""
        if msg_type == "chime_start":
            self.chime_receiving = True
            self.chime_buffer = bytearray()

        elif msg_type == "chime_end":
            self.chime_receiving = False
            if self.chime_buffer:
                audio_data = bytes(self.chime_buffer)
                self.chime_buffer = bytearray()
                await self.play_tts_audio(audio_data)

        elif msg_type == "tts_start":
            self.tts_receiving = True
            self.tts_buffer = bytearray()
            await self.broadcast_to_ui({"type": "state", "state": "speaking"})

        elif msg_type == "tts_end":
            self.tts_receiving = False
            if self.tts_buffer:
                audio_data = bytes(self.tts_buffer)
                self.tts_buffer = bytearray()
                await self.play_tts_audio(audio_data)
            await self.broadcast_to_ui({"type": "state", "state": "idle"})
            await ws.send(json.dumps({"type": "tts_finished"}))

        elif msg_type == "volume_adjust":
            step = float(msg.get("step", 0.1))
            self.tts_volume = max(0.0, min(1.0, self.tts_volume + step))
            log.info(f"Volume adjusted by {step:+.0%}: now {self.tts_volume:.0%}")
            await self.broadcast_to_ui({"type": "volume_sync", "level": self.tts_volume})
            await ws.send(json.dumps({"type": "volume_sync", "level": self.tts_volume}))

        elif msg_type == "mute_toggle":
            self.mic_muted = not self.mic_muted
            log.info(f"Mic {'muted' if self.mic_muted else 'unmuted'} (via server)")
            await self.broadcast_to_ui({"type": "mute_sync", "muted": self.mic_muted})
            await ws.send(json.dumps({"type": "mute_sync", "muted": self.mic_muted}))

        elif msg_type == "mute_query":
            await ws.send(json.dumps({"type": "mute_sync", "muted": self.mic_muted}))

        elif msg_type == "ping":
            await ws.send(json.dumps({"type": "pong"}))

        elif msg_type == "volume":
            try:
                level = float(msg.get("level", 0.7))
            except (TypeError, ValueError):
                level = 0.7
            self.tts_volume = max(0.0, min(1.0, level))
            log.info(f"Volume set to {self.tts_volume:.0%} (via server)")
            await self.broadcast_to_ui({"type": "volume_sync", "level": self.tts_volume})
            await ws.send(json.dumps({"type": "volume_sync", "level": self.tts_volume}))

        elif msg_type in (
            "transcription", "response", "wake", "state", "set_theme",
            "show_camera", "stream_start", "stream_stop", "webrtc_signal",
            "play_video", "video_stop",
        ):
            await self.broadcast_to_ui(msg)

    def handle_binary_data(self, data: bytes):
        """Handle binary data from the AI server (audio chunks)."""
        if self.chime_receiving:
            self.chime_buffer.extend(data)
        elif self.tts_receiving:
            self.tts_buffer.extend(data)

    # --- Main audio loop ---

    async def audio_stream_handler(self):
        """Main loop: capture audio and stream to AI server."""
        self.probe_device()

        log.info("Waiting for audio device...")
        loop = asyncio.get_event_loop()
        stream = await loop.run_in_executor(None, self.open_input_stream)

        uri = f"ws://{self.ai_server_host}:8765/ws/audio"

        while True:
            try:
                log.info(f"Connecting to AI server at {uri}...")
                async with websockets.connect(uri, max_size=16 * 1024 * 1024) as ws:
                    log.info("Connected to AI server")
                    self._server_ws = ws

                    chunks_sent = 0
                    last_health = time.monotonic()

                    async def send_audio():
                        nonlocal stream, chunks_sent, last_health
                        while True:
                            if self.tts_playing or self.mic_muted:
                                await asyncio.sleep(0.1)
                                continue

                            try:
                                audio_data = await asyncio.wait_for(
                                    loop.run_in_executor(
                                        None, stream.read, self.chunk_size, False
                                    ),
                                    timeout=5.0,
                                )
                            except asyncio.TimeoutError:
                                log.error("stream.read timed out (5s) — reopening audio device")
                                self.close_stream(stream)
                                stream = await loop.run_in_executor(None, self.open_input_stream)
                                continue
                            except Exception as e:
                                log.error(f"stream.read failed: {e} — reopening audio device")
                                self.close_stream(stream)
                                stream = await loop.run_in_executor(None, self.open_input_stream)
                                continue

                            if self.needs_downmix:
                                audio_data = self.downmix_to_mono(audio_data)

                            await ws.send(audio_data)
                            chunks_sent += 1

                            # Health log every 60s
                            now = time.monotonic()
                            if now - last_health >= 60.0:
                                log.info(f"Client health: chunks_sent={chunks_sent}, tts_playing={self.tts_playing}, mic_muted={self.mic_muted}")
                                last_health = now

                    async def receive_messages():
                        async for message in ws:
                            if isinstance(message, bytes):
                                self.handle_binary_data(message)
                            else:
                                msg = json.loads(message)
                                await self.handle_server_message(msg.get("type", ""), msg, ws)

                    await asyncio.gather(send_audio(), receive_messages())

            except (websockets.ConnectionClosed, ConnectionRefusedError, OSError) as e:
                log.warning(f"Connection lost: {e}. Reconnecting in 5s...")
                self._server_ws = None
                await asyncio.sleep(5)
                self.close_stream(stream)
                stream = await loop.run_in_executor(None, self.open_input_stream)
            except Exception as e:
                log.error(f"Unexpected error: {e}. Reconnecting in 5s...")
                self._server_ws = None
                await asyncio.sleep(5)
                self.close_stream(stream)
                stream = await loop.run_in_executor(None, self.open_input_stream)

    # --- UI WebSocket handler ---

    async def websocket_handler(self, request):
        """Handle web UI WebSocket connections."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ui_clients.add(ws)
        log.info(f"Web UI client connected (total: {len(self.ui_clients)})")

        # Send current state to new client
        await ws.send_json({"type": "mute_sync", "muted": self.mic_muted})
        await ws.send_json({"type": "volume_sync", "level": self.tts_volume})

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    msg_type = data.get("type")
                    if msg_type == "ping":
                        await ws.send_json({"type": "pong"})
                    elif msg_type == "volume":
                        self.tts_volume = max(0.0, min(1.0, float(data.get("level", 0.7))))
                        log.info(f"Volume set to {self.tts_volume:.0%}")
                        # Echo upstream so the AI server can republish to MQTT/HA.
                        if self._server_ws:
                            try:
                                await self._server_ws.send(json.dumps({"type": "volume_sync", "level": self.tts_volume}))
                            except Exception as e:
                                log.debug(f"volume_sync upstream forward failed: {e}")
                    elif msg_type == "mute":
                        self.mic_muted = bool(data.get("muted", False))
                        log.info(f"Mic {'muted' if self.mic_muted else 'unmuted'}")
                        if self._server_ws:
                            try:
                                await self._server_ws.send(json.dumps({"type": "mute_sync", "muted": self.mic_muted}))
                            except Exception as e:
                                log.debug(f"mute_sync upstream forward failed: {e}")
                    elif msg_type == "webrtc_signal":
                        # Kiosk → server WebRTC signaling. Forward upstream as-is.
                        if self._server_ws:
                            try:
                                await self._server_ws.send(json.dumps(data))
                            except Exception as e:
                                log.debug(f"webrtc_signal upstream forward failed: {e}")
        finally:
            self.ui_clients.discard(ws)
            log.info(f"Web UI client disconnected (total: {len(self.ui_clients)})")

        return ws

    # --- HID volume listener ---

    async def hid_volume_listener(self):
        """Listen for physical volume buttons on USB speakerphone via evdev."""
        try:
            import evdev
            from evdev import ecodes
        except ImportError:
            log.info("evdev not available, hardware volume buttons disabled")
            return

        while True:
            try:
                anker_devs = []
                for path in evdev.list_devices():
                    dev = evdev.InputDevice(path)
                    name = dev.name.lower()
                    if "anker" in name or "powerconf" in name:
                        anker_devs.append(dev)
                        log.info(f"HID volume listener: {dev.name} ({dev.path})")
                    else:
                        dev.close()

                if not anker_devs:
                    await asyncio.sleep(10)
                    continue

                mgr = self  # capture for nested function

                async def read_device(dev):
                    log.info(f"Listening for volume events on {dev.path}")
                    async for event in dev.async_read_loop():
                        if event.type != ecodes.EV_SYN:
                            code_name = ecodes.bytype.get(event.type, {}).get(event.code, event.code)
                            log.info(f"HID [{dev.path}]: type={event.type} code={event.code} ({code_name}) value={event.value}")

                        if event.type == ecodes.EV_KEY and event.value == 1:
                            step = 0.0
                            if event.code == 115:  # KEY_VOLUMEUP
                                step = 0.1
                            elif event.code == 114:  # KEY_VOLUMEDOWN
                                step = -0.1
                            if step == 0.0:
                                continue

                            # While Sendspin music is playing, hardware
                            # buttons drive the MA player; otherwise
                            # they adjust HAL's TTS volume locally.
                            if mgr.music_playing and mgr._server_ws is not None:
                                try:
                                    await mgr._server_ws.send(json.dumps(
                                        {"type": "ma_volume_adjust", "step": step}
                                    ))
                                    log.info(f"Hardware vol {'up' if step > 0 else 'down'} → MA player")
                                except Exception as e:
                                    log.warning(f"ma_volume_adjust send failed: {e}")
                            else:
                                mgr.tts_volume = max(0.0, min(1.0, mgr.tts_volume + step))
                                log.info(f"Hardware vol {'up' if step > 0 else 'down'}: {mgr.tts_volume:.0%}")
                                await mgr.broadcast_to_ui({"type": "volume_sync", "level": mgr.tts_volume})
                                # Echo upstream so the AI server republishes to MQTT/HA.
                                if mgr._server_ws is not None:
                                    try:
                                        await mgr._server_ws.send(json.dumps(
                                            {"type": "volume_sync", "level": mgr.tts_volume}
                                        ))
                                    except Exception as e:
                                        log.debug(f"volume_sync upstream forward failed: {e}")

                await asyncio.gather(*[read_device(dev) for dev in anker_devs])

            except Exception as e:
                log.warning(f"HID volume listener error: {e}. Retrying in 10s...")
                await asyncio.sleep(10)

    # --- Web server ---

    @staticmethod
    async def _serve_index(request):
        return web.FileResponse("/app/web/index.html")

    async def _set_music_state(self, request: web.Request) -> web.Response:
        """Sendspin daemon hook → tracks whether MA music is playing."""
        try:
            data = await request.json()
            self.music_playing = bool(data.get("playing", False))
            log.info(f"Sendspin music_playing={self.music_playing}")
        except Exception as e:
            log.warning(f"music/state parse failed: {e}")
            return web.Response(status=400)
        return web.Response(status=204)

    async def _proxy_snapshot(self, request: web.Request) -> web.Response:
        """Forward a JPEG snapshot from the kiosk page to the AI server."""
        body = await request.read()
        if not body:
            return web.Response(status=204)
        url = f"http://{self.ai_server_host}:8765/api/snapshot"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    data=body,
                    headers={"Content-Type": "image/jpeg"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    return web.Response(status=resp.status)
        except Exception as e:
            log.debug(f"snapshot proxy failed: {e}")
            return web.Response(status=502)

    async def start_web_server(self):
        """Start the aiohttp web server for serving UI and WebSocket."""
        app = web.Application(client_max_size=8 * 1024 * 1024)
        app.router.add_get("/ws", self.websocket_handler)
        app.router.add_get("/", self._serve_index)
        app.router.add_post("/api/snapshot", self._proxy_snapshot)
        app.router.add_post("/api/music/state", self._set_music_state)
        app.router.add_static("/", "/app/web")

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", 8080)
        await site.start()
        log.info("Web UI available at http://0.0.0.0:8080")

    # --- Entry point ---

    async def run(self):
        """Start all services."""
        log.info("Starting HAL RPi audio streamer...")
        await self.start_web_server()

        await asyncio.gather(
            self.audio_stream_handler(),
            self.hid_volume_listener(),
        )


def main():
    """Create AudioManager from environment and run."""
    mgr = AudioManager(
        ai_server_host=os.environ.get("AI_SERVER_HOST", "192.168.1.100"),
        sample_rate=int(os.environ.get("SAMPLE_RATE", "16000")),
        channels=int(os.environ.get("CHANNELS", "1")),
        chunk_size=int(os.environ.get("CHUNK_SIZE", "4096")),
    )
    asyncio.run(mgr.run())


if __name__ == "__main__":
    main()
