"""Tests for satellite-mode routing primitives (server/app/main.py helpers +
the satellite TTS endpoint). Uses lightweight fake state objects (SimpleNamespace
+ AsyncMock websockets), mirroring tests/test_pairing.py / test_photo_frame.py."""
import json
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
        satellite_photo_sessions={},
        audio_websocket=None,
        go2rtc=None,
        ha_ws=None,
        active_stream=None,
        active_visual=None,
        active_calendar=None,
    )


class _WS:
    """Hashable fake websocket (identity-based) usable as a dict key / set member."""
    def __init__(self):
        self.send_text = AsyncMock()
        self.send_json = AsyncMock()


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


# --- dismiss_satellite_photo_frames -----------------------------------------

@pytest.mark.asyncio
async def test_dismiss_satellite_photo_frames(monkeypatch):
    from server.app import main as srv_main
    st = _state()
    st.satellite_photo_sessions = {"tA": object(), "tB": object()}
    stop = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr("server.app.photo_frame.stop_photo_frame_for_device", stop)
    await srv_main.dismiss_satellite_photo_frames(st)
    assert stop.await_count == 2
    assert {c.args[1] for c in stop.await_args_list} == {"tA", "tB"}


@pytest.mark.asyncio
async def test_speak_to_satellites_dismisses_photo_frame(monkeypatch):
    from server.app import main as srv_main
    st = _state()
    sat = _ws()
    st.satellite_ws = {"tS": sat}
    st.satellite_ws_tokens = {sat: "tS"}
    st.satellite_photo_sessions = {"tS": object()}
    stop = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr("server.app.photo_frame.stop_photo_frame_for_device", stop)
    await srv_main.speak_to_satellites(st, "Dinner", b"RIFF")
    stop.assert_awaited_once()  # idle photo frame cleared before speaking


# --- RTSP per-device negotiation (reply_ws) ---------------------------------

def _g(answer):
    from server.app.go2rtc import Go2RTCClient
    g = Go2RTCClient("http://go2rtc")
    g.webrtc_offer = AsyncMock(return_value=answer)
    return g


@pytest.mark.asyncio
async def test_rtsp_negotiate_answers_satellite_peer():
    from server.app.streaming import _negotiate_rtsp_offer
    st = _state()
    st.audio_websocket = _ws()           # kiosk peer
    st.go2rtc = _g("ANSWER_SDP")
    st.active_stream = {"session_id": "s1", "kind": "rtsp", "go2rtc_name": "hal_s1"}
    sat = _ws()
    await _negotiate_rtsp_offer(st, "s1", "OFFER_SDP", reply_ws=sat)
    sat.send_json.assert_awaited_once()
    sent = sat.send_json.await_args.args[0]
    assert sent["kind"] == "answer" and sent["sdp"] == "ANSWER_SDP"
    st.audio_websocket.send_json.assert_not_awaited()   # kiosk untouched
    assert st.active_stream is not None                 # stream stays up


@pytest.mark.asyncio
async def test_rtsp_negotiate_satellite_failure_keeps_stream():
    from server.app.streaming import _negotiate_rtsp_offer
    st = _state()
    st.audio_websocket = _ws()
    st.go2rtc = _g(None)                 # go2rtc returns no answer
    st.active_stream = {"session_id": "s1", "kind": "rtsp", "go2rtc_name": "hal_s1"}
    sat = _ws()
    await _negotiate_rtsp_offer(st, "s1", "OFFER_SDP", reply_ws=sat)
    sat.send_json.assert_not_awaited()
    assert st.active_stream is not None  # a satellite failure does NOT tear down the shared stream


@pytest.mark.asyncio
async def test_stop_active_stream_fans_stop_to_satellites():
    from server.app.streaming import _stop_active_stream
    st = _state()
    st.go2rtc = _g("x"); st.go2rtc.delete_stream = AsyncMock()
    sat = _ws()
    st.satellite_ws = {"tS": sat}
    st.satellite_ws_tokens = {sat: "tS"}
    st.active_stream = {
        "session_id": "s1", "kind": "rtsp", "go2rtc_name": "hal_s1", "safety_task": None,
    }
    await _stop_active_stream(st)
    sat.send_text.assert_awaited_once()
    assert json.loads(sat.send_text.await_args.args[0]) == {"type": "stream_stop", "session_id": "s1"}


# --- HA-camera per-peer signaling to satellites -----------------------------

def _ha_stream_state(sat, *, ha_session_id=None, sub=7):
    from server.app.ha_ws import HAWSClient
    st = _state()
    st.audio_websocket = _ws()   # kiosk peer
    st.ha_ws = MagicMock(spec=HAWSClient)
    st.ha_ws.unsubscribe = AsyncMock()
    st.ha_ws.send_command = AsyncMock()
    st.active_stream = {
        "session_id": "s1", "kind": "ha", "entity_id": "camera.x", "safety_task": None,
        "ha_sub_id": None, "ha_session_id": None,
        "peers": {id(sat): {"ws": sat, "ha_sub_id": sub, "ha_session_id": ha_session_id}},
    }
    return st


@pytest.mark.asyncio
async def test_ha_answer_routes_to_satellite_not_kiosk():
    from server.app.streaming import _on_ha_webrtc_event
    sat = _ws()
    st = _ha_stream_state(sat)
    # session event captured into the peer's slot, not forwarded
    await _on_ha_webrtc_event(st, "s1", {"type": "session", "session_id": "HA-7"}, reply_ws=sat)
    assert st.active_stream["peers"][id(sat)]["ha_session_id"] == "HA-7"
    sat.send_json.assert_not_awaited()
    # answer forwarded to the satellite only
    await _on_ha_webrtc_event(st, "s1", {"type": "answer", "answer": "v=0"}, reply_ws=sat)
    sat.send_json.assert_awaited_once()
    assert sat.send_json.await_args.args[0]["kind"] == "answer"
    st.audio_websocket.send_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_ha_satellite_error_drops_only_that_peer():
    from server.app.streaming import _on_ha_webrtc_event
    sat = _ws()
    st = _ha_stream_state(sat, ha_session_id="HA-7")
    await _on_ha_webrtc_event(st, "s1", {"type": "error", "code": "x"}, reply_ws=sat)
    assert st.active_stream is not None          # shared stream survives
    assert id(sat) not in st.active_stream["peers"]  # only this peer dropped
    st.ha_ws.unsubscribe.assert_awaited_once_with(7)
    sat.send_json.assert_awaited()               # satellite told to stop


@pytest.mark.asyncio
async def test_ha_forward_candidate_uses_peer_session_id():
    from server.app.streaming import _forward_kiosk_candidate
    sat = _ws()
    st = _ha_stream_state(sat, ha_session_id="HA-SAT")
    await _forward_kiosk_candidate(
        st, "s1", {"candidate": "c", "sdpMid": "0", "sdpMLineIndex": 0}, reply_ws=sat,
    )
    st.ha_ws.send_command.assert_awaited_once()
    assert st.ha_ws.send_command.await_args.args[0]["session_id"] == "HA-SAT"


@pytest.mark.asyncio
async def test_start_webrtc_stream_stores_satellite_sub(monkeypatch):
    from server.app import streaming
    from server.app.ha_ws import HAWSClient
    sat = _ws()
    st = _state()
    st.audio_websocket = _ws()
    st.active_stream = {"session_id": "s1", "kind": "ha", "entity_id": "camera.x"}
    ha = MagicMock(spec=HAWSClient)
    ha.subscribe = AsyncMock(return_value=99)
    monkeypatch.setattr(streaming, "_ensure_ha_ws", AsyncMock(return_value=ha))
    ok = await streaming._start_webrtc_stream(st, "camera.x", "s1", "OFFER", reply_ws=sat)
    assert ok is True
    assert st.active_stream["peers"][id(sat)]["ha_sub_id"] == 99
    assert st.active_stream.get("ha_sub_id") is None   # kiosk slot untouched


@pytest.mark.asyncio
async def test_stop_stream_unsubscribes_all_peers_and_fans_stop():
    from server.app.streaming import _stop_active_stream
    sat = _ws()
    st = _ha_stream_state(sat)
    st.active_stream["ha_sub_id"] = 1       # kiosk's own sub
    st.satellite_ws = {"tS": sat}
    st.satellite_ws_tokens = {sat: "tS"}
    await _stop_active_stream(st)
    subs = {c.args[0] for c in st.ha_ws.unsubscribe.await_args_list}
    assert subs == {1, 7}                    # kiosk sub + satellite peer sub
    sat.send_text.assert_awaited()           # satellite got stream_stop via fan-out


# --- replay_visual_state (catch-up on reconnect) ----------------------------

@pytest.mark.asyncio
async def test_replay_stream_to_reconnecting_satellite():
    from server.app.main import replay_visual_state
    st = _state()
    st.active_stream = {"session_id": "s1", "kind": "rtsp", "rtsp_url": "rtsp://cam/1"}
    ws = _ws()
    await replay_visual_state(st, ws)
    ws.send_json.assert_awaited_once()
    sent = ws.send_json.await_args.args[0]
    assert sent["type"] == "stream_start" and sent["rtsp_url"] == "rtsp://cam/1"
    assert sent["mode"] == "non-trickle"


@pytest.mark.asyncio
async def test_replay_visual_with_remaining_duration():
    import time as _t
    from server.app.main import replay_visual_state
    st = _state()
    st.active_visual = {
        "msg": {"type": "show_camera", "image": "b64", "duration_s": 150},
        "expires": _t.monotonic() + 60,   # 60s left of the original 150
    }
    ws = _ws()
    await replay_visual_state(st, ws)
    sent = ws.send_json.await_args.args[0]
    assert sent["type"] == "show_camera"
    assert 50 <= sent["duration_s"] <= 60   # remaining time, not the original


@pytest.mark.asyncio
async def test_replay_skips_and_clears_expired_visual():
    import time as _t
    from server.app.main import replay_visual_state
    st = _state()
    st.active_visual = {"msg": {"type": "show_camera"}, "expires": _t.monotonic() - 5}
    ws = _ws()
    await replay_visual_state(st, ws)
    ws.send_json.assert_not_awaited()
    assert st.active_visual is None


@pytest.mark.asyncio
async def test_replay_calendar_overlay():
    import time as _t
    from server.app.main import replay_visual_state
    st = _state()
    st.active_calendar = {
        "msg": {"type": "show_calendar", "view": "month", "duration_s": 30},
        "expires": _t.monotonic() + 20,
    }
    ws = _ws()
    await replay_visual_state(st, ws)
    sent = ws.send_json.await_args.args[0]
    assert sent["type"] == "show_calendar"
    assert sent["duration_s"] <= 20


# --- calendar + proactive speak fan-out --------------------------------------

@pytest.mark.asyncio
async def test_show_calendar_fans_to_satellites(monkeypatch):
    import server.app.main as srv_main
    import server.app.calendar_ha as cal
    st = _state()
    st.runtime_config = {}
    sat = _ws()
    st.satellite_ws = {"tS": sat}
    st.satellite_ws_tokens = {sat: "tS"}
    monkeypatch.setattr(srv_main, "_record_user_activity", lambda s: None)
    monkeypatch.setattr(srv_main, "_push_to_rpi", AsyncMock(return_value=True))
    monkeypatch.setattr(cal, "list_calendars", AsyncMock(return_value=[]))
    monkeypatch.setattr(cal, "fetch_calendar_events", AsyncMock(return_value=[]))
    payload = await cal._show_calendar(st, view="month")
    assert payload["type"] == "show_calendar"
    sat.send_text.assert_awaited()                       # satellite got the overlay
    assert json.loads(sat.send_text.await_args.args[0])["type"] == "show_calendar"
    assert st.active_calendar is not None                # remembered for replay
    # hide clears it and fans out too
    sat.send_text.reset_mock()
    await cal._hide_calendar(st)
    assert st.active_calendar is None
    assert json.loads(sat.send_text.await_args.args[0])["type"] == "hide_calendar"


@pytest.mark.asyncio
async def test_speak_proactively_reaches_satellites(monkeypatch):
    import server.app.main as srv_main
    from server.app.media import _speak_proactively
    st = _state()
    st.photo_frame_session = None
    st.mqtt_bridge = None
    st.pipeline = None
    st.tts_engine = SimpleNamespace(synthesize=AsyncMock(return_value=b"RIFF....WAVE"))
    sat = _ws()
    st.satellite_ws = {"tS": sat}
    st.satellite_ws_tokens = {sat: "tS"}
    monkeypatch.setattr(srv_main, "_record_user_activity", lambda s: None)
    monkeypatch.setattr(srv_main, "_dismiss_photo_frame_async", lambda s, reason: None)
    ok = await _speak_proactively(st, "Heads up, Master")
    assert ok is True
    sent = [json.loads(c.args[0]) for c in sat.send_text.await_args_list]
    types = [m["type"] for m in sent]
    assert "response" in types and "tts_play" in types   # text + HAL's voice
    assert st.satellite_tts["tS"]["audio"] == b"RIFF....WAVE"


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
