"""Capture a screenshot of the kiosk page via Chrome DevTools Protocol.

Connects to the kiosk Chromium's remote debugging endpoint, picks the
page target, and asks for a JPEG screenshot. Returns the bytes of
exactly what the kiosk is rendering (live video frames included) —
much more accurate and lighter than the previous html2canvas path.

The kiosk Chromium must be launched with:
    --remote-debugging-port=9222
    --remote-debugging-address=0.0.0.0   (or another IP the audio_streamer container can reach)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging

import aiohttp
import websockets

log = logging.getLogger("hal.cdp")


async def capture_screenshot(
    debug_url: str,
    *,
    quality: int = 80,
    timeout: float = 5.0,
) -> bytes | None:
    """Return JPEG bytes of the kiosk page, or None on failure.

    debug_url: base URL of the Chromium remote-debugging endpoint,
    e.g. ``http://host.docker.internal:9222`` (no trailing slash).
    """
    debug_url = debug_url.rstrip("/")

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            async with session.get(f"{debug_url}/json") as resp:
                resp.raise_for_status()
                targets = await resp.json()
    except Exception as e:
        log.warning(f"CDP target list failed at {debug_url}: {e}")
        return None

    page = next((t for t in targets if t.get("type") == "page"), None)
    if not page:
        log.warning("CDP: no page target available")
        return None
    ws_url = page.get("webSocketDebuggerUrl")
    if not ws_url:
        log.warning(f"CDP: page target has no webSocketDebuggerUrl: {page}")
        return None

    try:
        async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
            await ws.send(json.dumps({
                "id": 1,
                "method": "Page.captureScreenshot",
                "params": {"format": "jpeg", "quality": quality},
            }))
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    log.warning("CDP screenshot: response timed out")
                    return None
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                msg = json.loads(raw)
                if msg.get("id") != 1:
                    continue  # async event from the page; ignore
                if "error" in msg:
                    log.warning(f"CDP captureScreenshot error: {msg['error']}")
                    return None
                data = (msg.get("result") or {}).get("data", "")
                return base64.b64decode(data) if data else None
    except Exception as e:
        log.warning(f"CDP screenshot failed: {e}")
        return None
