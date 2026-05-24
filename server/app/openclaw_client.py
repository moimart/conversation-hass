"""OpenClaw Gateway WebSocket client.

Connects to an OpenClaw Gateway instance and routes user text to the
agent, returning structured responses with text + optional rich media.
Modeled after ha_ws.py (same project).

Gateway protocol (WebSocket, JSON text frames):
  connect  → {type:"connect", ...}        → {type:"connected", ...}
  request  → {type:"req", id, method, params}
  response ← {type:"res", id, ok, payload|error}
  events   ← {type:"event", event, payload, seq?}

Reference: https://docs.openclaw.ai/concepts/architecture
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import websockets

log = logging.getLogger("hal.openclaw")

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg")
_VIDEO_EXTS = (".mp4", ".webm", ".m3u8", ".mov", ".avi")
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((https?://[^\s)]+)\)")
_MD_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\((https?://[^\s)]+)\)")
_BARE_URL_RE = re.compile(r"(?<!\()(https?://[^\s)>\]]+)")
_MD_STRIP_RE = re.compile(r"[*_~`#>\[\]!]")


@dataclass
class OpenClawResponse:
    """Parsed agent response from the OpenClaw Gateway."""

    text: str = ""
    images: list[dict] = field(default_factory=list)
    videos: list[dict] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    agent_time_s: float = 0.0


class OpenClawClient:
    """WebSocket client for an OpenClaw Gateway."""

    def __init__(
        self,
        gateway_url: str,
        channel_id: str = "",
        agent_id: str = "",
    ):
        self.gateway_url = gateway_url.rstrip("/")
        self.channel_id = channel_id
        self.agent_id = agent_id
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._next_id = 1
        self._pending: dict[str, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._connected = asyncio.Event()
        self._session_id: str = ""

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the WebSocket and run the Gateway handshake."""
        if self.connected:
            return
        log.info(f"Connecting to OpenClaw Gateway at {self.gateway_url}")
        self._ws = await websockets.connect(
            self.gateway_url,
            max_size=16 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        )

        # Handshake: send connect frame, wait for connected ack.
        handshake: dict[str, Any] = {"type": "connect"}
        if self.channel_id:
            handshake["channel"] = self.channel_id
        if self.agent_id:
            handshake["agent"] = self.agent_id
        await self._ws.send(json.dumps(handshake))
        raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
        hello = json.loads(raw)
        log.debug(f"OpenClaw handshake response: {hello}")
        hello_type = str(hello.get("type", "")).lower()
        if hello_type not in ("connected", "ack", "res"):
            log.warning(
                f"OpenClaw: unexpected handshake type {hello_type!r}, "
                "continuing anyway (protocol discovery)"
            )
        self._session_id = str(hello.get("session", hello.get("id", "")))

        self._reader_task = asyncio.create_task(self._reader())
        self._connected.set()
        log.info(
            f"OpenClaw Gateway connected (session={self._session_id or 'n/a'})"
        )

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
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("OpenClaw disconnected"))
        self._pending.clear()
        log.info("OpenClaw Gateway disconnected")

    # ------------------------------------------------------------------
    # Send a message to the agent
    # ------------------------------------------------------------------

    async def send_message(
        self,
        text: str,
        conversation_id: str = "",
        timeout: float = 120.0,
    ) -> OpenClawResponse:
        """Send user text to the OpenClaw agent and wait for the response.

        Timeout defaults to 120 s — agentic chains can be slow.
        """
        if not self._ws or not self.connected:
            raise ConnectionError("OpenClaw Gateway not connected")

        t0 = time.monotonic()
        msg_id = await self._send({
            "type": "req",
            "method": "agent",
            "params": {
                "message": text,
                **({"conversation_id": conversation_id} if conversation_id else {}),
            },
        })

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[msg_id] = fut
        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(msg_id, None)

        elapsed = time.monotonic() - t0
        log.info(f"OpenClaw agent responded in {elapsed:.2f}s")

        payload = result.get("payload", result) if isinstance(result, dict) else {}
        response = self._parse_response(payload)
        response.raw = result
        response.agent_time_s = elapsed
        return response

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _send(self, payload: dict) -> str:
        if not self._ws:
            raise ConnectionError("OpenClaw not connected")
        async with self._send_lock:
            msg_id = f"hal_{self._next_id}"
            self._next_id += 1
            payload = {**payload, "id": msg_id}
            raw = json.dumps(payload)
            log.debug(f"OpenClaw TX: {raw[:500]}")
            await self._ws.send(raw)
            return msg_id

    async def _reader(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    log.warning(f"OpenClaw: non-JSON frame: {raw[:200]!r}")
                    continue
                log.debug(f"OpenClaw RX: {json.dumps(msg)[:500]}")
                msg_type = str(msg.get("type", ""))
                msg_id = str(msg.get("id", ""))

                if msg_type == "res" and msg_id in self._pending:
                    fut = self._pending.get(msg_id)
                    if fut and not fut.done():
                        if msg.get("ok", True):
                            fut.set_result(msg)
                        else:
                            fut.set_exception(
                                RuntimeError(
                                    f"OpenClaw error: {msg.get('error', msg)}"
                                )
                            )
                elif msg_type == "event":
                    event_name = msg.get("event", "")
                    log.debug(
                        f"OpenClaw event: {event_name} "
                        f"seq={msg.get('seq', '')} "
                        f"payload_keys={list((msg.get('payload') or {}).keys())}"
                    )
                elif msg_type == "ack":
                    log.debug(f"OpenClaw ack for {msg_id}")
                elif msg_type in ("heartbeat", "ping"):
                    if msg_type == "ping":
                        try:
                            await self._ws.send(
                                json.dumps({"type": "pong", "id": msg_id})
                            )
                        except Exception:
                            pass
                else:
                    log.debug(f"OpenClaw unhandled frame: {msg}")
        except asyncio.CancelledError:
            raise
        except websockets.ConnectionClosed as e:
            log.warning(f"OpenClaw WS closed: {e}")
        except Exception as e:
            log.error(f"OpenClaw reader error: {e}", exc_info=True)
        finally:
            self._connected.clear()

    # ------------------------------------------------------------------
    # Response parsing (resilient to multiple possible formats)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_response(payload: dict) -> OpenClawResponse:
        """Extract text, images, videos, and links from an agent payload.

        The exact response schema is partially documented, so we handle
        several plausible shapes and log the raw payload at DEBUG for
        iterative discovery.
        """
        resp = OpenClawResponse()
        if not isinstance(payload, dict):
            resp.text = str(payload) if payload else ""
            return resp

        # Text: try common keys
        text = ""
        for key in ("message", "text", "content", "response", "reply"):
            if key in payload and isinstance(payload[key], str):
                text = payload[key]
                break

        # Structured media arrays (if the agent returns them)
        if isinstance(payload.get("images"), list):
            for item in payload["images"]:
                if isinstance(item, dict):
                    resp.images.append(item)
                elif isinstance(item, str):
                    resp.images.append({"url": item})
        if isinstance(payload.get("videos"), list):
            for item in payload["videos"]:
                if isinstance(item, dict):
                    resp.videos.append(item)
                elif isinstance(item, str):
                    resp.videos.append({"url": item})
        if isinstance(payload.get("attachments"), list):
            for att in payload["attachments"]:
                if not isinstance(att, dict):
                    continue
                mime = str(att.get("mime_type", att.get("type", ""))).lower()
                url = att.get("url", att.get("src", ""))
                if "image" in mime or any(url.lower().endswith(e) for e in _IMAGE_EXTS):
                    resp.images.append({"url": url, "mime": mime})
                elif "video" in mime or any(url.lower().endswith(e) for e in _VIDEO_EXTS):
                    resp.videos.append({"url": url})
                elif url:
                    resp.links.append(url)

        # Extract markdown images from text
        if text:
            for url in _MD_IMAGE_RE.findall(text):
                resp.images.append({"url": url})
            for url in _MD_LINK_RE.findall(text):
                low = url.lower()
                if any(low.endswith(e) for e in _IMAGE_EXTS):
                    resp.images.append({"url": url})
                elif any(low.endswith(e) for e in _VIDEO_EXTS):
                    resp.videos.append({"url": url})
                else:
                    resp.links.append(url)

        # Clean text for TTS: strip markdown images/links/formatting
        if text:
            clean = _MD_IMAGE_RE.sub("", text)
            clean = _MD_LINK_RE.sub(lambda m: m.group(0).split("]")[0].lstrip("["), clean)
            clean = _MD_STRIP_RE.sub("", clean)
            clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
            resp.text = clean
        else:
            resp.text = ""

        return resp
