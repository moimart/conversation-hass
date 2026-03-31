"""Audio processing pipeline: VAD, transcription, speaker filtering."""

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .transcriber import BaseTranscriber, create_transcriber
from .speaker_filter import SpeakerFilter

log = logging.getLogger("hal.pipeline")

# Silence threshold: seconds of silence after speech to trigger command processing
SILENCE_THRESHOLD = 1.5

# Minimum speech duration to attempt transcription (filters ambient noise bursts)
MIN_SPEECH_DURATION = 0.3  # seconds


class AudioPipeline:
    """Processes raw audio: VAD → transcription → speaker filtering."""

    def __init__(self, sample_rate: int = 16000, stt_engine: str = "whisper", stt_model: str = ""):
        self.sample_rate = sample_rate  # incoming audio rate (may be 48kHz)
        self._target_rate = 16000       # rate expected by VAD and STT
        self._stt_engine = stt_engine
        self._stt_model = stt_model
        self.transcriber: BaseTranscriber | None = None
        self.speaker_filter: SpeakerFilter | None = None

        # VAD state — runs in its own executor to never block the event loop
        self._vad_model = None
        self._vad_executor = ThreadPoolExecutor(max_workers=1)
        self._speech_active = False
        self._speech_start_time: float = 0.0
        self._silence_start: float | None = None
        self._last_successful_transcription: float = 0.0
        self._consecutive_empty: int = 0
        self._vad_reset_interval = 300.0  # reload VAD every 5 min without transcription
        self._last_vad_reset: float = 0.0
        self._reload_pending = False
        self._reload_count = 0
        self._chunk_count: int = 0
        self._last_health_log: float = 0.0
        self._health_log_interval = 60.0  # log health every 60s

        # Echo suppression: when AI is speaking, suppress transcription
        self._ai_speaking = False
        self._ai_speaking_since: float = 0.0
        self._ai_speaking_timeout = 30.0  # safety: auto-clear after 30s

        # Audio buffer for current speech segment
        self._audio_buffer = bytearray()
        self._partial_buffer = bytearray()
        self._partial_interval = 0.5  # seconds between partial transcriptions
        self._last_partial_time = 0.0

    async def initialize(self):
        """Load models."""
        log.info("Loading VAD model...")
        from silero_vad import load_silero_vad
        self._vad_model = load_silero_vad(onnx=True)

        log.info("Loading transcription model...")
        self.transcriber = create_transcriber(self._stt_engine, self._stt_model)
        await self.transcriber.initialize()

        log.info("Initializing speaker filter...")
        self.speaker_filter = SpeakerFilter()

        log.info("Audio pipeline ready.")

    def _to_16k_float(self, audio_bytes: bytes) -> np.ndarray:
        """Convert raw PCM bytes to 16kHz float32 using librosa for resampling."""
        audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if self.sample_rate != self._target_rate:
            import librosa
            audio = librosa.resample(audio, orig_sr=self.sample_rate, target_sr=self._target_rate)
        return audio

    def set_ai_speaking(self, speaking: bool):
        """Mark whether the AI is currently speaking (for echo suppression)."""
        self._ai_speaking = speaking
        if speaking:
            self._ai_speaking_since = time.monotonic()
            self._speech_active = False
            self._audio_buffer.clear()
            self._partial_buffer.clear()
        else:
            self._ai_speaking_since = 0.0

    async def _reload_models(self):
        """Reload VAD and STT models in a thread to avoid blocking the event loop."""
        loop = asyncio.get_event_loop()
        self._reload_count += 1
        log.info(f"Model reload #{self._reload_count} starting")

        try:
            # Reload VAD in executor with timeout
            def _reload_vad():
                from silero_vad import load_silero_vad
                return load_silero_vad(onnx=True)

            try:
                new_vad = await asyncio.wait_for(
                    loop.run_in_executor(None, _reload_vad),
                    timeout=30.0,
                )
                self._vad_model = new_vad
                log.info("VAD model reloaded")
            except asyncio.TimeoutError:
                log.error("VAD reload timed out (30s)")
            except Exception as e:
                log.error(f"VAD reload failed: {e}")

            # Reload STT in executor with timeout
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, self.transcriber._reload_model),
                    timeout=60.0,
                )
                self.transcriber._consecutive_failures = 0
                log.info("STT model reloaded")
            except asyncio.TimeoutError:
                log.error("STT reload timed out (60s)")
            except Exception as e:
                log.error(f"STT reload failed: {e}")

            self._last_vad_reset = time.monotonic()
            self._consecutive_empty = 0
        finally:
            self._reload_pending = False

        # Exponential backoff: double the interval after each reload, cap at 1 hour
        if self._reload_count > 1:
            self._vad_reset_interval = min(self._vad_reset_interval * 2, 3600.0)
            log.info(f"Next reload interval: {self._vad_reset_interval:.0f}s")

    def _run_vad(self, audio_float: np.ndarray) -> bool:
        """Run VAD synchronously in a thread. Returns True if speech detected."""
        import torch
        chunk_tensor = torch.from_numpy(audio_float)
        vad_chunk_size = 512
        for i in range(0, len(chunk_tensor), vad_chunk_size):
            segment = chunk_tensor[i : i + vad_chunk_size]
            if len(segment) < vad_chunk_size:
                segment = torch.nn.functional.pad(segment, (0, vad_chunk_size - len(segment)))
            confidence = self._vad_model(segment, self._target_rate).item()
            if confidence > 0.5:
                return True
        return False

    async def process_chunk(self, audio_bytes: bytes) -> dict | None:
        """
        Process a chunk of raw PCM 16-bit LE audio.

        Returns a transcription result dict or None.
        """
        if self._ai_speaking:
            # Safety timeout: auto-clear if stuck for too long
            if self._ai_speaking_since > 0 and (time.monotonic() - self._ai_speaking_since) > self._ai_speaking_timeout:
                log.warning(f"AI speaking timeout after {self._ai_speaking_timeout}s — auto-clearing")
                self.set_ai_speaking(False)
            else:
                return None

        now = time.monotonic()
        self._chunk_count += 1

        # Periodic health log
        if now - self._last_health_log >= self._health_log_interval:
            since_last = now - self._last_successful_transcription if self._last_successful_transcription > 0 else -1
            log.info(f"Pipeline health: chunks={self._chunk_count}, last_transcription={since_last:.0f}s ago, empty_streak={self._consecutive_empty}, speech_active={self._speech_active}")
            self._last_health_log = now

        # Self-healing: reload VAD + STT if no successful transcription in 5 minutes
        # But only reload ONCE — repeated reloads fragment CUDA memory and eventually hang
        if not self._reload_pending and now - self._last_vad_reset > self._vad_reset_interval:
            needs_reload = False
            if self._last_successful_transcription == 0:
                needs_reload = (self._chunk_count > self._vad_reset_interval * 12)
            elif (now - self._last_successful_transcription) > self._vad_reset_interval:
                needs_reload = True

            if needs_reload:
                self._reload_pending = True
                log.warning(f"No successful transcription in {self._vad_reset_interval:.0f}s — scheduling model reload")
                asyncio.get_event_loop().create_task(self._reload_models())

        # Convert bytes to float32 numpy array
        audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)
        audio_float = audio_int16.astype(np.float32) / 32768.0

        # Resample to 16kHz for VAD if incoming rate differs
        if self.sample_rate != self._target_rate:
            import librosa
            vad_audio = librosa.resample(audio_float, orig_sr=self.sample_rate, target_sr=self._target_rate)
        else:
            vad_audio = audio_float

        # Run VAD in an executor so it can never freeze the event loop
        loop = asyncio.get_event_loop()
        try:
            is_speech = await asyncio.wait_for(
                loop.run_in_executor(self._vad_executor, self._run_vad, vad_audio),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            log.error("VAD inference hung (5s timeout) — reloading model and executor")
            # Replace the stuck executor
            try:
                self._vad_executor.shutdown(wait=False)
            except Exception:
                pass
            self._vad_executor = ThreadPoolExecutor(max_workers=1)
            # Reload VAD model
            try:
                from silero_vad import load_silero_vad
                self._vad_model = load_silero_vad(onnx=True)
                log.info("VAD model reloaded after hang")
            except Exception as e:
                log.error(f"VAD reload failed: {e}")
            return None
        except Exception as e:
            log.error(f"VAD error: {e}")
            return None

        if is_speech:
            if not self._speech_active:
                log.info("Speech started")
                self._speech_start_time = now
            self._speech_active = True
            self._silence_start = None
            self._audio_buffer.extend(audio_bytes)
            self._partial_buffer.extend(audio_bytes)

            # Emit partial transcriptions periodically
            if now - self._last_partial_time >= self._partial_interval and len(self._partial_buffer) > 0:
                self._last_partial_time = now
                partial_audio = self._to_16k_float(bytes(self._partial_buffer))
                text = await self.transcriber.transcribe(partial_audio)
                if text and text.strip():
                    return {"text": text.strip(), "is_partial": True, "speaker": "human"}
        else:
            if self._speech_active:
                # Speech just ended
                if self._silence_start is None:
                    self._silence_start = now
                    buf_duration = len(self._audio_buffer) / (self.sample_rate * 2)  # 16-bit = 2 bytes/sample
                    log.info(f"Speech ended, buffer={buf_duration:.1f}s, waiting for silence threshold")
                elif now - self._silence_start >= SILENCE_THRESHOLD:
                    # Enough silence — finalize transcription
                    self._speech_active = False
                    buf_bytes = len(self._audio_buffer)
                    buf_duration = buf_bytes / (self.sample_rate * 2)

                    if buf_duration < MIN_SPEECH_DURATION:
                        # Too short — ambient noise, not speech
                        log.debug(f"Discarding noise burst ({buf_duration:.2f}s < {MIN_SPEECH_DURATION}s)")
                        self._audio_buffer.clear()
                        self._partial_buffer.clear()
                        self._silence_start = None
                    elif buf_bytes > 0:
                        full_audio = self._to_16k_float(bytes(self._audio_buffer))
                        log.info(f"Finalizing: buffer={buf_bytes} bytes, audio={len(full_audio)} samples ({len(full_audio)/16000:.1f}s)")
                        text = await self.transcriber.transcribe(full_audio)

                        self._audio_buffer.clear()
                        self._partial_buffer.clear()
                        self._silence_start = None

                        if text and text.strip():
                            self._last_successful_transcription = now
                            self._consecutive_empty = 0
                            # Reset reload backoff on success
                            if self._reload_count > 0:
                                self._reload_count = 0
                                self._vad_reset_interval = 300.0
                                log.info("Transcription working — reset reload backoff")
                            speaker = self.speaker_filter.identify(full_audio, self._target_rate)
                            log.info(f"Transcribed: '{text.strip()[:80]}' (speaker={speaker})")
                            return {"text": text.strip(), "is_partial": False, "speaker": speaker, "silence_after": True}
                        else:
                            self._consecutive_empty += 1
                            log.info(f"Transcription returned empty for {len(full_audio)} samples (streak: {self._consecutive_empty})")

        return None
