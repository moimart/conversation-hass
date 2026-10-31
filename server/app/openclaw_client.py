"""OpenClaw client — sends messages to the gateway channel webhook."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("hal.openclaw_client")

TIMEOUT_S = 120.0


@dataclass
class OpenClawResponse:
    text: str = ""
    media_urls: list[str] = field(default_factory=list)
    request_id: str = ""


class OpenClawClient:
    def __init__(self, gateway_url: str, hal_base_url: str):
        self.gateway_url = gateway_url.rstrip("/")
        self.hal_base_url = hal_base_url.rstrip("/")
        self._pending: dict[str, asyncio.Future[OpenClawResponse]] = {}
        self._gateway_pass = ""
        self._http = httpx.AsyncClient(timeout=15.0)

    def set_gateway_password(self, password: str) -> None:
        self._gateway_pass = password

    async def send_message(self, text: str) -> OpenClawResponse:
        """Send one turn to the PAL channel plugin and await its reply.

        The reply arrives on our /api/openclaw/response callback (resolve_
        response). Since OpenClaw 2026.10 the plugin runs the whole turn inside
        the webhook request (work admission), so that POST stays open for the
        turn's duration: it runs in the background and only an early failure
        (gateway down, plugin error before any reply) aborts the wait."""
        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[OpenClawResponse] = loop.create_future()
        self._pending[request_id] = future

        webhook_url = f"{self.gateway_url}/channels/hal/webhook"
        payload = {
            "text": text,
            "request_id": request_id,
            "sender_id": "kiosk-user",
        }
        headers = {}
        if self._gateway_pass:
            headers["Authorization"] = f"Bearer {self._gateway_pass}"

        async def _post() -> str:
            try:
                resp = await self._http.post(webhook_url, json=payload, headers=headers,
                                             timeout=httpx.Timeout(TIMEOUT_S + 10, connect=5.0))
            except httpx.HTTPError as e:
                raise RuntimeError(f"OpenClaw webhook unreachable: {e}") from e
            if resp.status_code != 200:
                raise RuntimeError(f"OpenClaw webhook returned {resp.status_code}: {resp.text[:200]}")
            try:
                return str((resp.json() or {}).get("status") or "")
            except ValueError:
                return ""

        started = loop.time()
        post_task = asyncio.create_task(_post())
        log.info(f"Message sent to OpenClaw (request_id={request_id})")
        try:
            done, _ = await asyncio.wait({future, post_task}, timeout=TIMEOUT_S,
                                         return_when=asyncio.FIRST_COMPLETED)
            if future in done:
                return future.result()
            if post_task in done:
                exc = post_task.exception()
                if exc is not None:
                    raise exc
                if post_task.result() == "completed":
                    # New plugin: the turn has settled. Allow a short grace for a
                    # reply callback that may still be in flight.
                    try:
                        return await asyncio.wait_for(future, timeout=5.0)
                    except asyncio.TimeoutError:
                        raise TimeoutError(
                            f"OpenClaw finished without a reply (request_id={request_id})")
                # Pre-2026.10 plugin ("accepted"): the turn runs on after the ack.
                remaining = max(0.0, TIMEOUT_S - (loop.time() - started))
                done, _ = await asyncio.wait({future}, timeout=remaining)
                if future in done:
                    return future.result()
            raise TimeoutError(
                f"OpenClaw did not respond within {TIMEOUT_S}s (request_id={request_id})")
        finally:
            self._pending.pop(request_id, None)
            if not post_task.done():
                # Reply already delivered; let the webhook finish quietly.
                post_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    def resolve_response(self, request_id: str, data: dict) -> None:
        future = self._pending.pop(request_id, None)
        if future is None:
            log.warning(
                f"No pending request for request_id={request_id} "
                "(late response or already timed out)"
            )
            return
        if future.done():
            return

        response = OpenClawResponse(
            text=data.get("text", ""),
            media_urls=data.get("media_urls", []),
            request_id=request_id,
        )
        future.set_result(response)
        log.info(
            f"OpenClaw response resolved (request_id={request_id}, "
            f"text_len={len(response.text)}, media={len(response.media_urls)})"
        )

    async def close(self) -> None:
        await self._http.aclose()
        for rid, fut in list(self._pending.items()):
            if not fut.done():
                fut.cancel()
            self._pending.pop(rid, None)
