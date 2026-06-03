"""Tests for satellite-mode routing primitives (server/app/main.py helpers +
the satellite TTS endpoint). Uses lightweight fake state objects (SimpleNamespace
+ AsyncMock websockets), mirroring tests/test_pairing.py / test_photo_frame.py."""
import json
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.app.main import (
    broadcast_to_ui,
    broadcast_force_action,
    send_to_device,
    send_to_satellites,
    speak_to_satellites,
    _deregister_satellite,
    cache_satellite_tts,
)


def _state():
    return SimpleNamespace(
        ui_clients=set(),
        satellite_ws={},          # token -> ws
        satellite_ws_tokens={},   # ws -> token
        satellite_tts={},
    )


class _WS:
    """Hashable fake websocket (identity-based) usable as a dict key / set member."""
    def __init__(self):
        self.send_text = AsyncMock()


def _ws():
    return _WS()


# --- send_to_device ---------------------------------------------------------

@pytest.mark.asyncio
async def test_send_to_device_targets_one_ws():
    st = _state()
    ws = _ws()
    st.satellite_ws["tokA"] = ws
    st.satellite_ws_tokens[ws] = "tokA"
    ok = await send_to_device(st, "tokA", {"type": "response", "text": "hi"})
    assert ok is True
    ws.send_text.assert_awaited_once()
    assert json.loads(ws.send_text.await_args.args[0]) == {"type": "response", "text": "hi"}


@pytest.mark.asyncio
async def test_send_to_device_absent_returns_false():
    st = _state()
    assert await send_to_device(st, "nope", {"type": "x"}) is False


@pytest.mark.asyncio
async def test_send_to_device_deregisters_on_error():
    st = _state()
    ws = _ws()
    ws.send_text = AsyncMock(side_effect=RuntimeError("closed"))
    st.satellite_ws["tokA"] = ws
    st.satellite_ws_tokens[ws] = "tokA"
    ok = await send_to_device(st, "tokA", {"type": "x"})
    assert ok is False
    assert "tokA" not in st.satellite_ws and ws not in st.satellite_ws_tokens


# --- broadcast_to_ui excludes satellites ------------------------------------

@pytest.mark.asyncio
async def test_broadcast_excludes_satellites():
    st = _state()
    mirror = _ws()
    sat = _ws()
    st.ui_clients = {mirror, sat}
    st.satellite_ws["tokS"] = sat
    st.satellite_ws_tokens[sat] = "tokS"
    await broadcast_to_ui(st, {"type": "state", "state": "speaking"})
    mirror.send_text.assert_awaited_once()      # mirror gets the global broadcast
    sat.send_text.assert_not_awaited()          # satellite is excluded


# --- force-action fan-out (send_to_satellites / broadcast_force_action) ------

@pytest.mark.asyncio
async def test_send_to_satellites_fans_out_to_all():
    st = _state()
    a, b = _ws(), _ws()
    st.satellite_ws = {"tA": a, "tB": b}
    st.satellite_ws_tokens = {a: "tA", b: "tB"}
    n = await send_to_satellites(st, {"type": "set_theme", "name": "sunset"})
    assert n == 2
    a.send_text.assert_awaited_once()
    b.send_text.assert_awaited_once()
    assert json.loads(a.send_text.await_args.args[0])["name"] == "sunset"


@pytest.mark.asyncio
async def test_broadcast_force_action_hits_mirrors_and_satellites():
    st = _state()
    mirror, sat = _ws(), _ws()
    st.ui_clients = {mirror, sat}
    st.satellite_ws = {"tS": sat}
    st.satellite_ws_tokens = {sat: "tS"}
    await broadcast_force_action(st, {"type": "show_camera", "image": "b64"})
    # mirror gets it via broadcast_to_ui; satellite gets it via the fan-out
    mirror.send_text.assert_awaited_once()
    sat.send_text.assert_awaited_once()
    assert json.loads(sat.send_text.await_args.args[0])["type"] == "show_camera"


# --- speak_to_satellites ----------------------------------------------------

@pytest.mark.asyncio
async def test_speak_to_satellites_sends_text_and_tts():
    st = _state()
    sat = _ws()
    st.satellite_ws = {"tS": sat}
    st.satellite_ws_tokens = {sat: "tS"}
    n = await speak_to_satellites(st, "Dinner is ready", b"RIFF....WAVE")
    assert n == 1
    # two frames: the response text, then the tts_play cue
    assert sat.send_text.await_count == 2
    sent = [json.loads(c.args[0]) for c in sat.send_text.await_args_list]
    assert sent[0] == {"type": "response", "text": "Dinner is ready"}
    assert sent[1]["type"] == "tts_play" and sent[1]["seq"] == 1
    # audio cached for the device's GET /api/satellite/tts fetch
    assert st.satellite_tts["tS"]["audio"] == b"RIFF....WAVE"


@pytest.mark.asyncio
async def test_speak_to_satellites_text_only_without_audio():
    st = _state()
    sat = _ws()
    st.satellite_ws = {"tS": sat}
    st.satellite_ws_tokens = {sat: "tS"}
    n = await speak_to_satellites(st, "Heads up", None)
    assert n == 0  # no audio cue delivered
    sat.send_text.assert_awaited_once()  # only the response text
    assert "tS" not in st.satellite_tts


@pytest.mark.asyncio
async def test_speak_to_satellites_none_connected():
    st = _state()
    assert await speak_to_satellites(st, "nobody home", b"RIFF") == 0


# --- cache_satellite_tts ----------------------------------------------------

def test_cache_satellite_tts_increments_seq():
    st = _state()
    s1 = cache_satellite_tts(st, "tokA", b"RIFFxxxx", "audio/wav")
    s2 = cache_satellite_tts(st, "tokA", b"RIFFyyyy", "audio/wav")
    assert s1 == 1 and s2 == 2
    assert st.satellite_tts["tokA"]["audio"] == b"RIFFyyyy"
    assert st.satellite_tts["tokA"]["mime"] == "audio/wav"


def test_deregister_satellite_idempotent():
    st = _state()
    ws = _ws()
    st.satellite_ws["t"] = ws
    st.satellite_ws_tokens[ws] = "t"
    _deregister_satellite(st, ws)
    assert "t" not in st.satellite_ws and ws not in st.satellite_ws_tokens
    _deregister_satellite(st, ws)  # no error second time


# --- GET /api/satellite/tts -------------------------------------------------

@pytest.fixture
def fake_main(monkeypatch):
    """Install a fake server.app.main exposing _get_state for the route handler."""
    st = _state()
    st.pairing = SimpleNamespace(is_valid_token=lambda t: t == "tokA")
    fake = SimpleNamespace(_get_state=lambda app: st)
    monkeypatch.setitem(sys.modules, "server.app.main", fake)
    return st


def _req(token=None):
    headers = {"authorization": f"Bearer {token}"} if token else {}
    return SimpleNamespace(app=SimpleNamespace(), headers=headers)


@pytest.mark.asyncio
async def test_satellite_tts_requires_connected_satellite(fake_main):
    from server.app.routes_http import get_satellite_tts
    # valid token but NOT connected as a satellite → 401
    assert (await get_satellite_tts(_req("tokA"))).status_code == 401
    # no token → 401
    assert (await get_satellite_tts(_req())).status_code == 401


@pytest.mark.asyncio
async def test_satellite_tts_returns_cached_audio(fake_main):
    from server.app.routes_http import get_satellite_tts
    fake_main.satellite_ws["tokA"] = _ws()
    cache_satellite_tts(fake_main, "tokA", b"RIFF....WAVE", "audio/wav")
    resp = await get_satellite_tts(_req("tokA"))
    assert resp.status_code == 200
    assert resp.body == b"RIFF....WAVE"
    assert resp.media_type == "audio/wav"


@pytest.mark.asyncio
async def test_satellite_tts_404_when_empty_or_expired(fake_main):
    from server.app.routes_http import get_satellite_tts
    fake_main.satellite_ws["tokA"] = _ws()
    # no cache → 404
    assert (await get_satellite_tts(_req("tokA"))).status_code == 404
    # expired cache → 404
    fake_main.satellite_tts["tokA"] = {"audio": b"x", "mime": "audio/wav", "ts": time.monotonic() - 120, "seq": 1}
    assert (await get_satellite_tts(_req("tokA"))).status_code == 404
