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


async def _stop_active_stream(state: AppState, *, notify_kiosk: bool = True) -> None:
    """Tear down the active WebRTC session (if any)."""
    from .main import broadcast_to_ui
    session = state.active_stream
    if not session:
        return
    state.active_stream = None
    safety = session.get("safety_task")
    if safety:
        safety.cancel()
    # HA path
    sub_id = session.get("ha_sub_id")
    if sub_id is not None and isinstance(state.ha_ws, HAWSClient):
        await state.ha_ws.unsubscribe(sub_id)
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
    from .main import broadcast_to_ui
    msg = {"type": "video_stop"}
    ws = state.audio_websocket
    if ws:
        try:
            await ws.send_json(msg)
        except Exception:
            pass
    await broadcast_to_ui(state, msg)


async def _negotiate_rtsp_offer(state: AppState, session_id: str, offer_sdp: str) -> None:
    """Hand a kiosk-side SDP offer to go2rtc and forward the answer back."""
    if not state.active_stream or state.active_stream.get("session_id") != session_id:
        return
    if state.active_stream.get("kind") != "rtsp":
        return
    name = state.active_stream.get("go2rtc_name")
    if not name or not isinstance(state.go2rtc, Go2RTCClient):
        return
    answer_sdp = await state.go2rtc.webrtc_offer(name, offer_sdp)
    if not answer_sdp:
        log.warning(f"go2rtc returned no answer for {name!r}")
        await _stop_active_stream(state)
        return
    msg = {
        "type": "webrtc_signal",
        "session_id": session_id,
        "kind": "answer",
        "sdp": answer_sdp,
    }
    ws = state.audio_websocket
    if ws:
        try:
            await ws.send_json(msg)
        except Exception as e:
            log.debug(f"forward go2rtc answer to kiosk failed: {e}")


async def _on_ha_webrtc_event(state: AppState, session_id: str, event: dict) -> None:
    """Forward an HA WebRTC subscription event to the kiosk as a webrtc_signal.

    HA emits events:
      {type: "session", session_id: <HA's id>}    — first; capture for candidate routing
      {type: "answer",  answer: <sdp>}            — forward as 'answer'
      {type: "candidate", candidate: {...}}       — forward as 'candidate'
      {type: "error",   code, message}            — tear down the stream
    """
    msg_type = event.get("type")
    log.debug(f"HA WebRTC event for session {session_id}: type={msg_type}")
    out: dict | None = None
    if msg_type == "session":
        if state.active_stream and state.active_stream.get("session_id") == session_id:
            state.active_stream["ha_session_id"] = event.get("session_id")
            log.info(f"Captured HA session_id={state.active_stream['ha_session_id']} for {session_id}")
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
        log.warning(f"HA WebRTC error: {event}")
        await _stop_active_stream(state)
        return
    if not out:
        return
    ws = state.audio_websocket
    if ws:
        try:
            await ws.send_json(out)
        except Exception as e:
            log.debug(f"forward HA WebRTC event to kiosk failed: {e}")


async def _start_webrtc_stream(state: AppState, entity_id: str, session_id: str, offer_sdp: str) -> bool:
    """Begin the HA WebRTC offer subscription. Returns True on success."""
    ha = await _ensure_ha_ws(state)
    if not ha:
        log.warning("HA WS unavailable (HA_URL/HA_TOKEN unset)")
        return False

    async def handler(event: dict) -> None:
        await _on_ha_webrtc_event(state, session_id, event)

    sub_id = await ha.subscribe(
        {"type": "camera/webrtc/offer", "entity_id": entity_id, "offer": offer_sdp},
        handler,
    )
    if state.active_stream:
        state.active_stream["ha_sub_id"] = sub_id
    return True


async def _forward_kiosk_candidate(state: AppState, session_id: str, payload: dict) -> None:
    """Forward a kiosk-side ICE candidate to HA for the active session.

    Uses HA's session_id (captured from the first 'session' event), not
    our internal session_id — HA only knows the id it issued.
    """
    if not state.active_stream or state.active_stream.get("session_id") != session_id:
        return
    if not isinstance(state.ha_ws, HAWSClient):
        return
    ha_session_id = state.active_stream.get("ha_session_id")
    if not ha_session_id:
        # HA hasn't emitted its session event yet — drop the candidate; the
        # peer connection will retry/regenerate as needed.
        log.debug("kiosk candidate dropped: no HA session_id yet")
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
