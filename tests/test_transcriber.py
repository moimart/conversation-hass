"""Tests for the server-side STT client (server/app/transcriber.py).

The AI server only carries RemoteTranscriber (a thin httpx client) — the heavy
ASR engines live in stt-service/engines.py. These tests assert the remote path:
the wire format, the short-audio shortcut, the create_transcriber factory always
returning the remote engine, and the never-raise-into-the-pipeline discipline.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import numpy as np
import pytest

from server.app.transcriber import (
    BaseTranscriber,
    RemoteTranscriber,
    create_transcriber,
)


class TestFactory:
    def test_remote_engine_returns_remote(self):
        assert isinstance(create_transcriber("remote"), RemoteTranscriber)

    def test_unknown_engine_falls_back_to_remote(self):
        # whisper/nemotron aren't available on the server — fall back to remote.
        assert isinstance(create_transcriber("whisper"), RemoteTranscriber)
        assert isinstance(create_transcriber("nemotron"), RemoteTranscriber)

    def test_url_from_env(self, monkeypatch):
        monkeypatch.setenv("STT_REMOTE_URL", "http://stt-host:9999")
        t = create_transcriber("remote")
        assert t.url == "http://stt-host:9999"

    def test_is_base_transcriber(self):
        assert isinstance(create_transcriber("remote"), BaseTranscriber)


class TestTranscribe:
    @pytest.mark.asyncio
    async def test_short_audio_skips_round_trip(self):
        t = RemoteTranscriber("http://x")
        t._client = MagicMock()
        t._client.post = AsyncMock()
        result = await t.transcribe(np.zeros(100, dtype=np.float32))
        assert result == ""
        t._client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_posts_float32_and_returns_text(self):
        t = RemoteTranscriber("http://stt:8770/")
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"text": "  hello world  "})
        t._client = MagicMock()
        t._client.post = AsyncMock(return_value=resp)

        audio = np.random.randn(16000).astype(np.float32)
        result = await t.transcribe(audio)

        assert result == "hello world"
        args, kwargs = t._client.post.call_args
        assert args[0] == "http://stt:8770/transcribe"
        # Body is the raw float32 bytes, byte-identical to the buffer.
        assert kwargs["content"] == audio.astype(np.float32).tobytes()
        assert kwargs["headers"]["X-Sample-Rate"] == "16000"

    @pytest.mark.asyncio
    async def test_retries_once_on_connect_error_then_returns_empty(self):
        t = RemoteTranscriber("http://x")
        t._client = MagicMock()
        t._client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        result = await t.transcribe(np.random.randn(16000).astype(np.float32))
        assert result == ""
        assert t._client.post.await_count == 2  # original + one retry

    @pytest.mark.asyncio
    async def test_never_raises_on_unexpected_error(self):
        t = RemoteTranscriber("http://x")
        t._client = MagicMock()
        t._client.post = AsyncMock(side_effect=RuntimeError("boom"))
        # Must degrade to "" rather than propagating into the pipeline.
        result = await t.transcribe(np.random.randn(16000).astype(np.float32))
        assert result == ""


class TestWarmUp:
    @pytest.mark.asyncio
    async def test_warm_up_returns_when_healthy(self):
        t = RemoteTranscriber("http://x")
        resp = MagicMock(status_code=200)
        t._client = MagicMock()
        t._client.get = AsyncMock(return_value=resp)
        await t.warm_up()  # should return promptly without raising
        t._client.get.assert_awaited()

    @pytest.mark.asyncio
    async def test_reload_model_is_noop(self):
        # The remote service owns its model lifecycle; this must not raise.
        t = RemoteTranscriber("http://x")
        assert t._reload_model() is None
