"""OpenClaw Gateway WebSocket client.

Connects to an OpenClaw Gateway instance and routes user text to the
agent, returning structured responses with text + optional rich media.

Gateway protocol (WebSocket, JSON text frames):
  challenge ← {type:"event", event:"connect.challenge", payload:{nonce}}
  connect   → {type:"req", method:"connect", params:{auth, client, device, ...}}
  response  ← {type:"res", id, ok, payload}
  events    ← {type:"event", event, payload}

Device authentication uses Ed25519 key pairs with a v3 signed payload:
  v3|deviceId|clientId|clientMode|role|scopes|signedAtMs|token|nonce|platform|deviceFamily

Reference: https://docs.openclaw.ai/concepts/architecture
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import websockets

log = logging.getLogger("hal.openclaw")

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg")
_VIDEO_EXTS = (".mp4", ".webm", ".m3u8", ".mov", ".avi")
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((https?://[^\s)]+)\)")
_MD_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\((https?://[^\s)]+)\)")
_MD_STRIP_RE = re.compile(r"[*_~`#>\[\]!]")

_IDENTITY_PATH = Path(
    os.environ.get("OPENCLAW_DEVICE_IDENTITY", "/app/runtime/openclaw-device.json")
)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


# ------------------------------------------------------------------
# Device identity (Ed25519 keypair + derived deviceId)
# ------------------------------------------------------------------

def _load_or_create_identity(path: Path) -> dict:
    """Load or generate an Ed25519 device identity for Gateway pairing."""
    if path.is_file():
        try:
            return json.loads(path.read_text())
        except Exception as e:
            log.warning(f"Corrupt device identity at {path}: {e} — regenerating")

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    priv = Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )
    identity = {
        "deviceId": hashlib.sha256(pub_raw).hexdigest(),
        "publicKeyPem": priv.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode(),
        "privateKeyPem": priv.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(identity, indent=2))
    log.info(f"Generated OpenClaw device identity: {identity['deviceId']}")
    return identity


def _sign_v3_payload(
    priv_pem: str,
    device_id: str,
    client_id: str,
    client_mode: str,
    role: str,
    scopes: list[str],
    signed_at_ms: int,
    token: str,
    nonce: str,
    platform: str,
    device_family: str,
) -> str:
    """Build and sign a v3 device auth payload. Returns base64url signature."""
    from cryptography.hazmat.primitives import serialization as _ser

    payload = "|".join([
        "v3", device_id, client_id, client_mode, role,
        ",".join(scopes), str(signed_at_ms),
        token, nonce, platform, device_family,
    ])
    key = _ser.load_pem_private_key(priv_pem.encode(), password=None)
    return _b64url(key.sign(payload.encode("utf-8")))


# ------------------------------------------------------------------
# Response dataclass
# ------------------------------------------------------------------

@dataclass
class OpenClawResponse:
    """Parsed agent response from the OpenClaw Gateway."""

    text: str = ""
    images: list[dict] = field(default_factory=list)
    videos: list[dict] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    agent_time_s: float = 0.0


# ------------------------------------------------------------------
# Client
# ------------------------------------------------------------------

class OpenClawClient:
    """WebSocket client for an OpenClaw Gateway with Ed25519 device auth."""

    def __init__(
        self,
        gateway_url: str,
        password: str = "",
        agent_id: str = "main",
        identity_path: Path | None = None,
    ):
        self.gateway_url = gateway_url.rstrip("/")
        self._password = password
        self.agent_id = agent_id
        self._identity = _load_or_create_identity(identity_path or _IDENTITY_PATH)
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._next_id = 1
        self._pending: dict[str, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._connected = asyncio.Event()
        self._session_key: str = ""
        self._agent_last_text: str = ""
        self._agent_done: asyncio.Event | None = None
        self._scopes: list[str] = []

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def needs_pairing(self) -> bool:
        return self.connected and "operator.write" not in self._scopes

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        if self.connected:
            return
        log.info(f"Connecting to OpenClaw Gateway at {self.gateway_url}")
        self._ws = await websockets.connect(
            self.gateway_url,
            max_size=16 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        )

        # Receive the connect.challenge
        raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
        challenge = json.loads(raw)
        log.debug(f"OpenClaw challenge: {challenge}")
        nonce = challenge.get("payload", {}).get("nonce", "")
        if not nonce:
            raise RuntimeError("OpenClaw: connect.challenge missing nonce")

        # Build signed connect frame
        device_id = self._identity["deviceId"]
        pub_raw = self._pub_raw()
        pub_b64 = _b64url(pub_raw)
        signed_at_ms = int(time.time() * 1000)
        scopes = ["operator.admin", "operator.read", "operator.write"]

        sig = _sign_v3_payload(
            priv_pem=self._identity["privateKeyPem"],
            device_id=device_id,
            client_id="gateway-client",
            client_mode="backend",
            role="operator",
            scopes=scopes,
            signed_at_ms=signed_at_ms,
            token="",
            nonce=nonce,
            platform="linux",
            device_family="server",
        )

        auth: dict[str, Any] = {}
        if self._password:
            auth["password"] = self._password

        connect_frame = {
            "type": "req", "id": "connect", "method": "connect",
            "params": {
                "auth": auth,
                "role": "operator",
                "scopes": scopes,
                "minProtocol": 4,
                "maxProtocol": 4,
                "client": {
                    "id": "gateway-client",
                    "mode": "backend",
                    "version": "2026.5.20",
                    "platform": "linux",
                    "deviceFamily": "server",
                },
                "device": {
                    "id": device_id,
                    "publicKey": pub_b64,
                    "signature": sig,
                    "signedAt": signed_at_ms,
                    "nonce": nonce,
                },
            },
        }
        await self._ws.send(json.dumps(connect_frame))
        resp_raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
        resp = json.loads(resp_raw)
        log.debug(f"OpenClaw connect response: {json.dumps(resp)[:500]}")

        if not resp.get("ok"):
            err = resp.get("error", {}).get("message", str(resp))
            details = resp.get("error", {}).get("details", {})
            code = details.get("code", "")
            if code == "PAIRING_REQUIRED":
                log.warning(
                    f"OpenClaw device needs pairing approval — run "
                    f"'openclaw devices approve --latest' on the Gateway host. "
                    f"Device ID: {device_id}"
                )
            raise RuntimeError(f"OpenClaw connect failed: {err}")

        auth_resp = resp.get("payload", {}).get("auth", {})
        self._scopes = auth_resp.get("scopes", [])
        log.info(
            f"OpenClaw Gateway connected "
            f"(role={auth_resp.get('role')}, scopes={self._scopes})"
        )

        self._reader_task = asyncio.create_task(self._reader())
        self._connected.set()

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
        self._session_key = ""
        log.info("OpenClaw Gateway disconnected")

    # ------------------------------------------------------------------
    # Send a message to the agent
    # ------------------------------------------------------------------

    async def send_message(
        self,
        text: str,
        timeout: float = 120.0,
    ) -> OpenClawResponse:
        if not self._ws or not self.connected:
            raise ConnectionError("OpenClaw Gateway not connected")
        if "operator.write" not in self._scopes:
            raise PermissionError(
                "OpenClaw device not paired (no operator.write scope). "
                "Run 'openclaw devices approve --latest' on the Gateway host."
            )

        # Ensure we have a session
        if not self._session_key:
            await self._create_session()

        t0 = time.monotonic()
        self._agent_last_text = ""
        self._agent_done = asyncio.Event()

        msg_id = await self._send({
            "type": "req",
            "method": "sessions.send",
            "params": {"key": self._session_key, "message": text},
        })

        # sessions.send returns immediately with {status:"started"}.
        # The actual agent text arrives via `chat` events. Wait for
        # those events, detecting completion when no new chat arrives
        # for a settling period after the initial response.
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[msg_id] = fut
        try:
            await asyncio.wait_for(fut, timeout=15.0)
        except asyncio.TimeoutError:
            log.warning("OpenClaw sessions.send ack timed out (continuing)")
        finally:
            self._pending.pop(msg_id, None)

        # Wait for agent text — chat events stream in. We detect
        # completion when _agent_done is set (by _handle_event seeing
        # an agent-complete signal) OR by settling timeout.
        try:
            await asyncio.wait_for(
                self._wait_for_agent_text(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            log.warning(
                f"OpenClaw agent timed out after {timeout}s "
                f"(partial text len={len(self._agent_last_text)})"
            )

        elapsed = time.monotonic() - t0
        final_text = self._agent_last_text
        log.info(
            f"OpenClaw agent done in {elapsed:.2f}s "
            f"(text_len={len(final_text)})"
        )

        response = self._parse_response({}, streamed_text=final_text)
        response.agent_time_s = elapsed
        self._agent_done = None
        return response

    async def _wait_for_agent_text(self) -> None:
        """Wait for the agent to finish streaming text. Returns when
        no new chat event arrives for 3 s after at least one chat was
        received, or when an explicit done signal arrives.
        """
        # Wait until we have at least some text
        deadline = time.monotonic() + 120.0
        while not self._agent_last_text and time.monotonic() < deadline:
            if self._agent_done and self._agent_done.is_set():
                return
            await asyncio.sleep(0.5)

        # Settle: wait until text stops changing for 3 s
        last_seen = self._agent_last_text
        stable_since = time.monotonic()
        while time.monotonic() < deadline:
            if self._agent_done and self._agent_done.is_set():
                return
            await asyncio.sleep(0.5)
            current = self._agent_last_text
            if current != last_seen:
                last_seen = current
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= 3.0:
                return

    async def _create_session(self) -> None:
        msg_id = await self._send({
            "type": "req",
            "method": "sessions.create",
            "params": {"agentId": self.agent_id},
        })
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[msg_id] = fut
        try:
            result = await asyncio.wait_for(fut, timeout=15.0)
        finally:
            self._pending.pop(msg_id, None)
        self._session_key = result.get("payload", {}).get("key", "")
        if not self._session_key:
            raise RuntimeError(f"OpenClaw sessions.create failed: {result}")
        log.info(f"OpenClaw session created: {self._session_key}")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _pub_raw(self) -> bytes:
        from cryptography.hazmat.primitives import serialization as _ser
        pub = _ser.load_pem_public_key(self._identity["publicKeyPem"].encode())
        return pub.public_bytes(_ser.Encoding.Raw, _ser.PublicFormat.Raw)

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
                    ev = msg.get("event", "")
                    if ev not in ("tick", "health", "heartbeat"):
                        log.info(
                            f"OpenClaw event: {ev} "
                            f"payload_keys={list((msg.get('payload') or {}).keys())[:8]}"
                        )
                    self._handle_event(msg)
                elif msg_type == "ack":
                    pass
                elif msg_type in ("heartbeat", "ping"):
                    if msg_type == "ping":
                        try:
                            await self._ws.send(
                                json.dumps({"type": "pong", "id": msg_id})
                            )
                        except Exception:
                            pass
                else:
                    log.debug(f"OpenClaw unhandled frame type={msg_type}")
        except asyncio.CancelledError:
            raise
        except websockets.ConnectionClosed as e:
            log.warning(f"OpenClaw WS closed: {e}")
        except Exception as e:
            log.error(f"OpenClaw reader error: {e}", exc_info=True)
        finally:
            self._connected.clear()

    def _handle_event(self, msg: dict) -> None:
        event = msg.get("event", "")
        payload = msg.get("payload", {})
        if not isinstance(payload, dict):
            return

        if event == "chat":
            # chat events carry cumulative assistant text. The message
            # dict may be at payload.message, payload.content, payload.data,
            # or payload itself.
            # Shape: {role:"assistant", content:[{type:"text", text:"..."}]}
            for candidate in [
                payload.get("message") if isinstance(payload.get("message"), dict) else None,
                payload,
                payload.get("content") if isinstance(payload.get("content"), dict) else None,
                payload.get("data") if isinstance(payload.get("data"), dict) else None,
            ]:
                if not isinstance(candidate, dict):
                    continue
                role = candidate.get("role", "")
                if role != "assistant":
                    continue
                content_blocks = candidate.get("content", [])
                if isinstance(content_blocks, list):
                    text = ""
                    for block in content_blocks:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text += block.get("text", "")
                    if text:
                        self._agent_last_text = text
                        log.info(
                            f"OpenClaw chat: {len(text)} chars "
                            f"(preview: {text[:80]!r})"
                        )
                        return
                elif isinstance(content_blocks, str) and content_blocks:
                    self._agent_last_text = content_blocks
                    log.info(f"OpenClaw chat: {len(content_blocks)} chars")
                    return
            # Didn't match any known shape — log for debugging
            log.warning(
                f"OpenClaw chat: unrecognized payload shape "
                f"keys={list(payload.keys())[:10]} "
                f"types={{k: type(v).__name__ for k, v in list(payload.items())[:5]}}"
            )
        elif event == "agent":
            # agent events may signal completion
            data = payload.get("data", payload)
            if isinstance(data, dict):
                status = data.get("status", "")
                if status in ("completed", "done", "finished", "idle"):
                    if self._agent_done:
                        self._agent_done.set()
        elif event in ("tick", "health", "heartbeat"):
            pass
        else:
            log.debug(f"OpenClaw event: {event} keys={list(payload.keys())[:8]}")

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_response(
        payload: dict,
        streamed_text: str = "",
    ) -> OpenClawResponse:
        resp = OpenClawResponse()
        if not isinstance(payload, dict):
            resp.text = str(payload) if payload else streamed_text
            return resp

        # Text: prefer streamed content, fall back to payload keys
        text = streamed_text
        if not text:
            for key in ("message", "text", "content", "response", "reply"):
                if key in payload and isinstance(payload[key], str):
                    text = payload[key]
                    break

        # Structured media arrays
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

        # Extract markdown media from text
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

        # Clean text for TTS
        if text:
            clean = _MD_IMAGE_RE.sub("", text)
            clean = _MD_LINK_RE.sub(
                lambda m: m.group(0).split("]")[0].lstrip("["), clean,
            )
            clean = _MD_STRIP_RE.sub("", clean)
            clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
            resp.text = clean
        else:
            resp.text = ""

        return resp
