"""Tests for the streaming transcriber."""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from server.app.transcriber import StreamingTranscriber


class TestTranscriberInit:
    def test_default_model_size(self):
        t = StreamingTranscriber()
        assert t.model_size == "base"
        assert t.model is None

    def test_custom_model_size(self):
        t = StreamingTranscriber(model_size="large-v3")
        assert t.model_size == "large-v3"


class TestTranscribe:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_model(self):
        t = StreamingTranscriber()
        audio = np.random.randn(16000).astype(np.float32)
        result = await t.transcribe(audio)
        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_for_short_audio(self):
        t = StreamingTranscriber()
        t.model = MagicMock()
        short_audio = np.random.randn(100).astype(np.float32)
        result = await t.transcribe(short_audio)
        assert result == ""
        t.model.transcribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_transcribes_audio(self):
        t = StreamingTranscriber()

        seg1 = MagicMock()
        seg1.text = "hello"
        seg2 = MagicMock()
        seg2.text = "world"

        mock_model = MagicMock()
        mock_model.transcribe.return_value = (iter([seg1, seg2]), MagicMock())
        t.model = mock_model

        audio = np.random.randn(16000).astype(np.float32)
        result = await t.transcribe(audio)

        assert result == "hello world"
        mock_model.transcribe.assert_called_once()
        call_kwargs = mock_model.transcribe.call_args
        assert call_kwargs.kwargs["beam_size"] == 5
        assert call_kwargs.kwargs["language"] == "en"
        assert call_kwargs.kwargs["vad_filter"] is True

    @pytest.mark.asyncio
    async def test_handles_empty_segments(self):
        t = StreamingTranscriber()
        mock_model = MagicMock()
        mock_model.transcribe.return_value = (iter([]), MagicMock())
        t.model = mock_model

        audio = np.random.randn(16000).astype(np.float32)
        result = await t.transcribe(audio)
        assert result == ""

    @pytest.mark.asyncio
    async def test_strips_whitespace(self):
        t = StreamingTranscriber()
        seg = MagicMock()
        seg.text = "  hello  "
        mock_model = MagicMock()
        mock_model.transcribe.return_value = (iter([seg]), MagicMock())
        t.model = mock_model

        audio = np.random.randn(16000).astype(np.float32)
        result = await t.transcribe(audio)
        assert result == "hello"


class TestInitialize:
    @pytest.mark.asyncio
    async def test_initialize_tries_cuda_first(self):
        t = StreamingTranscriber(model_size="tiny")
        mock_model = MagicMock()

        # WhisperModel is imported lazily inside initialize(), so use create=True
        with patch("server.app.transcriber.WhisperModel", return_value=mock_model, create=True) as MockWhisper:
            # Patch the import inside the method
            import server.app.transcriber as mod
            with patch.dict("sys.modules", {"faster_whisper": MagicMock(WhisperModel=MockWhisper)}):
                await t.initialize()

            assert t.model == mock_model

    @pytest.mark.asyncio
    async def test_initialize_falls_back_to_cpu(self):
        t = StreamingTranscriber(model_size="tiny")
        mock_model = MagicMock()

        call_count = 0
        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs.get("device") == "cuda":
                raise RuntimeError("CUDA not available")
            return mock_model

        mock_whisper_cls = MagicMock(side_effect=side_effect)
        mock_faster_whisper = MagicMock(WhisperModel=mock_whisper_cls)

        with patch.dict("sys.modules", {"faster_whisper": mock_faster_whisper}):
            await t.initialize()
            assert t.model == mock_model
            assert call_count == 2
