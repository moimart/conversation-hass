"""1:1 intercom calls between paired PAL devices.

The server is a **dumb, authenticated signaling relay**: it routes call-control
and WebRTC signaling JSON between exactly two paired devices and NEVER touches the
media (audio/video flow peer-to-peer over WebRTC, relayed by a TURN server only
when NAT requires it). All it owns is a tiny session registry and the
address↔token translation.

Addressing: a device's public address is `pairing.public_id(token)` =
sha256(token)[:16] — stable, unique, non-reversible to the secret token, so it's
safe to hand to other clients in the directory. The kiosk is a reserved address
(`KIOSK_ID`) reached over the RPi audio_websocket bridge rather than a /ws/ui
satellite socket (wired in a later phase).

Message protocol (over the existing /ws/ui socket; server stamps `from`):
  client→server                         server→peer
  intercom_invite {to, session_id,      intercom_invite {from, from_name,
                   media:{audio,video}}                  session_id, media, ice_servers}
  intercom_accept {session_id}          intercom_accept {session_id}
  intercom_decline {session_id}         intercom_decline {session_id}
  intercom_offer  {session_id, sdp}     intercom_offer  {session_id, sdp}
  intercom_answer {session_id, sdp}     intercom_answer {session_id, sdp}
  intercom_candidate {session_id, ...}  intercom_candidate {session_id, ...}
  intercom_hangup {session_id}          intercom_hangup {session_id, reason}
The caller also gets back, in response to its invite: intercom_ringing /
intercom_busy / intercom_unavailable.
"""
from __future__ import annotations

import logging
import os
import time

log = logging.getLogger("hal.intercom")

KIOSK_ID = "kiosk"


# --- ICE configuration ------------------------------------------------------

_DEFAULT_STUN = [{"urls": ["stun:stun.l.google.com:19302"]}]


def ice_servers(state) -> list[dict]:
    """ICE servers handed to both peers. LAN calls connect on host candidates
    (STUN only); remote/NAT calls need a TURN relay, configured out-of-band (a
    later phase) via runtime_config 'intercom_ice_servers' (a JSON list) or the
    INTERCOM_ICE_SERVERS env var. Falls back to public STUN."""
    rc = getattr(state, "runtime_config", None)
    if rc is not None:
        configured = rc.get("intercom_ice_servers", None)
        if isinstance(configured, list) and configured:
            return configured
    env = os.environ.get("INTERCOM_ICE_SERVERS")
    if env:
        import json
        try:
            parsed = json.loads(env)
            if isinstance(parsed, list) and parsed:
                return parsed
        except Exception:
            pass
    return _DEFAULT_STUN


# --- session registry -------------------------------------------------------
# state.intercom_sessions: dict[session_id, {caller, callee, state, started, media}]
# caller/callee are SECRET tokens; KIOSK_ID stands in for the kiosk.

def _sessions(state) -> dict:
    return state.intercom_sessions


def _peer_token(session: dict, token: str) -> str | None:
    """The OTHER participant's token for a session, or None if `token` isn't in
    it (so a stranger can't inject signaling into someone else's call)."""
    if token == session.get("caller"):
        return session.get("callee")
    if token == session.get("callee"):
        return session.get("caller")
    return None


def _device_label(state, token: str) -> str:
    if token == KIOSK_ID:
        return os.environ.get("HAL_DEVICE_NAME", "Kiosk")
    name = state.pairing.device_name(token) if state.pairing else None
    return name or "Device"


# --- directory --------------------------------------------------------------

def directory(state, *, exclude_token: str | None = None) -> list[dict]:
    """Callable devices (excluding the requester): public id, name, online,
    can_video (advisory — SDP negotiates the truth). Never exposes a token."""
    out: list[dict] = []
    pairing = getattr(state, "pairing", None)
    connected = getattr(state, "satellite_ws", {}) or {}
    if pairing is not None:
        for token, entry in pairing._tokens.items():
            if token == exclude_token:
                continue
            scope = entry.get("scope", "full")
            if scope == "watch":
                continue  # watches aren't call endpoints (yet)
            out.append({
                "id": pairing.public_id(token),
                "name": entry.get("device_name") or "Device",
                "online": token in connected,
                "can_video": scope == "full",
            })
    # The kiosk is always a callable endpoint when its panel is connected.
    out.append({
        "id": KIOSK_ID,
        "name": os.environ.get("HAL_DEVICE_NAME", "Kiosk"),
        "online": getattr(state, "audio_websocket", None) is not None,
        "can_video": False,  # the kiosk has no camera
    })
    out.sort(key=lambda d: (not d["online"], d["name"].lower()))
    return out


def resolve_target(state, name: str, *, exclude_token: str | None = None):
    """Fuzzy-resolve a spoken target ("the kitchen", "Bob's phone", "kiosk") to a
    callable device. Returns (id, name, online) or None. Exact (case-insensitive)
    name wins over a substring match; the kiosk matches its name or "kiosk"."""
    q = (name or "").strip().lower()
    for prefix in ("the ", "my ", "a "):
        if q.startswith(prefix):
            q = q[len(prefix):]
    q = q.removesuffix("'s phone").removesuffix("s phone").strip()
    if not q:
        return None

    candidates = []  # (id, name, online)
    pairing = getattr(state, "pairing", None)
    connected = getattr(state, "satellite_ws", {}) or {}
    if pairing is not None:
        for token, entry in pairing._tokens.items():
            if token == exclude_token or entry.get("scope") == "watch":
                continue
            candidates.append((pairing.public_id(token),
                               entry.get("device_name") or "Device",
                               token in connected))
    kiosk_name = os.environ.get("HAL_DEVICE_NAME", "Kiosk")
    candidates.append((KIOSK_ID, kiosk_name,
                       getattr(state, "audio_websocket", None) is not None))

    # The kiosk also answers to the generic word "kiosk".
    exact = [c for c in candidates if c[1].lower() == q
             or (c[0] == KIOSK_ID and q == "kiosk")]
    if exact:
        return exact[0]
    partial = [c for c in candidates if q in c[1].lower()]
    return partial[0] if len(partial) == 1 else None


# --- signaling relay --------------------------------------------------------

async def handle_signal(state, from_token: str, msg: dict) -> None:
    """Route one intercom message from a satellite (identified by from_token).
    Validates participation; never echoes back to the sender."""
    mtype = msg.get("type")
    if mtype == "intercom_invite":
        await _on_invite(state, from_token, msg)
        return
    # All other messages operate on an existing session the sender is part of.
    session_id = str(msg.get("session_id", ""))
    session = _sessions(state).get(session_id)
    if not session:
        return
    peer = _peer_token(session, from_token)
    if peer is None:
        return  # sender isn't a participant — drop

    if mtype == "intercom_accept":
        session["state"] = "active"
        await _send(state, peer, {"type": "intercom_accept", "session_id": session_id})
    elif mtype == "intercom_decline":
        await _send(state, peer, {"type": "intercom_decline", "session_id": session_id})
        _end(state, session_id)
    elif mtype in ("intercom_offer", "intercom_answer"):
        await _send(state, peer, {
            "type": mtype, "session_id": session_id, "sdp": msg.get("sdp"),
        })
    elif mtype == "intercom_candidate":
        await _send(state, peer, {
            "type": "intercom_candidate", "session_id": session_id,
            "candidate": msg.get("candidate"),
            "sdpMid": msg.get("sdpMid"),
            "sdpMLineIndex": msg.get("sdpMLineIndex"),
        })
    elif mtype == "intercom_hangup":
        await _send(state, peer, {
            "type": "intercom_hangup", "session_id": session_id, "reason": "remote",
        })
        _end(state, session_id)


async def _on_invite(state, from_token: str, msg: dict) -> None:
    caller = from_token
    to_id = str(msg.get("to", ""))
    session_id = str(msg.get("session_id", ""))
    if not to_id or not session_id:
        return
    callee = KIOSK_ID if to_id == KIOSK_ID else (
        state.pairing.token_for_public_id(to_id) if state.pairing else None)
    if callee is None or callee == caller:
        await _send(state, caller, {"type": "intercom_unavailable", "session_id": session_id})
        return

    # Busy check: either party already in a call.
    for s in _sessions(state).values():
        if caller in (s["caller"], s["callee"]) or callee in (s["caller"], s["callee"]):
            await _send(state, caller, {"type": "intercom_busy", "session_id": session_id})
            return

    media = msg.get("media") or {"audio": True, "video": True}
    _sessions(state)[session_id] = {
        "caller": caller, "callee": callee, "state": "ringing",
        "started": time.monotonic(), "media": media,
    }

    invite = {
        "type": "intercom_invite",
        "from": state.pairing.public_id(caller) if state.pairing else None,
        "from_name": _device_label(state, caller),
        "session_id": session_id,
        "media": media,
        "ice_servers": ice_servers(state),
    }
    delivered = await _send(state, callee, invite)
    if delivered:
        await _send(state, caller, {
            "type": "intercom_ringing", "session_id": session_id,
            "ice_servers": ice_servers(state),
        })
    else:
        # Callee not connected. (Offline push to wake a closed app is wired in a
        # later phase via push.py; for now report unavailable and drop.)
        _end(state, session_id)
        await _send(state, caller, {"type": "intercom_unavailable", "session_id": session_id})


def _end(state, session_id: str) -> None:
    _sessions(state).pop(session_id, None)


async def end_sessions_for_token(state, token: str, *, reason: str = "disconnect") -> None:
    """Tear down any call involving `token` (e.g. it disconnected) and notify the
    peer with a hangup. Idempotent."""
    if not token:
        return
    for sid, s in list(_sessions(state).items()):
        if token in (s.get("caller"), s.get("callee")):
            peer = _peer_token(s, token)
            _end(state, sid)
            if peer is not None:
                await _send(state, peer, {
                    "type": "intercom_hangup", "session_id": sid, "reason": reason,
                })


async def _send(state, token: str, msg: dict) -> bool:
    """Deliver to a participant. Satellites go over /ws/ui (send_to_device); the
    kiosk path (audio_websocket bridge) is wired in a later phase."""
    if token == KIOSK_ID:
        # Phase 3: route to the kiosk via _push_to_rpi + the audio_streamer
        # forward allowlist. Not yet wired.
        log.debug("intercom: kiosk delivery not yet wired (%s)", msg.get("type"))
        return False
    from .main import send_to_device
    return await send_to_device(state, token, msg)
