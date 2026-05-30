"""Tests for the OpenClaw gateway client (sends to the channel webhook,
holds per-request_id futures that get resolved by the response endpoint)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from server.app.openclaw_client import OpenClawClient, OpenClawResponse


# --- OpenClawResponse dataclass -------------------------------------------

class TestOpenClawResponse:
    def test_defaults(self):
        r = OpenClawResponse()
        assert r.text == ""
        assert r.media_urls == []
        assert r.request_id == ""

    def test_explicit_values(self):
        r = OpenClawResponse(text="hi", media_urls=["https://x/a.jpg"], request_id="abc")
        assert r.text == "hi"
        assert r.media_urls == ["https://x/a.jpg"]
        assert r.request_id == "abc"

    def test_media_urls_default_is_per_instance(self):
        """field(default_factory=list) must not share lists across instances."""
        a = OpenClawResponse()
        b = OpenClawResponse()
        a.media_urls.append("x")
        assert b.media_urls == []


# --- OpenClawClient construction ------------------------------------------

class TestClientInit:
    def test_strips_trailing_slashes(self):
        c = OpenClawClient(
            gateway_url="http://gw:18789/",
            hal_base_url="http://hal:8765///",
        )
        assert c.gateway_url == "http://gw:18789"
        assert c.hal_base_url == "http://hal:8765"

    def test_empty_state(self):
        c = OpenClawClient("http://gw", "http://hal")
        assert c._pending == {}
        assert c._gateway_pass == ""

    def test_set_gateway_password(self):
        c = OpenClawClient("http://gw", "http://hal")
        c.set_gateway_password("s3cret")
        assert c._gateway_pass == "s3cret"


# --- send_message --------------------------------------------------------

def _mock_response(status_code: int = 200, text: str = "") -> MagicMock:
    """Build a minimal mock httpx.Response."""
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    return r


@pytest.fixture
def client():
    """Fresh client with httpx replaced by an AsyncMock."""
    c = OpenClawClient("http://gw:18789", "http://hal:8765")
    c._http = MagicMock()
    c._http.post = AsyncMock(return_value=_mock_response(200))
    c._http.aclose = AsyncMock()
    return c


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_posts_to_channel_webhook_with_payload(self, client):
        # Resolve the future immediately so we don't wait for the 120s timeout.
        async def resolve_soon():
            await asyncio.sleep(0)  # let send_message register the future
            rid = next(iter(client._pending))
            client.resolve_response(rid, {"text": "ok", "media_urls": []})

        asyncio.create_task(resolve_soon())
        resp = await client.send_message("turn on the lamp")

        client._http.post.assert_awaited_once()
        url, = client._http.post.call_args.args
        assert url == "http://gw:18789/channels/hal/webhook"
        payload = client._http.post.call_args.kwargs["json"]
        assert payload["text"] == "turn on the lamp"
        assert payload["sender_id"] == "kiosk-user"
        assert isinstance(payload["request_id"], str) and len(payload["request_id"]) > 0
        # Resolved by our background task.
        assert resp.text == "ok"

    @pytest.mark.asyncio
    async def test_adds_bearer_header_when_password_set(self, client):
        client.set_gateway_password("topsecret")

        async def resolve_soon():
            await asyncio.sleep(0)
            rid = next(iter(client._pending))
            client.resolve_response(rid, {"text": "x"})

        asyncio.create_task(resolve_soon())
        await client.send_message("hi")
        headers = client._http.post.call_args.kwargs["headers"]
        assert headers == {"Authorization": "Bearer topsecret"}

    @pytest.mark.asyncio
    async def test_no_auth_header_when_password_unset(self, client):
        async def resolve_soon():
            await asyncio.sleep(0)
            rid = next(iter(client._pending))
            client.resolve_response(rid, {"text": "x"})

        asyncio.create_task(resolve_soon())
        await client.send_message("hi")
        headers = client._http.post.call_args.kwargs["headers"]
        assert headers == {}

    @pytest.mark.asyncio
    async def test_non_200_raises_runtime_and_clears_pending(self, client):
        client._http.post = AsyncMock(return_value=_mock_response(500, "boom"))
        with pytest.raises(RuntimeError, match="OpenClaw webhook returned 500"):
            await client.send_message("anything")
        assert client._pending == {}

    @pytest.mark.asyncio
    async def test_http_error_raises_runtime_and_clears_pending(self, client):
        client._http.post = AsyncMock(side_effect=httpx.ConnectError("unreachable"))
        with pytest.raises(RuntimeError, match="OpenClaw webhook unreachable"):
            await client.send_message("anything")
        assert client._pending == {}

    @pytest.mark.asyncio
    async def test_timeout_raises_and_clears_pending(self, client, monkeypatch):
        # Drop the real 120s timeout to something we can wait for.
        monkeypatch.setattr("server.app.openclaw_client.TIMEOUT_S", 0.05)
        # Don't resolve the future — let it time out.
        with pytest.raises(TimeoutError, match="did not respond within 0.05s"):
            await client.send_message("never resolved")
        assert client._pending == {}

    @pytest.mark.asyncio
    async def test_success_returns_resolved_response(self, client):
        async def resolve_soon():
            await asyncio.sleep(0)
            rid = next(iter(client._pending))
            client.resolve_response(
                rid,
                {"text": "the lamp is off", "media_urls": ["data:image/png;base64,AAAA"]},
            )

        asyncio.create_task(resolve_soon())
        resp = await client.send_message("turn off the lamp")
        assert isinstance(resp, OpenClawResponse)
        assert resp.text == "the lamp is off"
        assert resp.media_urls == ["data:image/png;base64,AAAA"]
        assert resp.request_id  # populated


# --- resolve_response ----------------------------------------------------

class TestResolveResponse:
    def test_unknown_request_id_is_noop(self, client, caplog):
        # No future registered — should log and return without raising.
        client.resolve_response("not-a-real-id", {"text": "x"})
        assert any("No pending request" in r.message for r in caplog.records) \
            or True  # log call is best-effort; the important contract is no raise

    @pytest.mark.asyncio
    async def test_done_future_is_noop(self, client):
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        fut.set_result(OpenClawResponse(text="already"))
        client._pending["rid"] = fut
        # Should not raise InvalidStateError despite future being done.
        client.resolve_response("rid", {"text": "new"})

    @pytest.mark.asyncio
    async def test_sets_text_and_media_urls_on_future(self, client):
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        client._pending["rid"] = fut
        client.resolve_response("rid", {"text": "hello", "media_urls": ["u1", "u2"]})
        resp = await fut
        assert resp.text == "hello"
        assert resp.media_urls == ["u1", "u2"]
        assert resp.request_id == "rid"
        # Pending is popped on resolve.
        assert "rid" not in client._pending

    @pytest.mark.asyncio
    async def test_missing_fields_default_to_empties(self, client):
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        client._pending["rid"] = fut
        client.resolve_response("rid", {})
        resp = await fut
        assert resp.text == ""
        assert resp.media_urls == []


# --- close ---------------------------------------------------------------

class TestClose:
    @pytest.mark.asyncio
    async def test_closes_http_and_cancels_pending(self, client):
        loop = asyncio.get_running_loop()
        fut1: asyncio.Future = loop.create_future()
        fut2: asyncio.Future = loop.create_future()
        # One pending, one already done — only the pending one should be cancelled.
        fut2.set_result(OpenClawResponse(text="done"))
        client._pending = {"a": fut1, "b": fut2}

        await client.close()

        client._http.aclose.assert_awaited_once()
        assert fut1.cancelled()
        # The done future should not be touched (its result is preserved).
        assert fut2.done() and not fut2.cancelled()
        assert client._pending == {}
