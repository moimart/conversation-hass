"""Tests for the CDP screenshot client."""

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rpi.audio_streamer.cdp_snapshot import capture_screenshot


def _make_session(targets=None, raise_on_get=False):
    """Build a fake aiohttp.ClientSession context manager."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=targets or [])

    @asynccontextmanager
    async def get_ctx(url):
        if raise_on_get:
            raise RuntimeError("connection refused")
        yield resp

    session = MagicMock()
    session.get = MagicMock(side_effect=get_ctx)

    @asynccontextmanager
    async def session_ctx():
        yield session

    return session_ctx()


class _FakeWS:
    """Minimal stand-in for a websockets ClientConnection."""
    def __init__(self, response):
        self._response = response
        self.sent = []

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def recv(self):
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


@pytest.mark.asyncio
async def test_target_list_failure_returns_none():
    with patch(
        "rpi.audio_streamer.cdp_snapshot.aiohttp.ClientSession",
        return_value=_make_session(raise_on_get=True),
    ):
        result = await capture_screenshot("http://nope:9222")
    assert result is None


@pytest.mark.asyncio
async def test_no_page_target_returns_none():
    with patch(
        "rpi.audio_streamer.cdp_snapshot.aiohttp.ClientSession",
        return_value=_make_session(targets=[{"type": "iframe"}]),
    ):
        result = await capture_screenshot("http://x:9222")
    assert result is None


@pytest.mark.asyncio
async def test_successful_screenshot_returns_jpeg_bytes():
    raw_jpeg = b"\xff\xd8\xff\xe0fake"
    targets = [{"type": "page", "webSocketDebuggerUrl": "ws://x:9222/devtools/page/abc"}]
    response = json.dumps({"id": 1, "result": {"data": base64.b64encode(raw_jpeg).decode("ascii")}})
    fake_ws = _FakeWS(response)
    with patch(
        "rpi.audio_streamer.cdp_snapshot.aiohttp.ClientSession",
        return_value=_make_session(targets=targets),
    ), patch(
        "rpi.audio_streamer.cdp_snapshot.websockets.connect",
        return_value=fake_ws,
    ):
        result = await capture_screenshot("http://x:9222", quality=90)

    assert result == raw_jpeg
    sent = fake_ws.sent[0]
    assert sent["method"] == "Page.captureScreenshot"
    assert sent["params"]["format"] == "jpeg"
    assert sent["params"]["quality"] == 90


@pytest.mark.asyncio
async def test_cdp_error_response_returns_none():
    targets = [{"type": "page", "webSocketDebuggerUrl": "ws://x:9222/devtools/page/abc"}]
    response = json.dumps({"id": 1, "error": {"code": -32000, "message": "fail"}})
    fake_ws = _FakeWS(response)
    with patch(
        "rpi.audio_streamer.cdp_snapshot.aiohttp.ClientSession",
        return_value=_make_session(targets=targets),
    ), patch(
        "rpi.audio_streamer.cdp_snapshot.websockets.connect",
        return_value=fake_ws,
    ):
        result = await capture_screenshot("http://x:9222")
    assert result is None


@pytest.mark.asyncio
async def test_skips_async_events_until_matching_id():
    raw_jpeg = b"\xff\xd8data"
    targets = [{"type": "page", "webSocketDebuggerUrl": "ws://x/devtools/page/y"}]
    # Queue: an async page event first, then the screenshot result.
    queued = [
        json.dumps({"method": "Page.frameNavigated", "params": {}}),
        json.dumps({"id": 1, "result": {"data": base64.b64encode(raw_jpeg).decode("ascii")}}),
    ]
    iter_responses = iter(queued)

    class _MultiFakeWS(_FakeWS):
        async def recv(self):
            return next(iter_responses)

    fake_ws = _MultiFakeWS(None)
    with patch(
        "rpi.audio_streamer.cdp_snapshot.aiohttp.ClientSession",
        return_value=_make_session(targets=targets),
    ), patch(
        "rpi.audio_streamer.cdp_snapshot.websockets.connect",
        return_value=fake_ws,
    ):
        result = await capture_screenshot("http://x:9222")
    assert result == raw_jpeg
