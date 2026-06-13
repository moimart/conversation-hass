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

import base64
import hashlib
import hmac
import json
import logging
import os
import time

log = logging.getLogger("hal.intercom")

# The PAL display unit is the "hub" in the intercom (you "call the hub"). Its
# routing id stays the internal "kiosk" string; only the user-facing name +
# spoken word are "hub". Decoupled from HAL_DEVICE_NAME (which names the whole
# server for onboarding/mDNS and is left as-is).
KIOSK_ID = "kiosk"
HUB_NAME = "Hub"


# --- ICE configuration ------------------------------------------------------
# LAN calls connect on host candidates (STUN only). Remote/NAT calls need a TURN
# relay; its credentials live in a gitignored, hot-reloaded runtime file
# (runtime/intercom_turn.json, chmod 600) — NEVER in the repo. Three providers:
#   {"provider":"cloudflare","key_id":"…","api_token":"…","ttl":86400}
#       → mint short-lived creds from Cloudflare's TURN API (no port-forward;
#         matches the existing Cloudflare-tunnel setup). Cached until ~expiry.
#   {"provider":"coturn","urls":["turn:host:3478"],"secret":"…","ttl":86400}
#       → self-hosted coturn with the TURN REST shared-secret (HMAC) scheme.
#   {"provider":"static","ice_servers":[…]}  → use the list verbatim.
# The minted servers are handed to BOTH peers in the invite/ringing payload.

_DEFAULT_STUN = [{"urls": ["stun:stun.l.google.com:19302"]}]
_TURN_CONFIG_PATH = os.environ.get("INTERCOM_TURN_CONFIG", "runtime/intercom_turn.json")
_turn_cache: dict = {"mtime": 0.0, "cfg": None, "minted": None, "minted_exp": 0.0}


def _load_turn_cfg():
    """Hot-reload the TURN config by mtime; None if absent/unreadable."""
    try:
        st = os.stat(_TURN_CONFIG_PATH)
    except OSError:
        _turn_cache.update(cfg=None, mtime=0.0)
        return None
    if st.st_mtime != _turn_cache["mtime"]:
        try:
            with open(_TURN_CONFIG_PATH) as f:
                _turn_cache.update(cfg=json.load(f), mtime=st.st_mtime, minted=None)
        except Exception as e:
            log.warning(f"intercom TURN config unreadable: {e}")
            _turn_cache.update(cfg=None)
    return _turn_cache["cfg"]


def _coturn_ice(cfg) -> list[dict]:
    urls = cfg.get("urls") or []
    secret = cfg.get("secret") or ""
    ttl = int(cfg.get("ttl", 86400))
    if not urls or not secret:
        return []
    username = str(int(time.time()) + ttl)   # TURN REST: username = expiry ts
    cred = base64.b64encode(
        hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()).decode()
    return [{"urls": urls, "username": username, "credential": cred}]


async def _cloudflare_ice(cfg):
    now = time.time()
    if _turn_cache["minted"] and now < _turn_cache["minted_exp"] - 60:
        return _turn_cache["minted"]
    key_id, token = cfg.get("key_id"), cfg.get("api_token")
    ttl = int(cfg.get("ttl", 86400))
    if not key_id or not token:
        return None
    import httpx
    url = f"https://rtc.live.cloudflare.com/v1/turn/keys/{key_id}/credentials/generate"
    # Cloudflare's edge bot-protection 403s (error 1010) requests whose
    # User-Agent looks like a script (the httpx/urllib defaults), so the mint
    # must present a browser-like UA — without it TURN silently never mints and
    # remote calls fall back to STUN-only (no relay → no media).
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "Mozilla/5.0"}
    async with httpx.AsyncClient(timeout=8) as client:
        r = await client.post(url, headers=headers, json={"ttl": ttl})
        r.raise_for_status()
        ice = r.json().get("iceServers")
    minted = [ice] if isinstance(ice, dict) else (ice if isinstance(ice, list) else None)
    if minted:
        _turn_cache.update(minted=minted, minted_exp=now + ttl)
    return minted


async def ice_servers(state) -> list[dict]:
    """ICE servers handed to both peers (always STUN; + TURN if configured)."""
    rc = getattr(state, "runtime_config", None)
    if rc is not None:
        override = rc.get("intercom_ice_servers", None)
        if isinstance(override, list) and override:
            return override
    servers = list(_DEFAULT_STUN)
    cfg = _load_turn_cfg()
    if not cfg:
        return servers
    provider = cfg.get("provider")
    try:
        if provider == "static":
            ice = cfg.get("ice_servers")
            return ice if isinstance(ice, list) and ice else servers
        if provider == "coturn":
            servers.extend(_coturn_ice(cfg))
        elif provider == "cloudflare":
            minted = await _cloudflare_ice(cfg)
            if minted:
                servers.extend(minted)
    except Exception as e:
        log.warning(f"intercom TURN mint failed: {e}")
    return servers


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
        return HUB_NAME
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
    # The hub (PAL display unit) is always a callable endpoint when connected.
    out.append({
        "id": KIOSK_ID,
        "name": HUB_NAME,
        "online": getattr(state, "audio_websocket", None) is not None,
        "can_video": False,  # the hub has no camera
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
    if exclude_token != KIOSK_ID:
        candidates.append((KIOSK_ID, HUB_NAME,
                           getattr(state, "audio_websocket", None) is not None))

    # The hub answers to "hub" (and still to "kiosk" as a quiet alias).
    exact = [c for c in candidates if c[1].lower() == q
             or (c[0] == KIOSK_ID and q in ("hub", "kiosk"))]
    partial = [c for c in candidates if q in c[1].lower()]
    pool = exact or partial   # exact name wins over a substring match
    if not pool:
        return None
    # Prefer an ONLINE match — device names are often generic/duplicated
    # ("iPhone", "Android device"), so disambiguate toward the one that can
    # actually answer right now.
    online = [c for c in pool if c[2]]
    return (online or pool)[0]


# --- signaling relay --------------------------------------------------------

async def handle_signal(state, from_token: str, msg: dict) -> None:
    """Route one intercom message from a satellite (identified by from_token).
    Validates participation; never echoes back to the sender."""
    mtype = msg.get("type")
    _who = "kiosk" if from_token == KIOSK_ID else (str(from_token)[:6] if from_token else "?")
    log.info(f"[intercom] signal {mtype} from {_who} session={msg.get('session_id')}")
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

    ice = await ice_servers(state)
    invite = {
        "type": "intercom_invite",
        "from": state.pairing.public_id(caller) if state.pairing else None,
        "from_name": _device_label(state, caller),
        "session_id": session_id,
        "media": media,
        "ice_servers": ice,
    }
    _cw = "kiosk" if callee == KIOSK_ID else str(callee)[:6]
    delivered = await _send(state, callee, invite)
    log.info(f"[intercom] invite → {_cw} delivered={delivered} "
             f"(audio_ws={'up' if getattr(state, 'audio_websocket', None) else 'down'})")
    if delivered:
        await _send(state, caller, {
            "type": "intercom_ringing", "session_id": session_id,
            "ice_servers": ice,
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
    """Deliver to a participant: satellites over /ws/ui (send_to_device), the
    kiosk over the RPi audio_websocket bridge (_push_to_rpi → the audio_streamer
    forwards intercom_* to the kiosk Chromium)."""
    if token == KIOSK_ID:
        from .main import _push_to_rpi
        return bool(await _push_to_rpi(state, msg))
    from .main import send_to_device
    return await send_to_device(state, token, msg)
