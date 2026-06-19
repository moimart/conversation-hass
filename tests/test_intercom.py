"""Intercom signaling relay: the server is a dumb authenticated router between two
paired devices. The relay logic + addressing + session lifecycle are pure/
network-free (the only side effect is send_to_device, which we capture), so we
pin them here. Mirrors tests/test_weather.py's monkeypatch-the-send style."""

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from server.app import intercom


def pid(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


class _FakePairing:
    def __init__(self, tokens: dict):
        self._tokens = tokens  # token -> {device_name, scope}

    @staticmethod
    def public_id(token):
        return hashlib.sha256(token.encode()).hexdigest()[:16] if token else None

    def token_for_public_id(self, p):
        for t in self._tokens:
            if self.public_id(t) == p:
                return t
        return None

    def device_name(self, token):
        e = self._tokens.get(token)
        return e.get("device_name") if e else None


def _state(online=("A", "B"), *, audio=False, sessions=None):
    pairing = _FakePairing({
        "A": {"device_name": "Alice phone", "scope": "full"},
        "B": {"device_name": "Bob phone", "scope": "full"},
        "C": {"device_name": "Carol watch", "scope": "watch"},
    })
    return SimpleNamespace(
        pairing=pairing,
        satellite_ws={t: object() for t in online},
        intercom_sessions=dict(sessions or {}),
        audio_websocket=(object() if audio else None),
        runtime_config=SimpleNamespace(get=lambda k, d=None: d),
    )


@pytest.fixture
def sent(monkeypatch):
    """Capture every send_to_device call; report delivered iff the target is a
    connected satellite (mirrors the real send_to_device contract)."""
    out = []

    async def fake_send(state, token, msg):
        out.append((token, msg))
        return token in state.satellite_ws

    monkeypatch.setattr("server.app.main.send_to_device", fake_send)
    return out


def _to(sent, token):
    return [m for (t, m) in sent if t == token]


# ---- directory / addressing ----------------------------------------------

def test_public_id_stable_and_not_token():
    assert intercom.KIOSK_ID == "kiosk"
    assert pid("A") != "A"
    assert pid("A") == pid("A")  # stable


def test_directory_excludes_caller_and_watch_includes_kiosk():
    state = _state(online=("A", "B"), audio=True)
    dirn = intercom.directory(state, exclude_token="A")
    ids = {d["id"] for d in dirn}
    assert pid("A") not in ids          # requester excluded
    assert pid("C") not in ids          # watch scope is not a call endpoint
    assert pid("B") in ids
    assert intercom.KIOSK_ID in ids
    bob = next(d for d in dirn if d["id"] == pid("B"))
    assert bob == {"id": pid("B"), "name": "Bob phone", "online": True, "can_video": True}
    kiosk = next(d for d in dirn if d["id"] == intercom.KIOSK_ID)
    assert kiosk["online"] is True and kiosk["can_video"] is False


# ---- invite ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_invite_online_callee_rings_both(sent):
    state = _state(online=("A", "B"))
    await intercom.handle_signal(state, "A", {
        "type": "intercom_invite", "to": pid("B"), "session_id": "s1",
        "media": {"audio": True, "video": True},
    })
    inv = _to(sent, "B")
    assert len(inv) == 1
    assert inv[0]["type"] == "intercom_invite"
    assert inv[0]["from"] == pid("A") and inv[0]["from_name"] == "Alice phone"
    assert inv[0]["session_id"] == "s1" and inv[0]["ice_servers"]
    assert _to(sent, "A")[0]["type"] == "intercom_ringing"
    assert state.intercom_sessions["s1"]["caller"] == "A"
    assert state.intercom_sessions["s1"]["callee"] == "B"


# ---- incoming-call spoken announcement (mobile callee only) ----------------

class _FakeTTS:
    def __init__(self, audio=b"WAVDATA", fail=False):
        self._audio, self._fail, self.calls = audio, fail, []

    async def synthesize(self, text):
        self.calls.append(text)
        if self._fail:
            raise RuntimeError("boom")
        return self._audio


async def _drain():
    # Let the fire-and-forget announce task run to completion.
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.fixture
def cached(monkeypatch):
    """Capture cache_satellite_tts calls; return a fixed seq (the helper lazy-
    imports it from server.app.main)."""
    out = []

    def fake_cache(state, token, audio, mime):
        out.append((token, audio, mime))
        return 7

    monkeypatch.setattr("server.app.main.cache_satellite_tts", fake_cache)
    return out


@pytest.mark.asyncio
async def test_invite_satellite_callee_gets_spoken_announcement(sent, cached):
    state = _state(online=("A", "B"))
    state.tts_engine = _FakeTTS(b"WAVDATA")
    await intercom.handle_signal(state, "A", {
        "type": "intercom_invite", "to": pid("B"), "session_id": "s1",
        "media": {"audio": True, "video": True},
    })
    await _drain()
    plays = [m for m in _to(sent, "B") if m["type"] == "tts_play"]
    assert len(plays) == 1
    assert plays[0]["url"] == "/api/satellite/tts" and plays[0]["seq"] == 7
    assert plays[0]["mime"] == "audio/wav"
    assert cached == [("B", b"WAVDATA", "audio/wav")]
    assert state.tts_engine.calls == ["Incoming call from Alice phone"]


@pytest.mark.asyncio
async def test_invite_hub_callee_is_not_announced(sent, cached, monkeypatch):
    # The hub auto-answers (no ring) — it must never get a spoken cue. Its
    # delivery goes over the RPi bridge (_push_to_rpi), not send_to_device.
    async def fake_push(state, msg):
        return True

    monkeypatch.setattr("server.app.main._push_to_rpi", fake_push)
    state = _state(online=("A",), audio=True)
    state.tts_engine = _FakeTTS(b"WAVDATA")
    await intercom.handle_signal(state, "A", {
        "type": "intercom_invite", "to": intercom.KIOSK_ID, "session_id": "s1",
        "media": {"audio": True, "video": True},
    })
    await _drain()
    assert not any(m["type"] == "tts_play" for (_t, m) in sent)
    assert state.tts_engine.calls == []
    assert cached == []


@pytest.mark.asyncio
async def test_invite_announcement_failure_does_not_break_call(sent, cached):
    state = _state(online=("A", "B"))
    state.tts_engine = _FakeTTS(fail=True)   # synth raises
    await intercom.handle_signal(state, "A", {
        "type": "intercom_invite", "to": pid("B"), "session_id": "s1",
        "media": {"audio": True, "video": True},
    })
    await _drain()
    # No spoken cue, but the call still rang both parties.
    assert not any(m["type"] == "tts_play" for (_t, m) in sent)
    assert _to(sent, "A")[0]["type"] == "intercom_ringing"
    assert _to(sent, "B")[0]["type"] == "intercom_invite"
    assert cached == []


@pytest.mark.asyncio
async def test_invite_without_tts_engine_is_silent_no_crash(sent, cached):
    state = _state(online=("A", "B"))   # no state.tts_engine attribute
    await intercom.handle_signal(state, "A", {
        "type": "intercom_invite", "to": pid("B"), "session_id": "s1",
        "media": {"audio": True, "video": True},
    })
    await _drain()
    assert not any(m["type"] == "tts_play" for (_t, m) in sent)
    assert cached == []


@pytest.mark.asyncio
async def test_invite_announcement_uses_configurable_template(sent, cached):
    state = _state(online=("A", "B"))
    state.tts_engine = _FakeTTS(b"WAVDATA")
    state.intercom_announce_template = "{name} is calling you"
    await intercom.handle_signal(state, "A", {
        "type": "intercom_invite", "to": pid("B"), "session_id": "s1",
        "media": {"audio": True, "video": True},
    })
    await _drain()
    assert state.tts_engine.calls == ["Alice phone is calling you"]
    assert any(m["type"] == "tts_play" for m in _to(sent, "B"))


@pytest.mark.asyncio
async def test_invite_announcement_bad_template_falls_back(sent, cached):
    state = _state(online=("A", "B"))
    state.tts_engine = _FakeTTS(b"WAVDATA")
    state.intercom_announce_template = "Call from {unknown}"   # invalid placeholder
    await intercom.handle_signal(state, "A", {
        "type": "intercom_invite", "to": pid("B"), "session_id": "s1",
        "media": {"audio": True, "video": True},
    })
    await _drain()
    assert state.tts_engine.calls == ["Incoming call from Alice phone"]


@pytest.mark.asyncio
async def test_invite_unknown_device_is_unavailable(sent):
    state = _state(online=("A",))
    await intercom.handle_signal(state, "A", {
        "type": "intercom_invite", "to": "deadbeefdeadbeef", "session_id": "s1",
    })
    assert _to(sent, "A")[0]["type"] == "intercom_unavailable"
    assert "s1" not in state.intercom_sessions


@pytest.mark.asyncio
async def test_invite_offline_callee_is_unavailable(sent):
    state = _state(online=("A",))   # B paired but not connected
    await intercom.handle_signal(state, "A", {
        "type": "intercom_invite", "to": pid("B"), "session_id": "s1",
    })
    assert _to(sent, "A")[-1]["type"] == "intercom_unavailable"
    assert "s1" not in state.intercom_sessions   # session rolled back


@pytest.mark.asyncio
async def test_invite_self_is_unavailable(sent):
    state = _state(online=("A",))
    await intercom.handle_signal(state, "A", {
        "type": "intercom_invite", "to": pid("A"), "session_id": "s1",
    })
    assert _to(sent, "A")[0]["type"] == "intercom_unavailable"


@pytest.mark.asyncio
async def test_invite_busy_callee(sent):
    # B already in a call with someone → A's invite to B is busy.
    state = _state(online=("A", "B"), sessions={
        "existing": {"caller": "B", "callee": "X", "state": "active", "started": 0, "media": {}},
    })
    await intercom.handle_signal(state, "A", {
        "type": "intercom_invite", "to": pid("B"), "session_id": "s2",
    })
    assert _to(sent, "A")[0]["type"] == "intercom_busy"
    assert "s2" not in state.intercom_sessions


# ---- in-call signaling relay ---------------------------------------------

def _active(state):
    state.intercom_sessions["s1"] = {
        "caller": "A", "callee": "B", "state": "active", "started": 0, "media": {},
    }


@pytest.mark.asyncio
async def test_offer_relays_to_peer_only(sent):
    state = _state(online=("A", "B"))
    _active(state)
    await intercom.handle_signal(state, "A", {
        "type": "intercom_offer", "session_id": "s1", "sdp": "OFFER",
    })
    assert _to(sent, "B") == [{"type": "intercom_offer", "session_id": "s1", "sdp": "OFFER"}]
    assert _to(sent, "A") == []   # never echoed to sender


@pytest.mark.asyncio
async def test_candidate_relays_to_peer(sent):
    state = _state(online=("A", "B"))
    _active(state)
    await intercom.handle_signal(state, "B", {
        "type": "intercom_candidate", "session_id": "s1",
        "candidate": "cand", "sdpMid": "0", "sdpMLineIndex": 0,
    })
    got = _to(sent, "A")[0]
    assert got["type"] == "intercom_candidate" and got["candidate"] == "cand"


@pytest.mark.asyncio
async def test_non_participant_signaling_dropped(sent):
    state = _state(online=("A", "B", "C"))
    _active(state)
    await intercom.handle_signal(state, "C", {   # C isn't in s1
        "type": "intercom_offer", "session_id": "s1", "sdp": "x",
    })
    assert sent == []


@pytest.mark.asyncio
async def test_accept_marks_active_and_notifies_caller(sent):
    state = _state(online=("A", "B"))
    state.intercom_sessions["s1"] = {
        "caller": "A", "callee": "B", "state": "ringing", "started": 0, "media": {},
    }
    await intercom.handle_signal(state, "B", {"type": "intercom_accept", "session_id": "s1"})
    assert _to(sent, "A") == [{"type": "intercom_accept", "session_id": "s1"}]
    assert state.intercom_sessions["s1"]["state"] == "active"


@pytest.mark.asyncio
async def test_hangup_tears_down_and_notifies_peer(sent):
    state = _state(online=("A", "B"))
    _active(state)
    await intercom.handle_signal(state, "A", {"type": "intercom_hangup", "session_id": "s1"})
    assert _to(sent, "B")[0]["type"] == "intercom_hangup"
    assert "s1" not in state.intercom_sessions


@pytest.mark.asyncio
async def test_decline_tears_down(sent):
    state = _state(online=("A", "B"))
    state.intercom_sessions["s1"] = {
        "caller": "A", "callee": "B", "state": "ringing", "started": 0, "media": {},
    }
    await intercom.handle_signal(state, "B", {"type": "intercom_decline", "session_id": "s1"})
    assert _to(sent, "A")[0]["type"] == "intercom_decline"
    assert "s1" not in state.intercom_sessions


@pytest.mark.asyncio
async def test_disconnect_ends_call_and_hangs_up_peer(sent):
    state = _state(online=("A", "B"))
    _active(state)
    await intercom.end_sessions_for_token(state, "A", reason="disconnect")
    hg = _to(sent, "B")[0]
    assert hg["type"] == "intercom_hangup" and hg["reason"] == "disconnect"
    assert "s1" not in state.intercom_sessions


def test_resolve_target():
    state = _state(online=("A", "B"))
    # exact name (case-insensitive), excluding the caller
    assert intercom.resolve_target(state, "Bob phone", exclude_token="A")[0] == pid("B")
    assert intercom.resolve_target(state, "bob phone", exclude_token="A")[0] == pid("B")
    # "the " prefix + substring
    assert intercom.resolve_target(state, "the bob", exclude_token="A")[0] == pid("B")
    # kiosk by generic word + by name
    assert intercom.resolve_target(state, "kiosk", exclude_token="A")[0] == intercom.KIOSK_ID
    # caller excluded, watch excluded → unknown
    assert intercom.resolve_target(state, "Alice phone", exclude_token="A") is None
    assert intercom.resolve_target(state, "Carol watch", exclude_token="A") is None
    # online flag flows through
    assert intercom.resolve_target(state, "Bob phone", exclude_token="A")[2] is True
    assert intercom.resolve_target(state, "nobody", exclude_token="A") is None


def test_resolve_target_prefers_online_among_duplicates():
    # Two devices share the generic name "iPhone"; only one is connected.
    pairing = _FakePairing({
        "P": {"device_name": "Pixel", "scope": "full"},
        "old": {"device_name": "iPhone", "scope": "full"},   # offline (stale)
        "air": {"device_name": "iPhone", "scope": "full"},   # online
    })
    state = SimpleNamespace(
        pairing=pairing, satellite_ws={"P": object(), "air": object()},
        intercom_sessions={}, audio_websocket=None,
        runtime_config=SimpleNamespace(get=lambda k, d=None: d),
    )
    got = intercom.resolve_target(state, "iPhone", exclude_token="P")
    assert got[0] == pid("air") and got[2] is True   # the connected one wins


@pytest.mark.asyncio
async def test_signaling_for_unknown_session_is_noop(sent):
    state = _state(online=("A", "B"))
    await intercom.handle_signal(state, "A", {
        "type": "intercom_offer", "session_id": "nope", "sdp": "x",
    })
    assert sent == []


@pytest.mark.asyncio
async def test_cloudflare_mint_sends_browser_user_agent(monkeypatch):
    """Cloudflare's edge bot-protection 403s (error 1010) the httpx/urllib
    default User-Agent, so the TURN mint MUST present a browser-like UA — else
    it silently never mints and remote calls fall back to STUN-only."""
    import httpx

    captured = {}

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"iceServers": {"urls": ["turn:turn.cloudflare.com:3478"],
                                   "username": "u", "credential": "c"}}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured["headers"] = headers or {}
            return _FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    # Bypass the mint cache so the request actually fires.
    intercom._turn_cache.update(minted=None, minted_exp=0.0)

    minted = await intercom._cloudflare_ice({"key_id": "k", "api_token": "t"})
    assert minted  # non-empty -> mint succeeded
    ua = captured["headers"].get("User-Agent", "")
    assert ua and "python" not in ua.lower()
