"""Tests for the stt-service ASR engines (stt-service/engines.py).

engines.py lives in the dash-named `stt-service/` dir (not importable as a
package), so we load it by adding that dir to sys.path. The heavy ASR libs
(faster_whisper / nemo / torch) are imported lazily inside the engine methods,
so the module imports cleanly here and the tests mock those libs via
sys.modules.
"""

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

REPO = Path(__file__).resolve().parent.parent
STT_DIR = REPO / "stt-service"
if str(STT_DIR) not in sys.path:
    sys.path.insert(0, str(STT_DIR))

import engines  # noqa: E402
from engines import WhisperTranscriber, create_transcriber  # noqa: E402


class TestTranscriberInit:
    def test_default_model_size(self):
        t = WhisperTranscriber()
        assert t.model_size == "large-v3-turbo"
        assert t.model is None

    def test_custom_model_size(self):
        t = WhisperTranscriber(model_size="large-v3")
        assert t.model_size == "large-v3"


class TestTranscribe:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_model(self):
        t = WhisperTranscriber()
        audio = np.random.randn(16000).astype(np.float32)
        assert await t.transcribe(audio) == ""

    @pytest.mark.asyncio
    async def test_returns_empty_for_short_audio(self):
        t = WhisperTranscriber()
        t.model = MagicMock()
        short_audio = np.random.randn(100).astype(np.float32)
        assert await t.transcribe(short_audio) == ""
        t.model.transcribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_transcribes_audio(self):
        t = WhisperTranscriber()
        seg1 = MagicMock(text="hello", no_speech_prob=0.1)
        seg2 = MagicMock(text="world", no_speech_prob=0.1)
        mock_model = MagicMock()
        mock_model.transcribe.return_value = (iter([seg1, seg2]), MagicMock())
        t.model = mock_model

        audio = np.random.randn(16000).astype(np.float32)
        result = await t.transcribe(audio)

        assert result == "hello world"
        mock_model.transcribe.assert_called_once()
        kwargs = mock_model.transcribe.call_args.kwargs
        assert kwargs["beam_size"] == 1
        assert kwargs["language"] == "en"
        assert kwargs["vad_filter"] is False

    @pytest.mark.asyncio
    async def test_handles_empty_segments(self):
        t = WhisperTranscriber()
        mock_model = MagicMock()
        mock_model.transcribe.return_value = (iter([]), MagicMock())
        t.model = mock_model
        audio = np.random.randn(16000).astype(np.float32)
        assert await t.transcribe(audio) == ""

    @pytest.mark.asyncio
    async def test_strips_whitespace(self):
        t = WhisperTranscriber()
        seg = MagicMock(text="  hello  ", no_speech_prob=0.1)
        mock_model = MagicMock()
        mock_model.transcribe.return_value = (iter([seg]), MagicMock())
        t.model = mock_model
        audio = np.random.randn(16000).astype(np.float32)
        assert await t.transcribe(audio) == "hello"


class TestInitialize:
    @pytest.mark.asyncio
    async def test_initialize_tries_cuda_first(self):
        t = WhisperTranscriber(model_size="tiny")
        mock_model = MagicMock()
        mock_whisper = MagicMock(WhisperModel=MagicMock(return_value=mock_model))
        with patch.dict("sys.modules", {"faster_whisper": mock_whisper}):
            await t.initialize()
        assert t.model == mock_model

    @pytest.mark.asyncio
    async def test_initialize_falls_back_to_cpu(self):
        t = WhisperTranscriber(model_size="tiny")
        mock_model = MagicMock()
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs.get("device") == "cuda":
                raise RuntimeError("CUDA not available")
            return mock_model

        mock_whisper = MagicMock(WhisperModel=MagicMock(side_effect=side_effect))
        with patch.dict("sys.modules", {"faster_whisper": mock_whisper}):
            await t.initialize()
        assert t.model == mock_model
        assert call_count == 2


class TestFactory:
    def test_default_is_whisper(self):
        t = create_transcriber("whisper")
        assert isinstance(t, WhisperTranscriber)

    def test_unknown_engine_falls_back_to_whisper(self):
        # The service only knows whisper/nemotron; anything else → whisper.
        t = create_transcriber("bogus")
        assert isinstance(t, WhisperTranscriber)

    def test_nemotron_engine(self):
        t = create_transcriber("nemotron")
        assert type(t).__name__ == "NemotronTranscriber"
