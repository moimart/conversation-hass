"""WebRTC / go2rtc / RTSP live streaming.

Signaling glue between the kiosk, Home Assistant's WebRTC offer/candidate API,
and go2rtc's RTSP→WebRTC bridge. Extracted from main.py; callers reach these
via the re-export in main (and the lazy `from .main import` pattern elsewhere).
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from .ha_ws import HAWSClient
from .go2rtc import Go2RTCClient

if TYPE_CHECKING:
    from .main import AppState

log = logging.getLogger("hal")


async def _ensure_ha_ws(state: AppState) -> HAWSClient | None:
    """Lazy-connect the HA WS client. Returns None if HA_URL/HA_TOKEN unset."""
    ha_url = os.environ.get("HA_URL", "").strip()
    ha_token = os.environ.get("HA_TOKEN", "").strip()
    if not ha_url or not ha_token:
        return None
    if state.ha_ws and getattr(state.ha_ws, "connected", False):
        return state.ha_ws
    client = HAWSClient(ha_url, ha_token)
    await client.connect()
    state.ha_ws = client
    return client


def _peer_slot(state: AppState, reply_ws, *, create: bool = True) -> dict | None:
    """Return the per-peer signaling slot for an HA-camera stream.

    The kiosk (reply_ws None or the audio_websocket) uses the active_stream dict
    itself, preserving the original single-peer fields (`ha_sub_id`,
    `ha_session_id`). Each satellite gets its own slot under
    active_stream['peers'][id(ws)] so its HA subscription, HA session id, and
    answer/candidate routing stay independent."""
    sess = state.active_stream
    if sess is None:
        return None
    if reply_ws is None or reply_ws is state.audio_websocket:
        return sess  # kiosk slot == the stream dict (legacy fields)
    peers = sess.setdefault("peers", {})
    key = id(reply_ws)
    if key not in peers:
        if not create:
            return None
        peers[key] = {"ws": reply_ws, "ha_sub_id": None, "ha_session_id": None}
    return peers[key]


async def _drop_stream_peer(state: AppState, reply_ws) -> None:
    """Tear down ONE satellite peer of an HA-camera stream without disturbing the
    others (used when that peer errors/disconnects). Unsubscribes its HA offer and
    tells just that device to stop. The kiosk is never dropped this way."""
    sess = state.active_stream
    if sess is None or reply_ws is None or reply_ws is state.audio_websocket:
        return
    peers = sess.get("peers") or {}
    entry = peers.pop(id(reply_ws), None)
    if entry is None:
        return
    sub = entry.get("ha_sub_id")
    if sub is not None and isinstance(state.ha_ws, HAWSClient):
        try:
            await state.ha_ws.unsubscribe(sub)
        except Exception:
            pass
    try:
        await reply_ws.send_json({"type": "stream_stop", "session_id": sess.get("session_id", "")})
    except Exception:
        pass


async def _stop_active_stream(state: AppState, *, notify_kiosk: bool = True) -> None:
    """Tear down the active WebRTC session (if any) for every peer."""
    from .main import broadcast_to_ui
    session = state.active_stream
    if not session:
        return
    state.active_stream = None
    safety = session.get("safety_task")
    if safety:
        safety.cancel()
    # HA path — unsubscribe the kiosk's offer plus every satellite peer's offer.
    subs = [session.get("ha_sub_id")]
    subs += [p.get("ha_sub_id") for p in (session.get("peers") or {}).values()]
    for sub_id in subs:
        if sub_id is not None and isinstance(state.ha_ws, HAWSClient):
            try:
                await state.ha_ws.unsubscribe(sub_id)
            except Exception:
                pass
    # go2rtc path
    go2rtc_name = session.get("go2rtc_name")
    if go2rtc_name and isinstance(state.go2rtc, Go2RTCClient):
        await state.go2rtc.delete_stream(go2rtc_name)
    if notify_kiosk:
        msg = {"type": "stream_stop", "session_id": session.get("session_id", "")}
        ws = state.audio_websocket
        if ws:
            try:
                await ws.send_json(msg)
            except Exception:
                pass
        await broadcast_to_ui(state, msg)
        # Live streams are fanned out to satellites — tell them to tear down too.
        from .main import send_to_satellites
        await send_to_satellites(state, msg)


def _ensure_go2rtc(state: AppState) -> Go2RTCClient | None:
    """Lazy-construct the go2rtc client. Returns None if GO2RTC_URL unset."""
    url = os.environ.get("GO2RTC_URL", "").strip()
    if not url:
        return None
    if isinstance(state.go2rtc, Go2RTCClient):
        return state.go2rtc
    state.go2rtc = Go2RTCClient(url)
    return state.go2rtc


async def _stop_active_video(state: AppState) -> None:
    """Tell the kiosk to clear any HTTP video that may be playing.

    We don't track video state server-side (the kiosk owns the lifecycle —
    no peer connection or HA subscription to clean up), so this is just a
    fire-and-forget message. Safe to call at any time.
    """
    from .main import broadcast_to_ui, send_to_satellites
    state.active_visual = None  # nothing left to replay to reconnecting satellites
    msg = {"type": "video_stop"}
    ws = state.audio_websocket
    if ws:
        try:
            await ws.send_json(msg)
        except Exception:
            pass
    await broadcast_to_ui(state, msg)
    await send_to_satellites(state, msg)


async def _negotiate_rtsp_offer(
    state: AppState, session_id: str, offer_sdp: str, *, reply_ws=None,
) -> None:
    """Hand an SDP offer to go2rtc and forward the answer back to the peer.

    go2rtc's WebRTC offer endpoint is stateless per-offer and the same
    registered stream serves multiple peers, so this works for the kiosk AND
    each satellite. `reply_ws` is the peer that sent the offer (the kiosk's
    audio_websocket by default; a satellite's /ws/ui when fanning out). A
    satellite peer that fails to negotiate is NOT a reason to tear down the
    shared stream — only a kiosk-side failure stops it."""
    if not state.active_stream or state.active_stream.get("session_id") != session_id:
        return
    if state.active_stream.get("kind") != "rtsp":
        return
    name = state.active_stream.get("go2rtc_name")
    if not name or not isinstance(state.go2rtc, Go2RTCClient):
        return
    is_satellite_peer = reply_ws is not None and reply_ws is not state.audio_websocket
    answer_sdp = await state.go2rtc.webrtc_offer(name, offer_sdp)
    if not answer_sdp:
        log.warning(f"go2rtc returned no answer for {name!r} (satellite={is_satellite_peer})")
        if not is_satellite_peer:
            await _stop_active_stream(state)
        return
    msg = {
        "type": "webrtc_signal",
        "session_id": session_id,
        "kind": "answer",
        "sdp": answer_sdp,
    }
    ws = reply_ws if reply_ws is not None else state.audio_websocket
    if ws:
        try:
            await ws.send_json(msg)
        except Exception as e:
            log.debug(f"forward go2rtc answer to peer failed (satellite={is_satellite_peer}): {e}")


async def _on_ha_webrtc_event(
    state: AppState, session_id: str, event: dict, *, reply_ws=None,
) -> None:
    """Forward an HA WebRTC subscription event to ONE peer as a webrtc_signal.

    `reply_ws` is the peer this HA subscription belongs to (the kiosk's
    audio_websocket by default; a satellite's /ws/ui for a fanned-out stream).
    Each peer has its own HA subscription, so events are naturally demuxed by the
    handler closure — we just route the output back to that peer's socket.

    HA emits events:
      {type: "session", session_id: <HA's id>}    — first; capture for candidate routing
      {type: "answer",  answer: <sdp>}            — forward as 'answer'
      {type: "candidate", candidate: {...}}       — forward as 'candidate'
      {type: "error",   code, message}            — tear down this peer (whole
                                                    stream if it's the kiosk)
    """
    msg_type = event.get("type")
    peer_ws = reply_ws if reply_ws is not None else state.audio_websocket
    is_satellite_peer = reply_ws is not None and reply_ws is not state.audio_websocket
    log.debug(f"HA WebRTC event for session {session_id}: type={msg_type} satellite={is_satellite_peer}")
    out: dict | None = None
    if msg_type == "session":
        slot = _peer_slot(state, reply_ws, create=False)
        if slot is not None and state.active_stream and state.active_stream.get("session_id") == session_id:
            slot["ha_session_id"] = event.get("session_id")
            log.info(f"Captured HA session_id={slot['ha_session_id']} for {session_id} (satellite={is_satellite_peer})")
        return
    elif msg_type == "answer":
        out = {
            "type": "webrtc_signal",
            "session_id": session_id,
            "kind": "answer",
            "sdp": event.get("answer"),
        }
    elif msg_type == "candidate":
        cand = event.get("candidate") or {}
        out = {
            "type": "webrtc_signal",
            "session_id": session_id,
            "kind": "candidate",
            "candidate": cand.get("candidate") if isinstance(cand, dict) else cand,
            "sdpMid": cand.get("sdpMid") if isinstance(cand, dict) else None,
            "sdpMLineIndex": cand.get("sdpMLineIndex") if isinstance(cand, dict) else None,
        }
    elif msg_type == "error":
        log.warning(f"HA WebRTC error (satellite={is_satellite_peer}): {event}")
        # A satellite's failure drops only that peer; a kiosk failure (or a
        # stream with no satellite context) tears the whole stream down.
        if is_satellite_peer:
            await _drop_stream_peer(state, reply_ws)
        else:
            await _stop_active_stream(state)
        return
    if not out:
        return
    if peer_ws:
        try:
            await peer_ws.send_json(out)
        except Exception as e:
            log.debug(f"forward HA WebRTC event to peer failed (satellite={is_satellite_peer}): {e}")


async def _start_webrtc_stream(
    state: AppState, entity_id: str, session_id: str, offer_sdp: str, *, reply_ws=None,
) -> bool:
    """Begin an HA WebRTC offer subscription for ONE peer. Returns True on success.

    `reply_ws` is the peer that sent the offer (the kiosk by default; a satellite
    when fanning out). Each peer gets its own subscription + handler so HA's
    answer/candidates route back to the right socket."""
    ha = await _ensure_ha_ws(state)
    if not ha:
        log.warning("HA WS unavailable (HA_URL/HA_TOKEN unset)")
        return False
    if not state.active_stream or state.active_stream.get("session_id") != session_id:
        return False

    async def handler(event: dict) -> None:
        await _on_ha_webrtc_event(state, session_id, event, reply_ws=reply_ws)

    sub_id = await ha.subscribe(
        {"type": "camera/webrtc/offer", "entity_id": entity_id, "offer": offer_sdp},
        handler,
    )
    slot = _peer_slot(state, reply_ws)
    if slot is not None:
        slot["ha_sub_id"] = sub_id
    return True


async def _forward_kiosk_candidate(state: AppState, session_id: str, payload: dict, *, reply_ws=None) -> None:
    """Forward a peer's ICE candidate to HA for the active session.

    Uses HA's session_id (captured from that peer's first 'session' event), not
    our internal session_id — HA only knows the id it issued. `reply_ws`
    identifies the peer (kiosk by default; a satellite when fanning out).
    """
    if not state.active_stream or state.active_stream.get("session_id") != session_id:
        return
    if not isinstance(state.ha_ws, HAWSClient):
        return
    slot = _peer_slot(state, reply_ws, create=False)
    ha_session_id = slot.get("ha_session_id") if slot is not None else None
    if not ha_session_id:
        # HA hasn't emitted its session event yet — drop the candidate; the
        # peer connection will retry/regenerate as needed.
        log.debug("peer candidate dropped: no HA session_id yet")
        return
    try:
        await state.ha_ws.send_command(
            {
                "type": "camera/webrtc/candidate",
                "session_id": ha_session_id,
                "candidate": {
                    "candidate": payload.get("candidate", ""),
                    "sdpMid": payload.get("sdpMid"),
                    "sdpMLineIndex": payload.get("sdpMLineIndex"),
                },
            },
            timeout=2.0,
        )
    except Exception as e:
        log.debug(f"forward kiosk candidate failed: {e}")
