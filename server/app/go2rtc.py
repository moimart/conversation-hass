"""Tiny client for the go2rtc sidecar's HTTP API.

We use only two operations: register a stream from an RTSP URL, and
exchange a WebRTC offer for an answer. ICE candidates are bundled into
the offer/answer SDPs (non-trickle), so no separate signaling channel
to go2rtc is required.

Ref: https://github.com/AlexxIT/go2rtc#api
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("hal.go2rtc")


class Go2RTCClient:
    """Async wrapper around the go2rtc REST API."""

    def __init__(self, base_url: str, *, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def register_stream(self, name: str, src: str) -> bool:
        """Register or replace a named stream pointing at the given source URL."""
        url = f"{self.base_url}/api/streams"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                # PUT replaces an existing stream with the same name.
                r = await client.put(url, params={"name": name, "src": src})
                r.raise_for_status()
            log.info(f"go2rtc: registered stream {name!r} -> {src!r}")
            return True
        except Exception as e:
            log.error(f"go2rtc: register_stream({name!r}) failed: {e}")
            return False

    async def delete_stream(self, name: str) -> bool:
        """Unregister a previously registered stream by name."""
        url = f"{self.base_url}/api/streams"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.delete(url, params={"src": name})
                r.raise_for_status()
            log.debug(f"go2rtc: deleted stream {name!r}")
            return True
        except Exception as e:
            log.debug(f"go2rtc: delete_stream({name!r}) failed: {e}")
            return False

    async def webrtc_offer(self, name: str, offer_sdp: str) -> str | None:
        """Send an SDP offer for a stream. Returns the answer SDP or None."""
        url = f"{self.base_url}/api/webrtc"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(
                    url,
                    params={"src": name},
                    json={"type": "offer", "sdp": offer_sdp},
                )
                r.raise_for_status()
                data: Any = r.json()
        except Exception as e:
            log.error(f"go2rtc: webrtc_offer({name!r}) failed: {e}")
            return None
        if not isinstance(data, dict) or data.get("type") != "answer":
            log.error(f"go2rtc: unexpected webrtc response: {data!r}")
            return None
        return str(data.get("sdp") or "") or None
