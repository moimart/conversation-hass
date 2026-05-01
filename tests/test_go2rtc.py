"""Tests for the go2rtc client."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.app.go2rtc import Go2RTCClient


def _resp(json_data=None, status=200):
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=json_data or {})
    if status >= 400:
        r.raise_for_status = MagicMock(side_effect=Exception(f"HTTP {status}"))
    else:
        r.raise_for_status = MagicMock()
    return r


def _client_ctx(methods: dict) -> MagicMock:
    """Return a context-manager-shaped mock for httpx.AsyncClient.

    methods: e.g. {"put": AsyncMock(return_value=_resp())}.
    """
    inst = MagicMock()
    for name, mock in methods.items():
        setattr(inst, name, mock)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=inst)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@pytest.mark.asyncio
async def test_register_stream_puts_with_name_and_src():
    put = AsyncMock(return_value=_resp())
    with patch("server.app.go2rtc.httpx.AsyncClient", return_value=_client_ctx({"put": put})):
        client = Go2RTCClient("http://go2rtc.test:1984")
        ok = await client.register_stream("hal_abc", "rtsp://u:p@cam/x")
    assert ok is True
    put.assert_awaited_once()
    args, kwargs = put.call_args
    assert args[0] == "http://go2rtc.test:1984/api/streams"
    assert kwargs["params"] == {"name": "hal_abc", "src": "rtsp://u:p@cam/x"}


@pytest.mark.asyncio
async def test_register_stream_returns_false_on_http_error():
    put = AsyncMock(return_value=_resp(status=500))
    with patch("server.app.go2rtc.httpx.AsyncClient", return_value=_client_ctx({"put": put})):
        client = Go2RTCClient("http://go2rtc.test:1984")
        ok = await client.register_stream("hal_abc", "rtsp://x")
    assert ok is False


@pytest.mark.asyncio
async def test_delete_stream_uses_src_param():
    delete = AsyncMock(return_value=_resp())
    with patch("server.app.go2rtc.httpx.AsyncClient", return_value=_client_ctx({"delete": delete})):
        client = Go2RTCClient("http://go2rtc.test:1984")
        await client.delete_stream("hal_abc")
    delete.assert_awaited_once()
    args, kwargs = delete.call_args
    assert args[0] == "http://go2rtc.test:1984/api/streams"
    assert kwargs["params"] == {"src": "hal_abc"}


@pytest.mark.asyncio
async def test_webrtc_offer_returns_answer_sdp():
    answer = {"type": "answer", "sdp": "v=0...answer..."}
    post = AsyncMock(return_value=_resp(json_data=answer))
    with patch("server.app.go2rtc.httpx.AsyncClient", return_value=_client_ctx({"post": post})):
        client = Go2RTCClient("http://go2rtc.test:1984")
        result = await client.webrtc_offer("hal_abc", "v=0...offer...")
    assert result == "v=0...answer..."
    post.assert_awaited_once()
    args, kwargs = post.call_args
    assert kwargs["params"] == {"src": "hal_abc"}
    assert kwargs["json"] == {"type": "offer", "sdp": "v=0...offer..."}


@pytest.mark.asyncio
async def test_webrtc_offer_returns_none_on_unexpected_response():
    post = AsyncMock(return_value=_resp(json_data={"type": "error", "message": "no such stream"}))
    with patch("server.app.go2rtc.httpx.AsyncClient", return_value=_client_ctx({"post": post})):
        client = Go2RTCClient("http://go2rtc.test:1984")
        result = await client.webrtc_offer("hal_abc", "v=0...")
    assert result is None


@pytest.mark.asyncio
async def test_webrtc_offer_returns_none_on_http_failure():
    post = AsyncMock(side_effect=Exception("connection refused"))
    with patch("server.app.go2rtc.httpx.AsyncClient", return_value=_client_ctx({"post": post})):
        client = Go2RTCClient("http://go2rtc.test:1984")
        result = await client.webrtc_offer("hal_abc", "v=0...")
    assert result is None
