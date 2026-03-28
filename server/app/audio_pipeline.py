"""Audio processing pipeline: VAD, transcription, speaker filtering."""

import asyncio
import logging
import time

import numpy as np

from .transcriber import BaseTranscriber, create_transcriber
from .speaker_filter import SpeakerFilter

log = logging.getLogger("hal.pipeline")

# Silence threshold: seconds of silence after speech to trigger command processing
SILENCE_THRESHOLD = 1.5


class AudioPipeline:
    """Processes raw audio: VAD → transcription → speaker filtering."""

    def __init__(self, sample_rate: int = 16000, stt_engine: str = "whisper", stt_model: str = ""):
        self.sample_rate = sample_rate  # incoming audio rate (may be 48kHz)
        self._target_rate = 16000       # rate expected by VAD and STT
        self._stt_engine = stt_engine
        self._stt_model = stt_model
        self.transcriber: BaseTranscriber | None = None
        self.speaker_filter: SpeakerFilter | None = None

        # VAD state
        self._vad_model = None
        self._speech_active = False
        self._silence_start: float | None = None
        self._last_speech_time: float = 0.0
        self._vad_reset_interval = 300.0  # reset VAD state every 5 min of silence
        self._last_vad_reset: float = 0.0
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
            silence_duration = now - self._last_speech_time if self._last_speech_time > 0 else 0
            log.info(f"Pipeline health: chunks={self._chunk_count}, silence={silence_duration:.0f}s, speech_active={self._speech_active}")
            self._last_health_log = now

        # Reload VAD model after extended silence — reset_states() alone
        # isn't enough, the ONNX runtime accumulates corruption over thousands of calls
        if self._last_speech_time > 0 and (now - self._last_speech_time) > self._vad_reset_interval:
            if now - self._last_vad_reset > self._vad_reset_interval:
                log.info("Reloading VAD model after prolonged silence")
                try:
                    from silero_vad import load_silero_vad
                    self._vad_model = load_silero_vad(onnx=True)
                    log.info("VAD model reloaded successfully")
                except Exception as e:
                    log.error(f"Failed to reload VAD model: {e}")
                    self._vad_model.reset_states()
                self._last_vad_reset = now

        # Convert bytes to float32 numpy array
        audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)
        audio_float = audio_int16.astype(np.float32) / 32768.0

        # Resample to 16kHz for VAD if incoming rate differs
        if self.sample_rate != self._target_rate:
            import librosa
            vad_audio = librosa.resample(audio_float, orig_sr=self.sample_rate, target_sr=self._target_rate)
        else:
            vad_audio = audio_float

        # Run VAD on 16kHz audio
        import torch
        chunk_tensor = torch.from_numpy(vad_audio)

        # Silero VAD expects chunks of specific sizes (512 for 16kHz)
        vad_chunk_size = 512
        is_speech = False
        for i in range(0, len(chunk_tensor), vad_chunk_size):
            segment = chunk_tensor[i : i + vad_chunk_size]
            if len(segment) < vad_chunk_size:
                segment = torch.nn.functional.pad(segment, (0, vad_chunk_size - len(segment)))
            confidence = self._vad_model(segment, self._target_rate).item()
            if confidence > 0.5:
                is_speech = True
                break

        if is_speech:
            if not self._speech_active:
                log.info("Speech started")
            self._speech_active = True
            self._silence_start = None
            self._last_speech_time = now
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

                    if len(self._audio_buffer) > 0:
                        buf_bytes = len(self._audio_buffer)
                        full_audio = self._to_16k_float(bytes(self._audio_buffer))
                        log.info(f"Finalizing: buffer={buf_bytes} bytes, audio={len(full_audio)} samples ({len(full_audio)/16000:.1f}s)")
                        text = await self.transcriber.transcribe(full_audio)

                        self._audio_buffer.clear()
                        self._partial_buffer.clear()
                        self._silence_start = None

                        if text and text.strip():
                            speaker = self.speaker_filter.identify(full_audio, self._target_rate)
                            log.info(f"Transcribed: '{text.strip()[:80]}' (speaker={speaker})")
                            return {"text": text.strip(), "is_partial": False, "speaker": speaker, "silence_after": True}
                        else:
                            log.info(f"Transcription returned empty for {len(full_audio)} samples")

        return None
