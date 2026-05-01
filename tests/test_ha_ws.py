"""Tests for the HA WebSocket client."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.app.ha_ws import HAWSClient


# --- URL normalization -------------------------------------------------------

class TestWsUrl:
    def test_http_to_ws(self):
        assert HAWSClient._ws_url("http://ha.local:8123") == "ws://ha.local:8123/api/websocket"

    def test_https_to_wss(self):
        assert HAWSClient._ws_url("https://ha.example.com") == "wss://ha.example.com/api/websocket"

    def test_trailing_slash_stripped(self):
        assert HAWSClient._ws_url("http://h:8123/") == "ws://h:8123/api/websocket"

    def test_already_ws_prefixed(self):
        # If a caller passes a ws:// URL we leave it alone (just append path).
        assert HAWSClient._ws_url("ws://ha:8123") == "ws://ha:8123/api/websocket"

    def test_already_full_path(self):
        # Don't double-append the websocket path.
        assert HAWSClient._ws_url("http://ha:8123/api/websocket") == "ws://ha:8123/api/websocket"


# --- Connection / handshake / dispatch --------------------------------------

class _FakeWS:
    """A minimal aioiterator-friendly stand-in for the websockets client."""

    def __init__(self, scripted: list[dict]):
        # scripted: messages HA "sends" to us, in order.
        self._scripted = list(scripted)
        self._sent: list[dict] = []
        self._recv_q: asyncio.Queue = asyncio.Queue()
        for m in self._scripted:
            self._recv_q.put_nowait(json.dumps(m))
        self._closed = False
        # Pulled by the reader after auth.
        self._iter_messages: list[str] = []

    async def recv(self):
        if not self._recv_q.empty():
            return await self._recv_q.get()
        # block until a test puts something or close
        return await self._recv_q.get()

    async def send(self, data):
        self._sent.append(json.loads(data))

    async def close(self):
        self._closed = True
        await self._recv_q.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        # Reader loops over messages; we feed from queue, sentinel None ends.
        item = await self._recv_q.get()
        if item is None:
            raise StopAsyncIteration
        return item

    def push_event(self, msg: dict):
        self._recv_q.put_nowait(json.dumps(msg))

    def close_now(self):
        self._recv_q.put_nowait(None)


@pytest.mark.asyncio
async def test_connect_authenticates_and_starts_reader():
    fake = _FakeWS([
        {"type": "auth_required"},
        {"type": "auth_ok"},
    ])
    with patch("server.app.ha_ws.websockets.connect", AsyncMock(return_value=fake)):
        client = HAWSClient("http://ha.local:8123", "TOKEN")
        await client.connect()
        assert client.connected
        # Sent auth payload
        assert fake._sent[0] == {"type": "auth", "access_token": "TOKEN"}
        await client.disconnect()


@pytest.mark.asyncio
async def test_send_command_resolves_on_matching_id():
    fake = _FakeWS([
        {"type": "auth_required"},
        {"type": "auth_ok"},
    ])
    with patch("server.app.ha_ws.websockets.connect", AsyncMock(return_value=fake)):
        client = HAWSClient("http://ha", "T")
        await client.connect()

        async def reply():
            # Wait for the client to send a command, then reply with matching id.
            await asyncio.sleep(0.01)
            sent = next((m for m in fake._sent if m.get("type") == "ping"), None)
            assert sent is not None
            fake.push_event({"id": sent["id"], "type": "result", "success": True, "result": {}})

        asyncio.create_task(reply())
        result = await client.send_command({"type": "ping"}, timeout=2.0)
        assert result["type"] == "result"
        assert result["success"] is True
        await client.disconnect()


@pytest.mark.asyncio
async def test_subscribe_dispatches_events_to_handler():
    fake = _FakeWS([
        {"type": "auth_required"},
        {"type": "auth_ok"},
    ])
    with patch("server.app.ha_ws.websockets.connect", AsyncMock(return_value=fake)):
        client = HAWSClient("http://ha", "T")
        await client.connect()
        received = []

        async def handler(ev):
            received.append(ev)

        sub_id = await client.subscribe({"type": "camera/webrtc/offer"}, handler)
        # Push a result then two events with the matching id.
        fake.push_event({"id": sub_id, "type": "result", "success": True})
        fake.push_event({"id": sub_id, "type": "event", "event": {"type": "answer", "answer": "v=0"}})
        fake.push_event({"id": sub_id, "type": "event", "event": {"type": "candidate", "candidate": {}}})
        await asyncio.sleep(0.05)
        assert any(ev.get("type") == "answer" for ev in received)
        assert any(ev.get("type") == "candidate" for ev in received)
        await client.disconnect()


@pytest.mark.asyncio
async def test_disconnect_fails_pending_futures():
    fake = _FakeWS([
        {"type": "auth_required"},
        {"type": "auth_ok"},
    ])
    with patch("server.app.ha_ws.websockets.connect", AsyncMock(return_value=fake)):
        client = HAWSClient("http://ha", "T")
        await client.connect()
        # Issue a command but never reply — disconnect should fail it.
        task = asyncio.create_task(client.send_command({"type": "ping"}, timeout=5.0))
        await asyncio.sleep(0.01)
        await client.disconnect()
        with pytest.raises(ConnectionError):
            await task


@pytest.mark.asyncio
async def test_auth_failure_raises():
    fake = _FakeWS([
        {"type": "auth_required"},
        {"type": "auth_invalid", "message": "bad token"},
    ])
    with patch("server.app.ha_ws.websockets.connect", AsyncMock(return_value=fake)):
        client = HAWSClient("http://ha", "T")
        with pytest.raises(RuntimeError, match="HA auth failed"):
            await client.connect()
