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
        try:
            resp = await self._http.post(webhook_url, json=payload, headers=headers)
            if resp.status_code != 200:
                self._pending.pop(request_id, None)
                raise RuntimeError(
                    f"OpenClaw webhook returned {resp.status_code}: {resp.text}"
                )
            log.info(f"Message sent to OpenClaw (request_id={request_id})")
        except httpx.HTTPError as e:
            self._pending.pop(request_id, None)
            raise RuntimeError(f"OpenClaw webhook unreachable: {e}") from e

        try:
            return await asyncio.wait_for(future, timeout=TIMEOUT_S)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise TimeoutError(
                f"OpenClaw did not respond within {TIMEOUT_S}s "
                f"(request_id={request_id})"
            )

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
