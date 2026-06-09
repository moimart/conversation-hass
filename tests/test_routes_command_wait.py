"""POST /api/command wait_reply=true — synchronous reply for the watch.

The watch's scope deliberately excludes /ws/ui, so it can't receive the turn's
reply asynchronously like phone satellites do. wait_reply=true makes the route
await the turn inline and return the latest assistant reply in the HTTP
response. Default (wait_reply absent/false) keeps the fire-and-forget ACK.
"""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _conversation(reply="The lights are off."):
    conv = SimpleNamespace(
        _pending_origin=None,
        _command_buffer=[],
        _wake_detected=False,
        history=[{"role": "user", "content": "earlier"},
                 {"role": "assistant", "content": "earlier reply"}],
    )

    async def on_silence():
        text = " ".join(conv._command_buffer)
        conv._command_buffer.clear()
        conv.history.append({"role": "user", "content": text})
        conv.history.append({"role": "assistant", "content": reply})

    conv.on_silence = on_silence
    return conv


@pytest.fixture
def fake_main(monkeypatch):
    state = SimpleNamespace(
        pairing=None,
        conversation=_conversation(),
        satellite_ws={},
        audio_websocket=None,
        ui_clients=set(),
    )
    fake = SimpleNamespace(
        _get_state=lambda app: state,
        broadcast_to_ui=AsyncMock(),
        send_to_device=AsyncMock(),
        _record_user_activity=lambda s: None,
        _dismiss_photo_frame_async=lambda s, reason="": None,
    )
    monkeypatch.setitem(sys.modules, "server.app.main", fake)
    monkeypatch.delenv("HAL_REQUIRE_TOKEN", raising=False)
    return state


def _req():
    return SimpleNamespace(app=SimpleNamespace(), headers={}, query_params={})


@pytest.mark.asyncio
async def test_wait_reply_returns_assistant_text(fake_main):
    from server.app.routes_http import post_command, CommandRequest
    out = await post_command(_req(), CommandRequest(text="turn off the lights",
                                                    wait_reply=True))
    assert out == {"status": "ok", "reply": "The lights are off."}


@pytest.mark.asyncio
async def test_default_stays_fire_and_forget(fake_main):
    from server.app.routes_http import post_command, CommandRequest
    out = await post_command(_req(), CommandRequest(text="turn off the lights"))
    assert out == {"status": "ok", "message": "Command received"}
    assert "reply" not in out
    await asyncio.sleep(0)   # let the background task run before teardown


@pytest.mark.asyncio
async def test_wait_reply_timeout_returns_ok_empty(fake_main, monkeypatch):
    from server.app import routes_http
    from server.app.routes_http import post_command, CommandRequest

    async def never_finishes():
        await asyncio.sleep(3600)

    fake_main.conversation.on_silence = never_finishes
    real_wait_for = asyncio.wait_for

    async def instant_timeout(coro, timeout):
        return await real_wait_for(coro, timeout=0.01)

    monkeypatch.setattr(routes_http.asyncio, "wait_for", instant_timeout)
    out = await post_command(_req(), CommandRequest(text="x", wait_reply=True))
    assert out["status"] == "ok" and out["reply"] == ""
