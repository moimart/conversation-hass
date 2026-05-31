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

from .display_backend import detect_backend as _detect_display_backend

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("hal.rpi")

# Writable dir for the looping photo-frame video, served at /media/ (the
# /app/web tree is mounted read-only, so the loop can't live there). The
# kiosk plays {LOOP_MEDIA_DIR}/loop.mp4 from its own web server.
LOOP_MEDIA_DIR = os.environ.get("LOOP_MEDIA_DIR", "/app/media")
LOOP_VIDEO_FILE = "loop.mp4"


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
        # Set by a `tts_cancel` message from the server (Push-to-Talk
        # interrupts in-flight TTS). The inner write loop in
        # _play_tts_audio_once checks this between chunks and bails out
        # cleanly. Cleared at the start of every play_tts_audio call.
        self._tts_cancel_flag: bool = False

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

        # Display power backend (DPMS). Picks wlr-randr / xset /
        # vcgencmd at startup depending on which the host exposes.
        # None means no backend was usable — the server will see the
        # display feature degrade gracefully (HA entity unavailable,
        # voice tool returns "not supported").
        self._display = _detect_display_backend()

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
        """Get the output sample rate. Only caches if Anker is actually
        present — caching the rate from the 'default' fallback locks us
        in to the wrong rate after the Anker becomes available again."""
        if self._output_rate is not None:
            return self._output_rate
        output_device = self.find_output_device()
        rate = self.find_supported_sample_rate(output_device, 48000, for_output=True)
        if output_device is not None:
            self._output_rate = rate
        return rate

    def probe_device(self):
        """Probe and configure the audio input device."""
        self.device_index = self.find_audio_device()
        self.device_rate = self.find_supported_sample_rate(self.device_index, self.target_sample_rate)
        self.device_channels = self.get_device_channels(self.device_index)
        self.needs_downmix = self.device_channels > 1
        log.info(f"Device config: {self.device_rate} Hz, {self.device_channels}ch (sending raw to server)")
        if self.needs_downmix:
            log.info(f"Will downmix from {self.device_channels}ch to mono")
        # Eagerly probe the Anker OUTPUT too. Doing it at startup
        # surfaces any USB enumeration delay here (where retry/restart
        # is easy) instead of during the first TTS playback (where
        # the user just hears silence). This populates self._output_device
        # and self._output_rate.
        _ = self.get_output_rate()

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
        """Play TTS WAV audio through the speaker, resampling if needed.

        On failure (typically 'Sample format not supported' when the Anker
        USB device dropped out of PortAudio's cached device list),
        reinit PyAudio so the next device scan sees the current device
        set, clear the cached output device/rate, and retry once. This
        is the main symptom users hit: TTS bytes arrive fine but the
        speaker is silent until the container is manually restarted."""
        self.tts_playing = True
        # Fresh playback: clear any cancel flag that might have been left
        # set from a previous cancelled session.
        self._tts_cancel_flag = False
        try:
            try:
                await self._play_tts_audio_once(audio_data)
            except Exception as e:
                log.error(f"TTS playback error: {e}. Reinitialising PyAudio and retrying once.")
                self._reset_pyaudio()
                try:
                    await self._play_tts_audio_once(audio_data)
                except Exception as e2:
                    log.error(f"TTS playback retry also failed: {e2}")
        finally:
            self.tts_playing = False
            self._tts_cancel_flag = False

    def _reset_pyaudio(self):
        """Terminate the current PyAudio instance and re-init. Forces a
        device re-enumeration so USB devices that disappeared from
        PortAudio's stale cache become visible again. Clears the
        output device / rate caches so the next TTS call re-probes."""
        try:
            self.pa.terminate()
        except Exception:
            pass
        self.pa = pyaudio.PyAudio()
        self._output_device = None
        self._output_rate = None

    async def _play_tts_audio_once(self, audio_data: bytes):
        """Single playback attempt. Raises on failure so the caller can retry."""
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

        try:
            chunk = 4096
            for i in range(0, len(frames), chunk):
                if self._tts_cancel_flag:
                    log.info("TTS playback cancelled mid-stream (likely PTT interrupt)")
                    break
                stream.write(frames[i : i + chunk])
                await asyncio.sleep(0)
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass

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

        elif msg_type == "tts_cancel":
            # PTT (or any other interrupt) is taking over — stop any
            # in-flight playback ASAP. The inner write loop checks this
            # flag between chunks; cancellation typically lands within
            # one chunk (~85ms at 48kHz). Drop any audio that's been
            # received but not yet played, and notify the server so its
            # AI-speaking bookkeeping unblocks.
            self._tts_cancel_flag = True
            if self.tts_receiving:
                self.tts_receiving = False
                self.tts_buffer = bytearray()
            await self.broadcast_to_ui({"type": "state", "state": "idle"})
            try:
                await ws.send(json.dumps({"type": "tts_finished"}))
            except Exception:
                pass

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

        elif msg_type == "mute_set":
            # Idempotent setter — server uses this to push the desired
            # mic state (e.g. start_muted on RPi connect) without
            # depending on whatever transient toggle state the RPi
            # happened to have. Only logs / re-broadcasts if it changed.
            desired = bool(msg.get("muted", False))
            if self.mic_muted != desired:
                self.mic_muted = desired
                log.info(f"Mic {'muted' if self.mic_muted else 'unmuted'} (via mute_set)")
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

        elif msg_type == "display_set":
            # Server requested a display-power change. Dispatcher
            # picked the right backend at startup; this is just a
            # subprocess call.
            target_on = (str(msg.get("state", "")).lower() == "on")
            if self._display is None:
                log.warning("display_set received but no display backend available")
                await ws.send(json.dumps({
                    "type": "display_state",
                    "state": "unknown",
                    "available": False,
                }))
                return
            try:
                self._display.set(target_on)
                log.info(
                    f"display set to {'on' if target_on else 'off'} via "
                    f"{self._display.name}"
                )
            except Exception as e:
                log.warning(f"display backend {self._display.name} set failed: {e}")
            # Confirm the new state back to the server. We trust the
            # backend's set() rather than re-querying — query is
            # best-effort and racy with the actual transition.
            await ws.send(json.dumps({
                "type": "display_state",
                "state": "on" if target_on else "off",
                "available": True,
                "backend": self._display.name,
            }))

        elif msg_type == "display_query":
            # Optional health probe — server can ask "what's available?".
            if self._display is None:
                await ws.send(json.dumps({
                    "type": "display_state", "state": "unknown", "available": False,
                }))
            else:
                live = self._display.state()
                await ws.send(json.dumps({
                    "type": "display_state",
                    "state": ("on" if live else "off") if live is not None else "unknown",
                    "available": True,
                    "backend": self._display.name,
                }))

        elif msg_type in (
            "transcription", "response", "wake", "state", "set_theme",
            "show_camera", "stream_start", "stream_stop", "webrtc_signal",
            "play_video", "video_stop", "themes_changed",
            "show_calendar", "hide_calendar",
            "ptt_active",
            "show_photo_frame", "photo_frame_update", "hide_photo_frame",
            "show_photo_frame_video",
        ):
            await self.broadcast_to_ui(msg)

        elif msg_type == "photo_frame_video_sync":
            # Not forwarded to the kiosk — the RPi acts on it: pull the
            # looping video from the AI server (only if our local copy's
            # hash differs) and store it locally for the kiosk to play.
            asyncio.create_task(self._sync_loop_video(
                str(msg.get("hash") or ""),
                str(msg.get("url") or "/api/photo_frame/video"),
            ))

        elif msg_type == "set_orientation":
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

        # How long to let a single WS connection live before forcing a fresh
        # reconnect. Defensive against a class of bugs where the websockets
        # library's keepalive stops firing and the connection appears alive
        # while no data flows. Cheap (~1s blip), guarantees recovery.
        MAX_WS_LIFETIME_S = 1800  # 30 min

        while True:
            try:
                log.info(f"Connecting to AI server at {uri}...")
                async with websockets.connect(
                    uri,
                    max_size=16 * 1024 * 1024,
                    # Tighter than the library defaults (20/20) so half-open
                    # TCP gets detected within ~30s instead of ~40s.
                    ping_interval=15,
                    ping_timeout=15,
                ) as ws:
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

                    # FIRST_COMPLETED so a clean server-side close (which
                    # ends `receive_messages` without raising) still tears
                    # the whole thing down — otherwise `send_audio` can
                    # sit forever in its mute/tts-sleep loop and the
                    # outer reconnect logic never fires.
                    send_task = asyncio.create_task(send_audio())
                    recv_task = asyncio.create_task(receive_messages())
                    try:
                        try:
                            done, pending = await asyncio.wait_for(
                                asyncio.wait(
                                    {send_task, recv_task},
                                    return_when=asyncio.FIRST_COMPLETED,
                                ),
                                timeout=MAX_WS_LIFETIME_S,
                            )
                        except asyncio.TimeoutError:
                            # Lifetime cap hit — neither task finished and
                            # the library never raised. Force a clean
                            # reconnect rather than trust the silent WS.
                            log.info(
                                f"Scheduled reconnect: {MAX_WS_LIFETIME_S}s "
                                "WS lifetime cap reached"
                            )
                            raise websockets.ConnectionClosed(None, None)
                        for t in pending:
                            t.cancel()
                            try:
                                await t
                            except (asyncio.CancelledError, Exception):
                                pass
                        # Surface exceptions from the finisher so the
                        # outer reconnect except-block can handle them.
                        for t in done:
                            exc = t.exception()
                            if exc is not None:
                                raise exc
                        # If we got here, the WS closed cleanly server-side.
                        # Force a reconnect by raising ConnectionClosed.
                        raise websockets.ConnectionClosed(None, None)
                    finally:
                        if not send_task.done():
                            send_task.cancel()
                        if not recv_task.done():
                            recv_task.cancel()

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
                    elif msg_type == "photo_frame_dismissed":
                        # Kiosk auto-dismissed the photo frame (user
                        # interaction or state change). Forward upstream so
                        # the server can tear down its HA subscription.
                        if self._server_ws:
                            try:
                                await self._server_ws.send(json.dumps(data))
                            except Exception as e:
                                log.debug(f"photo_frame_dismissed upstream forward failed: {e}")
                    elif msg_type == "photo_frame_video_error":
                        # Kiosk couldn't load/autoplay the looping video.
                        # Forward upstream so the server falls back to photos.
                        if self._server_ws:
                            try:
                                await self._server_ws.send(json.dumps(data))
                            except Exception as e:
                                log.debug(f"photo_frame_video_error upstream forward failed: {e}")
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
        return await self._upload_snapshot(body)

    async def _upload_snapshot(self, jpeg: bytes) -> web.Response:
        """POST a JPEG to the AI server's /api/snapshot endpoint."""
        url = f"http://{self.ai_server_host}:8765/api/snapshot"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    data=jpeg,
                    headers={"Content-Type": "image/jpeg"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    return web.Response(status=resp.status)
        except Exception as e:
            log.debug(f"snapshot upload failed: {e}")
            return web.Response(status=502)

    async def _sync_loop_video(self, want_hash: str, path: str) -> None:
        """Ensure the local looping video matches `want_hash`, pulling it
        from the AI server over HTTP if it differs. Streams to a temp file
        and renames atomically so the kiosk never reads a partial loop."""
        import hashlib
        dest = os.path.join(LOOP_MEDIA_DIR, LOOP_VIDEO_FILE)
        # Skip the download if our local file already matches.
        if want_hash and os.path.isfile(dest):
            try:
                h = hashlib.sha256()
                with open(dest, "rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(chunk)
                if h.hexdigest() == want_hash:
                    log.info("loop video already up to date (hash match)")
                    return
            except Exception as e:
                log.debug(f"loop video local hash check failed: {e}")

        url = f"http://{self.ai_server_host}:8765{path}"
        try:
            os.makedirs(LOOP_MEDIA_DIR, exist_ok=True)
        except OSError as e:
            log.warning(f"loop video: cannot create {LOOP_MEDIA_DIR}: {e}")
            return
        tmp = dest + ".tmp"
        sha = hashlib.sha256()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=120)
                ) as up:
                    if up.status != 200:
                        log.warning(f"loop video pull got HTTP {up.status} from {url}")
                        return
                    with open(tmp, "wb") as f:
                        async for chunk in up.content.iter_chunked(256 * 1024):
                            sha.update(chunk)
                            f.write(chunk)
            digest = sha.hexdigest()
            if want_hash and digest != want_hash:
                log.warning(
                    f"loop video hash mismatch (got {digest[:12]}, "
                    f"want {want_hash[:12]}) — discarding"
                )
                os.unlink(tmp)
                return
            os.replace(tmp, dest)
            log.info(f"loop video synced: sha={digest[:12]}")
        except Exception as e:
            log.warning(f"loop video sync failed: {e}")
            try:
                os.unlink(tmp)
            except OSError:
                pass

    async def _proxy_to_ai_server(self, request: web.Request) -> web.Response:
        """Forward a GET request to the AI server transparently.

        Used for theme catalog and per-theme static files so the kiosk
        can keep talking only to localhost:8080.
        """
        path = request.path  # e.g. /api/themes or /themes/matrix/effect.js
        if request.query_string:
            path = f"{path}?{request.query_string}"
        url = f"http://{self.ai_server_host}:8765{path}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as up:
                    body = await up.read()
                    return web.Response(
                        status=up.status,
                        body=body,
                        content_type=up.content_type or "application/octet-stream",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
                    )
        except Exception as e:
            log.debug(f"proxy {path} failed: {e}")
            return web.Response(status=502)

    async def cdp_snapshot_loop(self):
        """Periodically capture the kiosk via CDP and post it upstream.

        Replaces the old html2canvas/in-page publisher: the screenshot
        comes straight from the running Chromium's compositor, so live
        video frames, animations, custom fonts, masks, and filters are
        all rendered exactly as the user sees them. Disabled when
        CHROMIUM_DEBUG_URL is unset.
        """
        from .cdp_snapshot import capture_screenshot
        debug_url = os.environ.get("CHROMIUM_DEBUG_URL", "").strip()
        if not debug_url:
            log.info("CDP snapshot disabled (CHROMIUM_DEBUG_URL not set)")
            return
        try:
            interval = max(5, int(os.environ.get("SNAPSHOT_INTERVAL_S", "60")))
        except ValueError:
            interval = 60
        log.info(f"CDP snapshot loop: {debug_url} every {interval}s")
        # Brief startup delay so the kiosk page has time to settle.
        await asyncio.sleep(5)
        while True:
            try:
                jpeg = await capture_screenshot(debug_url)
                if jpeg:
                    await self._upload_snapshot(jpeg)
            except Exception as e:
                log.debug(f"cdp snapshot iteration failed: {e}")
            await asyncio.sleep(interval)

    @staticmethod
    @web.middleware
    async def _no_cache_middleware(request: web.Request, handler):
        """Tell browsers never to cache the kiosk's UI assets.

        The kiosk Chromium aggressively caches HTML/CSS/JS by default,
        so deploys of new orb tweaks are invisible until the browser
        is restarted. With these headers every refresh re-downloads
        the assets and the kiosk picks up the latest build immediately.
        """
        response = await handler(request)
        if isinstance(response, web.WebSocketResponse):
            return response
        # Decide off BOTH the request path and the content-type. Static
        # files are served by aiohttp's FileResponse, which doesn't set
        # Content-Type until send time (AFTER this middleware runs) — so a
        # content-type-only check silently skips every static .js/.css and
        # the kiosk keeps serving stale modules from cache. Keying off the
        # path extension (always known here) closes that gap. This matters
        # for dynamically-imported modules: app.js refreshes on navigation
        # but import("./foo.js") comes from cache unless told otherwise.
        path = request.path.lower()
        ct = response.headers.get("Content-Type", "")
        is_asset = (
            path == "/"
            or path.endswith((".html", ".js", ".mjs", ".css", ".json",
                              ".woff", ".woff2", ".ttf", ".otf"))
            or any(t in ct for t in ("html", "css", "javascript", "json", "font"))
        )
        if is_asset:
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    async def start_web_server(self):
        """Start the aiohttp web server for serving UI and WebSocket."""
        app = web.Application(
            client_max_size=8 * 1024 * 1024,
            middlewares=[self._no_cache_middleware],
        )
        app.router.add_get("/ws", self.websocket_handler)
        app.router.add_get("/", self._serve_index)
        app.router.add_post("/api/snapshot", self._proxy_snapshot)
        app.router.add_post("/api/music/state", self._set_music_state)
        # Theme plug-in passthrough: list + per-theme assets forward to the AI server.
        app.router.add_get("/api/themes", self._proxy_to_ai_server)
        app.router.add_get(r"/themes/{name}/{filename}", self._proxy_to_ai_server)
        # Looping photo-frame video lives in a writable dir (the /app/web
        # tree is mounted read-only). Register BEFORE the "/" catch-all
        # static — aiohttp matches routes in registration order.
        try:
            os.makedirs(LOOP_MEDIA_DIR, exist_ok=True)
        except OSError as e:
            log.warning(f"loop media dir {LOOP_MEDIA_DIR} not creatable: {e}")
        app.router.add_static("/media/", LOOP_MEDIA_DIR)
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
            self.cdp_snapshot_loop(),
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
