"""Minimal Home Assistant WebSocket API client.

Used for things the MCP server can't do — currently only the WebRTC
offer subscription used by stream_camera. Connects to /api/websocket,
authenticates with a long-lived access token, dispatches commands and
subscriptions by message id.

Ref: https://developers.home-assistant.io/docs/api/websocket/
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

import websockets

log = logging.getLogger("hal.ha_ws")

EventHandler = Callable[[dict], Awaitable[None]]


class HAWSClient:
    """Connects, authenticates, dispatches commands and subscriptions by id."""

    def __init__(self, url: str, token: str):
        self.url = self._ws_url(url)
        self._token = token
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._subscriptions: dict[int, EventHandler] = {}
        self._reader_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._connected = asyncio.Event()

    @staticmethod
    def _ws_url(url: str) -> str:
        url = url.rstrip("/")
        if url.startswith("https://"):
            url = "wss://" + url[len("https://"):]
        elif url.startswith("http://"):
            url = "ws://" + url[len("http://"):]
        if not url.endswith("/api/websocket"):
            url = url + "/api/websocket"
        return url

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def connect(self) -> None:
        if self.connected:
            return
        log.info(f"Connecting to HA WS: {self.url}")
        self._ws = await websockets.connect(self.url, max_size=4 * 1024 * 1024)
        # auth handshake
        hello = json.loads(await self._ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected handshake from HA: {hello}")
        await self._ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        result = json.loads(await self._ws.recv())
        if result.get("type") != "auth_ok":
            raise RuntimeError(f"HA auth failed: {result}")
        self._reader_task = asyncio.create_task(self._reader())
        self._connected.set()
        log.info("HA WS connected and authenticated")

    async def disconnect(self) -> None:
        self._connected.clear()
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        # Fail any in-flight callers
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("HA WS disconnected"))
        self._pending.clear()
        self._subscriptions.clear()

    async def _send(self, payload: dict) -> int:
        """Assign an id, send, return the id."""
        if not self._ws:
            raise ConnectionError("HA WS not connected")
        async with self._send_lock:
            msg_id = self._next_id
            self._next_id += 1
            payload = {**payload, "id": msg_id}
            await self._ws.send(json.dumps(payload))
            return msg_id

    async def send_command(self, payload: dict, timeout: float = 10.0) -> dict:
        """One-shot command. Returns the result message dict."""
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        msg_id = await self._send(payload)
        self._pending[msg_id] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(msg_id, None)

    async def subscribe(self, payload: dict, on_event: EventHandler) -> int:
        """Send a subscription command. Events are delivered to on_event.

        Returns the subscription id (use it with unsubscribe).
        """
        msg_id = await self._send(payload)
        self._subscriptions[msg_id] = on_event
        return msg_id

    async def unsubscribe(self, sub_id: int) -> None:
        self._subscriptions.pop(sub_id, None)
        try:
            await self.send_command({"type": "unsubscribe_events", "subscription": sub_id}, timeout=2.0)
        except Exception:
            pass

    async def _reader(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    log.warning(f"HA WS: dropping non-JSON message: {raw!r}")
                    continue
                msg_id = msg.get("id")
                msg_type = msg.get("type")
                if msg_id in self._subscriptions:
                    if msg_type == "event":
                        await self._dispatch_subscription(msg_id, msg.get("event") or {})
                    elif msg_type == "result":
                        # Subscription created — first response. Forward as event too
                        # so callers can see {success, error, ...}.
                        await self._dispatch_subscription(msg_id, msg)
                    else:
                        await self._dispatch_subscription(msg_id, msg)
                elif msg_id in self._pending:
                    fut = self._pending.get(msg_id)
                    if fut and not fut.done():
                        fut.set_result(msg)
                else:
                    log.debug(f"HA WS: untracked message: {msg}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"HA WS reader stopped: {e}")
        finally:
            self._connected.clear()

    async def _dispatch_subscription(self, sub_id: int, event: dict) -> None:
        handler = self._subscriptions.get(sub_id)
        if not handler:
            return
        try:
            await handler(event)
        except Exception as e:
            log.error(f"HA WS subscription handler {sub_id} raised: {e}")
