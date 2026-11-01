"""Per-device TTS voice: each paired device can pick its own Wyoming voice
(stored on its pairing entry; "" = follow the hub voice). Its own turns, the
announcements it hears and its incoming-call ring use that voice."""
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from server.app.main import device_voice, speak_to_satellites
from server.app.tts import TTSEngine
from tests.test_tts import FakeReader, FakeWriter, _make_wyoming_response


# --- TTSEngine voice override ----------------------------------------------

def _sent_voice(writer: FakeWriter):
    header, rest = bytes(writer.data).split(b"\n", 1)
    n = json.loads(header)["data_length"]
    return json.loads(rest[:n]).get("voice")


@pytest.mark.asyncio
@pytest.mark.parametrize("override,expected", [
    (None, {"name": "Hub"}),
    ("", {"name": "Hub"}),
    ("Kid", {"name": "Kid"}),
])
async def test_synthesize_voice_override(override, expected):
    writer = FakeWriter()
    engine = TTSEngine(voice="Hub")
    with patch("asyncio.open_connection",
               return_value=(FakeReader(_make_wyoming_response()), writer)):
        assert await engine.synthesize("hi", voice=override)
    assert _sent_voice(writer) == expected
    assert engine.voice == "Hub"   # the hub voice is untouched


# --- pairing store -----------------------------------------------------------

@pytest.fixture
def pmod(monkeypatch, tmp_path):
    monkeypatch.setenv("PAIRING_TOKENS_FILE", str(tmp_path / "tokens.json"))
    import server.app.pairing as p
    return p


def test_pairing_voice_set_clear_persist(pmod):
    mgr = pmod.PairingManager()
    code, _ = mgr.create_code()
    tok = mgr.redeem(code, "Kitchen")
    assert mgr.voice(tok) == ""
    assert mgr.set_voice(tok, " Kid ")
    assert pmod.PairingManager().voice(tok) == "Kid"      # persisted
    assert mgr.set_voice(tok, "")
    assert pmod.PairingManager().voice(tok) == ""
    assert mgr.set_voice("nope", "Kid") is False
    assert mgr.voice(None) == ""


def test_watch_scope_cannot_pick_voice(pmod):
    mgr = pmod.PairingManager()
    code, _ = mgr.create_code()
    full = mgr.redeem(code, "Phone")
    watch = mgr.derive(full, "watch", "Watch")
    assert mgr.scope_allows(full, "voice")
    assert not mgr.scope_allows(watch, "voice")


# --- device_voice ------------------------------------------------------------

def _st(voices=None, options=("Hub", "Kid", "Gran"), hub="Hub"):
    voices = dict(voices or {})
    return SimpleNamespace(
        pairing=SimpleNamespace(voice=lambda t: voices.get(t, "")),
        voice_options=list(options),
        tts_engine=SimpleNamespace(voice=hub, synthesize=AsyncMock()),
        ui_clients=set(), satellite_ws={}, satellite_ws_tokens={},
        satellite_tts={}, satellite_photo_sessions={},
    )


def test_device_voice_resolution():
    st = _st({"a": "Kid", "b": "Gone", "c": "Hub"})
    assert device_voice(st, "a") == "Kid"
    assert device_voice(st, "b") is None      # no longer offered → hub
    assert device_voice(st, "c") is None      # same as hub → reuse hub audio
    assert device_voice(st, "x") is None      # follows hub
    assert device_voice(st, None) is None
    st.voice_options = []                     # voice list unknown → trust it
    assert device_voice(st, "b") == "Gone"


# --- speak_to_satellites -----------------------------------------------------

class _WS:
    def __init__(self):
        self.send_text = AsyncMock()
        self.send_json = AsyncMock()


def _connect(st, *tokens):
    for t in tokens:
        ws = _WS()
        st.satellite_ws[t] = ws
        st.satellite_ws_tokens[ws] = t


@pytest.mark.asyncio
async def test_speak_resynthesizes_once_per_device_voice():
    st = _st({"k1": "Kid", "k2": "Kid", "g": "Gran"})
    _connect(st, "hub1", "k1", "k2", "g")
    st.tts_engine.synthesize = AsyncMock(side_effect=lambda text, voice=None: f"WAV-{voice}".encode())
    n = await speak_to_satellites(st, "Dinner", b"WAV-hub")
    assert n == 4
    assert st.satellite_tts["hub1"]["audio"] == b"WAV-hub"
    assert st.satellite_tts["k1"]["audio"] == b"WAV-Kid"
    assert st.satellite_tts["k2"]["audio"] == b"WAV-Kid"
    assert st.satellite_tts["g"]["audio"] == b"WAV-Gran"
    voices = sorted(c.kwargs["voice"] for c in st.tts_engine.synthesize.await_args_list)
    assert voices == ["Gran", "Kid"]


@pytest.mark.asyncio
async def test_speak_device_voice_failure_falls_back_to_hub_audio():
    st = _st({"k": "Kid"})
    _connect(st, "k")
    st.tts_engine.synthesize = AsyncMock(return_value=None)
    assert await speak_to_satellites(st, "Hi", b"WAV-hub") == 1
    assert st.satellite_tts["k"]["audio"] == b"WAV-hub"


@pytest.mark.asyncio
async def test_speak_text_only_stays_text_only():
    st = _st({"k": "Kid"})
    _connect(st, "k")
    assert await speak_to_satellites(st, "Hi", None) == 0
    st.tts_engine.synthesize.assert_not_awaited()
    assert "k" not in st.satellite_tts


@pytest.mark.asyncio
async def test_speak_alarm_reprepends_beeps(monkeypatch):
    st = _st({"k": "Kid"})
    _connect(st, "k")
    st.tts_engine.synthesize = AsyncMock(return_value=b"WAV-Kid")
    monkeypatch.setattr("server.app.alarm.prepend_alarm", lambda a: b"BEEP+" + a)
    await speak_to_satellites(st, "Timer done", b"BEEP+WAV-hub", alarm=True)
    assert st.satellite_tts["k"]["audio"] == b"BEEP+WAV-Kid"


# --- conversation turn voice -------------------------------------------------

def test_conversation_turn_voice():
    from server.app.conversation import ConversationManager
    conv = ConversationManager.__new__(ConversationManager)
    conv._turn_origin = None
    conv.voice_resolver = lambda t: {"tokA": "Kid"}.get(t)
    assert conv._turn_voice() is None          # kiosk turn → hub voice
    conv._turn_origin = "tokA"
    assert conv._turn_voice() == "Kid"
    conv._turn_origin = "tokB"
    assert conv._turn_voice() is None
    conv.voice_resolver = None
    assert conv._turn_voice() is None


# --- GET/POST /api/satellite/voice --------------------------------------------

def _req(token=None, body=None):
    headers = {"authorization": f"Bearer {token}"} if token else {}

    async def _json():
        if body is None:
            raise ValueError("no body")
        return body

    return SimpleNamespace(app=SimpleNamespace(), headers=headers, query_params={}, json=_json)


@pytest.fixture
def fake_main(monkeypatch):
    voices = {}
    st = SimpleNamespace(
        pairing=SimpleNamespace(
            is_valid_token=lambda t: t in ("full", "watch"),
            scope_allows=lambda t, perm: t == "full" and perm == "voice",
            voice=lambda t: voices.get(t, ""),
            set_voice=lambda t, v: voices.__setitem__(t, v) or True,
        ),
        tts_engine=SimpleNamespace(voice="Hub"),
        voice_options=["Hub", "Kid"],
    )
    monkeypatch.setitem(sys.modules, "server.app.main", SimpleNamespace(_get_state=lambda app: st))
    return st


@pytest.mark.asyncio
async def test_voice_routes(fake_main):
    from server.app.routes_http import get_satellite_voice, set_satellite_voice
    assert await get_satellite_voice(_req("full")) == {
        "voice": "", "hub_voice": "Hub", "voices": ["Hub", "Kid"]}
    assert await set_satellite_voice(_req("full", {"voice": "Kid"})) == {"voice": "Kid"}
    assert (await get_satellite_voice(_req("full")))["voice"] == "Kid"
    assert await set_satellite_voice(_req("full", {"voice": ""})) == {"voice": ""}


@pytest.mark.asyncio
async def test_voice_routes_reject(fake_main):
    from server.app.routes_http import get_satellite_voice, set_satellite_voice
    assert (await get_satellite_voice(_req())).status_code == 401
    assert (await get_satellite_voice(_req("bogus"))).status_code == 401
    assert (await get_satellite_voice(_req("watch"))).status_code == 403
    assert (await set_satellite_voice(_req("watch", {"voice": "Kid"}))).status_code == 403
    assert (await set_satellite_voice(_req("full", {"voice": "Nope"}))).status_code == 400
    assert (await set_satellite_voice(_req("full", {"voice": 3}))).status_code == 400
    assert (await set_satellite_voice(_req("full"))).status_code == 400
