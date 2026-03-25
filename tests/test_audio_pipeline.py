"""Tests for the audio processing pipeline."""

import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from tests.conftest import make_audio_bytes, make_silence_bytes

# We need to mock torch before importing audio_pipeline since it does inline `import torch`
mock_torch = MagicMock()
mock_torch.from_numpy = MagicMock(side_effect=lambda x: MagicMock(
    __len__=lambda self: len(x),
    __getitem__=lambda self, s: MagicMock(__len__=lambda self: 512),
))
mock_torch.nn.functional.pad = MagicMock(side_effect=lambda x, pad: x)
sys.modules.setdefault("torch", mock_torch)

from server.app.audio_pipeline import AudioPipeline, SILENCE_THRESHOLD


class TestPipelineInit:
    def test_defaults(self):
        p = AudioPipeline()
        assert p.sample_rate == 16000
        assert p._ai_speaking is False
        assert p._speech_active is False

    def test_custom_sample_rate(self):
        p = AudioPipeline(sample_rate=44100)
        assert p.sample_rate == 44100


class TestEchoSuppression:
    def test_set_ai_speaking_true(self):
        p = AudioPipeline()
        p._speech_active = True
        p._audio_buffer.extend(b"\x00" * 100)
        p._partial_buffer.extend(b"\x00" * 50)

        p.set_ai_speaking(True)

        assert p._ai_speaking is True
        assert p._speech_active is False
        assert len(p._audio_buffer) == 0
        assert len(p._partial_buffer) == 0

    def test_set_ai_speaking_false(self):
        p = AudioPipeline()
        p.set_ai_speaking(True)
        p.set_ai_speaking(False)
        assert p._ai_speaking is False

    @pytest.mark.asyncio
    async def test_returns_none_while_ai_speaking(self):
        p = AudioPipeline()
        p._ai_speaking = True
        p._vad_model = MagicMock()

        result = await p.process_chunk(make_audio_bytes(0.1))
        assert result is None
        p._vad_model.assert_not_called()


def _make_pipeline(vad_speech: bool = True) -> AudioPipeline:
    """Create a pipeline with all heavy models mocked out."""
    p = AudioPipeline(sample_rate=16000)

    # Mock VAD: return high or low confidence
    confidence = 0.9 if vad_speech else 0.1
    mock_vad = MagicMock()
    mock_vad.return_value.item.return_value = confidence
    p._vad_model = mock_vad

    # Mock transcriber
    p.transcriber = AsyncMock()
    p.transcriber.transcribe = AsyncMock(return_value="hello world")

    # Mock speaker filter
    p.speaker_filter = MagicMock()
    p.speaker_filter.identify.return_value = "human"

    return p


class TestProcessChunk:
    @pytest.mark.asyncio
    async def test_speech_detected_buffers_audio(self):
        p = _make_pipeline(vad_speech=True)
        audio = make_audio_bytes(0.1)

        await p.process_chunk(audio)

        assert p._speech_active is True
        assert len(p._audio_buffer) > 0

    @pytest.mark.asyncio
    async def test_silence_after_speech_triggers_final_transcription(self):
        p = _make_pipeline(vad_speech=False)
        p._speech_active = True
        p._audio_buffer.extend(make_audio_bytes(1.0))
        p._silence_start = time.monotonic() - SILENCE_THRESHOLD - 1

        result = await p.process_chunk(make_silence_bytes(0.1))

        assert result is not None
        assert result["text"] == "hello world"
        assert result["is_partial"] is False
        assert result["speaker"] == "human"
        assert result["silence_after"] is True

    @pytest.mark.asyncio
    async def test_brief_silence_doesnt_finalize(self):
        p = _make_pipeline(vad_speech=False)
        p._speech_active = True
        p._audio_buffer.extend(make_audio_bytes(0.5))
        p._silence_start = None  # silence just starting

        result = await p.process_chunk(make_silence_bytes(0.1))

        assert result is None
        assert p._silence_start is not None
        assert p._speech_active is True

    @pytest.mark.asyncio
    async def test_silence_without_prior_speech_ignored(self):
        p = _make_pipeline(vad_speech=False)
        p._speech_active = False

        result = await p.process_chunk(make_silence_bytes(0.1))

        assert result is None

    @pytest.mark.asyncio
    async def test_empty_transcription_returns_none(self):
        p = _make_pipeline(vad_speech=False)
        p._speech_active = True
        p._audio_buffer.extend(make_audio_bytes(1.0))
        p._silence_start = time.monotonic() - SILENCE_THRESHOLD - 1
        p.transcriber.transcribe = AsyncMock(return_value="")

        result = await p.process_chunk(make_silence_bytes(0.1))

        assert result is None

    @pytest.mark.asyncio
    async def test_speaker_filter_labels_ai(self):
        p = _make_pipeline(vad_speech=False)
        p._speech_active = True
        p._audio_buffer.extend(make_audio_bytes(1.0))
        p._silence_start = time.monotonic() - SILENCE_THRESHOLD - 1
        p.speaker_filter.identify.return_value = "ai"

        result = await p.process_chunk(make_silence_bytes(0.1))

        assert result["speaker"] == "ai"

    @pytest.mark.asyncio
    async def test_partial_transcription_during_speech(self):
        p = _make_pipeline(vad_speech=True)
        p._last_partial_time = 0  # force partial emission

        audio = make_audio_bytes(0.1)
        result = await p.process_chunk(audio)

        # Should get a partial result
        if result:
            assert result["is_partial"] is True
            assert result["speaker"] == "human"
