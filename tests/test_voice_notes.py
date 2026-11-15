"""Voice notes: storage (mocked asyncpg), the upload route (auth, f32le/s16le,
streamed/chunked bodies, 413, 422, stored=false), long-audio windowing, the
list/delete routes, the note_added broadcast, and the local-tool/MCP surface."""
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

import server.app.conversation_log as clog_mod
from server.app import routes_notes
from server.app.conversation_log import ConversationLog


# --- fakes -------------------------------------------------------------------

class _FakeConn:
    def __init__(self, rows=None, fetchrow_result=None, fetchval_result=None, fail=False):
        self.execute = AsyncMock()
        self.fetch = AsyncMock(return_value=rows or [])
        self.fetchrow = AsyncMock(return_value=fetchrow_result)
        self.fetchval = AsyncMock(return_value=fetchval_result)
        if fail:
            for m in (self.fetch, self.fetchrow, self.fetchval):
                m.side_effect = RuntimeError("db down")


class _FakePool:
    def __init__(self, conn):
        self._conn = conn
        self.close = AsyncMock()

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _Ctx()


def _patched_pool(conn):
    fake = MagicMock()
    fake.create_pool = AsyncMock(return_value=_FakePool(conn))
    return patch.object(clog_mod, "asyncpg", fake)


def _nrow(id, text="buy milk", origin="iPhone", duration_s=3.5):
    return {"id": id, "ts": datetime(2026, 11, 15, 9, 0, id % 60, tzinfo=timezone.utc),
            "text": text, "origin": origin, "duration_s": duration_s}


# --- storage -------------------------------------------------------------------

def test_schema_creates_voice_notes_table_and_index():
    assert "CREATE TABLE IF NOT EXISTS voice_notes" in clog_mod._SCHEMA
    assert "duration_s REAL" in clog_mod._SCHEMA
    assert "voice_notes_id_desc ON voice_notes (id DESC)" in clog_mod._SCHEMA


@pytest.mark.asyncio
async def test_add_note_inserts_and_returns_row():
    conn = _FakeConn(fetchrow_result=_nrow(5))
    with _patched_pool(conn):
        cl = ConversationLog("postgresql://x")
        note = await cl.add_note("  buy milk ", origin="iPhone", duration_s=3.5)
    args = conn.fetchrow.await_args.args
    assert "INSERT INTO voice_notes" in args[0]
    assert args[1:] == ("buy milk", "iPhone", 3.5)
    assert note["id"] == 5 and note["text"] == "buy milk"
    assert note["ts"].startswith("2026-11-15T09:00")
    assert note["duration_s"] == 3.5


@pytest.mark.asyncio
async def test_add_note_empty_or_disabled_or_failing_returns_none():
    assert await ConversationLog("").add_note("hi") is None
    conn = _FakeConn(fail=True)
    with _patched_pool(conn):
        cl = ConversationLog("postgresql://x")
        assert await cl.add_note("") is None
        assert await cl.add_note("hello") is None      # never raises
        assert cl._pool is None                        # dropped for lazy reconnect


@pytest.mark.asyncio
async def test_fetch_notes_keyset_asc_and_has_more():
    conn = _FakeConn(rows=[_nrow(9), _nrow(8)])        # DESC from the DB
    with _patched_pool(conn):
        cl = ConversationLog("postgresql://x")
        page = await cl.fetch_notes(limit=2, before_id=10)
    sql, before, pattern, limit = conn.fetch.await_args.args
    assert "FROM voice_notes" in sql and "id < $1" in sql and "ILIKE" in sql
    assert (before, pattern, limit) == (10, None, 2)
    assert [r["id"] for r in page["rows"]] == [8, 9]    # ASC
    assert page["has_more"] is True


@pytest.mark.asyncio
async def test_fetch_notes_query_escapes_like_metachars():
    conn = _FakeConn(rows=[])
    with _patched_pool(conn):
        cl = ConversationLog("postgresql://x")
        page = await cl.fetch_notes(q="50%_off")
    assert conn.fetch.await_args.args[2] == "%50\\%\\_off%"
    assert page == {"rows": [], "has_more": False}


@pytest.mark.asyncio
async def test_fetch_notes_disabled():
    assert await ConversationLog("").fetch_notes() == {
        "rows": [], "has_more": False, "disabled": True}


@pytest.mark.asyncio
async def test_get_and_delete_note():
    conn = _FakeConn(fetchrow_result=_nrow(3), fetchval_result=3)
    with _patched_pool(conn):
        cl = ConversationLog("postgresql://x")
        assert (await cl.get_note(3))["id"] == 3
        assert await cl.delete_note(3) is True
        assert "DELETE FROM voice_notes" in conn.fetchval.await_args.args[0]
        conn.fetchval.return_value = None
        assert await cl.delete_note(4) is False
        conn.fetchrow.return_value = None
        assert await cl.get_note(99) is None


# --- windowing -----------------------------------------------------------------

def test_split_windows_short_audio_single_window():
    a = np.zeros(16000 * 5, dtype=np.float32)
    assert routes_notes.split_windows(a) == [(0, len(a))]
    assert routes_notes.split_windows(np.zeros(0, dtype=np.float32)) == []


def test_split_windows_cuts_at_quietest_frame_in_search_region():
    sr = 16000
    rng = np.random.default_rng(0)
    a = (rng.standard_normal(sr * 70) * 0.3).astype(np.float32)
    # A silent gap 26.0-26.2 s (inside the last 4 s of the first 28 s window).
    a[int(26.0 * sr):int(26.2 * sr)] = 0.0
    wins = routes_notes.split_windows(a, sr)
    # contiguous, complete coverage, each ≤ 28 s
    assert wins[0][0] == 0 and wins[-1][1] == len(a)
    for (s0, e0), (s1, _e1) in zip(wins, wins[1:]):
        assert e0 == s1
    assert all(e - s <= 28 * sr for s, e in wins)
    cut = wins[0][1]
    assert 26.0 * sr <= cut <= 26.2 * sr


def test_split_windows_ten_minutes():
    a = np.full(16000 * 600, 0.1, dtype=np.float32)
    wins = routes_notes.split_windows(a)
    assert len(wins) >= 22                          # ≥ 600/28
    assert all(e - s <= 28 * 16000 for s, e in wins)
    assert sum(e - s for s, e in wins) == len(a)


@pytest.mark.asyncio
async def test_transcribe_long_joins_windows_in_order_and_skips_silence():
    sr = 16000
    a = np.full(sr * 90, 0.2, dtype=np.float32)
    a[sr * 30:] = 0.0                              # 60 s of trailing digital silence
    calls = []

    async def fake(chunk):
        calls.append(len(chunk))
        assert len(chunk) <= 28 * sr
        return f"part{len(calls)}"

    text = await routes_notes.transcribe_long(SimpleNamespace(transcribe=fake), a)
    assert text == "part1 part2"                   # the silent windows were skipped
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_transcribe_long_survives_window_failure():
    sr = 16000
    a = np.full(sr * 40, 0.2, dtype=np.float32)
    tr = SimpleNamespace(transcribe=AsyncMock(side_effect=[RuntimeError("x"), "tail"]))
    assert await routes_notes.transcribe_long(tr, a) == "tail"


def test_decode_pcm_formats():
    s16 = np.array([0, 16384, -32768], dtype="<i2").tobytes() + b"\x01"   # stray byte
    out = routes_notes.decode_pcm(s16, "s16le")
    assert out.dtype == np.float32
    assert np.allclose(out, [0.0, 0.5, -1.0])
    f32 = np.array([0.25, -0.5], dtype="<f4").tobytes()
    assert np.allclose(routes_notes.decode_pcm(f32, "f32le"), [0.25, -0.5])


# --- upload route --------------------------------------------------------------

def _req(token="full_tok", body=b"", sr="16000", fmt=None, chunk=None, headers=None):
    h = {}
    if token:
        h["authorization"] = f"Bearer {token}"
    if sr is not None:
        h["x-sample-rate"] = sr
    if fmt is not None:
        h["x-sample-format"] = fmt
    h.update(headers or {})

    async def stream():
        step = chunk or max(1, len(body))
        for i in range(0, len(body), step):
            yield body[i:i + step]
        yield b""

    return SimpleNamespace(app=SimpleNamespace(), headers=h, query_params={}, stream=stream)


@pytest.fixture
def notes_state(monkeypatch):
    stored = []

    async def add_note(text, origin=None, duration_s=None):
        row = {"id": 11, "ts": "2026-11-15T09:00:00+00:00", "text": text,
               "origin": origin, "duration_s": duration_s}
        stored.append(row)
        return row

    st = SimpleNamespace(
        pairing=SimpleNamespace(
            is_valid_token=lambda t: t in ("full_tok", "watch_tok"),
            scope_allows=lambda t, perm: t == "full_tok",
            device_name=lambda t: "Kitchen Phone" if t == "full_tok" else "Watch",
        ),
        pipeline=SimpleNamespace(transcriber=SimpleNamespace(
            transcribe=AsyncMock(return_value="remember the milk"))),
        conversation_log=SimpleNamespace(enabled=True, add_note=AsyncMock(side_effect=add_note)),
        stored=stored,
    )
    fake_main = SimpleNamespace(
        _get_state=lambda app: st,
        _push_to_rpi=AsyncMock(return_value=True),
        broadcast_force_action=AsyncMock(),
    )
    monkeypatch.setitem(sys.modules, "server.app.main", fake_main)
    monkeypatch.delenv("HAL_DEMO_MODE", raising=False)
    monkeypatch.delenv("HAL_REQUIRE_TOKEN", raising=False)
    st.main = fake_main
    return st


def _status(resp):
    return getattr(resp, "status_code", 200)


def _json(resp):
    import json
    if isinstance(resp, dict):
        return resp
    return json.loads(resp.body)


@pytest.mark.asyncio
async def test_note_requires_paired_token_and_scope(notes_state):
    f32 = np.full(16000, 0.1, dtype=np.float32).tobytes()
    assert _status(await routes_notes.post_satellite_note(_req(None, f32))) == 401
    assert _status(await routes_notes.post_satellite_note(_req("bogus", f32))) == 401
    assert _status(await routes_notes.post_satellite_note(_req("watch_tok", f32))) == 403
    notes_state.pipeline.transcriber.transcribe.assert_not_awaited()


@pytest.mark.asyncio
async def test_note_f32le_default_stores_and_broadcasts(notes_state):
    body = np.full(32000, 0.1, dtype=np.float32).tobytes()
    resp = await routes_notes.post_satellite_note(_req(body=body, chunk=1000))
    assert _status(resp) == 200
    out = _json(resp)
    assert out["id"] == 11 and out["stored"] is True
    assert out["text"] == "remember the milk"
    assert out["origin"] == "Kitchen Phone"
    assert out["duration_s"] == 2.0
    arr = notes_state.pipeline.transcriber.transcribe.await_args.args[0]
    assert arr.dtype == np.float32 and len(arr) == 32000
    msg = notes_state.main.broadcast_force_action.await_args.args[1]
    assert msg["type"] == "note_added" and msg["note"]["id"] == 11
    assert notes_state.main._push_to_rpi.await_args.args[1]["type"] == "note_added"


@pytest.mark.asyncio
async def test_note_s16le_streamed_in_small_chunks(notes_state):
    pcm = (np.full(16000 * 3, 0.25) * 32767).astype("<i2").tobytes()
    resp = await routes_notes.post_satellite_note(
        _req(body=pcm, fmt="s16le", chunk=777))
    assert _json(resp)["duration_s"] == 3.0
    arr = notes_state.pipeline.transcriber.transcribe.await_args.args[0]
    assert len(arr) == 48000 and abs(float(arr[0]) - 0.25) < 1e-3


@pytest.mark.asyncio
async def test_note_resamples_to_16k(notes_state, monkeypatch):
    fake_librosa = SimpleNamespace(
        resample=MagicMock(side_effect=lambda a, orig_sr, target_sr:
                           np.zeros(int(len(a) * target_sr / orig_sr), dtype=np.float32) + 0.1))
    monkeypatch.setitem(sys.modules, "librosa", fake_librosa)
    body = np.full(48000, 0.1, dtype=np.float32).tobytes()
    resp = await routes_notes.post_satellite_note(_req(body=body, sr="48000"))
    assert _json(resp)["duration_s"] == 1.0
    kw = fake_librosa.resample.call_args.kwargs
    assert kw == {"orig_sr": 48000, "target_sr": 16000}


@pytest.mark.asyncio
async def test_note_bad_headers_400(notes_state):
    body = np.full(1600, 0.1, dtype=np.float32).tobytes()
    assert _status(await routes_notes.post_satellite_note(_req(body=body, sr="4000"))) == 400
    assert _status(await routes_notes.post_satellite_note(_req(body=body, sr="abc"))) == 400
    assert _status(await routes_notes.post_satellite_note(_req(body=body, fmt="mp3"))) == 400
    assert _status(await routes_notes.post_satellite_note(_req(body=b""))) == 400


@pytest.mark.asyncio
async def test_note_too_large_413(notes_state, monkeypatch):
    monkeypatch.setattr(routes_notes, "MAX_BODY_BYTES", 1000)
    # streamed (no Content-Length) past the cap
    resp = await routes_notes.post_satellite_note(_req(body=b"\0" * 2000, chunk=300))
    assert _status(resp) == 413
    # declared Content-Length past the cap → rejected before reading
    resp = await routes_notes.post_satellite_note(
        _req(body=b"\0" * 10, headers={"content-length": "5000"}))
    assert _status(resp) == 413
    notes_state.pipeline.transcriber.transcribe.assert_not_awaited()


@pytest.mark.asyncio
async def test_note_too_long_413(notes_state, monkeypatch):
    monkeypatch.setattr(routes_notes, "MAX_DURATION_S", 2.0)
    pcm = np.zeros(16000 * 3, dtype="<i2").tobytes()
    resp = await routes_notes.post_satellite_note(_req(body=pcm, fmt="s16le"))
    assert _status(resp) == 413 and _json(resp)["error"] == "too_long"


@pytest.mark.asyncio
async def test_note_no_speech_422(notes_state):
    notes_state.pipeline.transcriber.transcribe.return_value = "  "
    body = np.full(16000, 0.1, dtype=np.float32).tobytes()
    resp = await routes_notes.post_satellite_note(_req(body=body))
    assert _status(resp) == 422 and _json(resp) == {"error": "no_speech"}
    assert notes_state.stored == []
    notes_state.main.broadcast_force_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_note_db_down_still_returns_text(notes_state):
    notes_state.conversation_log.add_note = AsyncMock(return_value=None)
    body = np.full(16000, 0.1, dtype=np.float32).tobytes()
    out = _json(await routes_notes.post_satellite_note(_req(body=body)))
    assert out["id"] is None and out["stored"] is False
    assert out["text"] == "remember the milk" and out["ts"]
    notes_state.main.broadcast_force_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_note_db_disabled_still_returns_text(notes_state):
    notes_state.conversation_log = SimpleNamespace(enabled=False)
    body = np.full(16000, 0.1, dtype=np.float32).tobytes()
    out = _json(await routes_notes.post_satellite_note(_req(body=body)))
    assert out["stored"] is False and out["text"] == "remember the milk"


@pytest.mark.asyncio
async def test_note_long_audio_is_windowed(notes_state):
    n = {"i": 0}

    async def fake(chunk):
        assert len(chunk) <= 28 * 16000
        n["i"] += 1
        return f"w{n['i']}"
    notes_state.pipeline.transcriber.transcribe = fake
    pcm = (np.full(16000 * 90, 0.2) * 32767).astype("<i2").tobytes()   # 90 s
    out = _json(await routes_notes.post_satellite_note(_req(body=pcm, fmt="s16le", chunk=65536)))
    assert out["text"] == "w1 w2 w3 w4"
    assert out["duration_s"] == 90.0


@pytest.mark.asyncio
async def test_note_demo_mode_refuses(notes_state, monkeypatch):
    monkeypatch.setenv("HAL_DEMO_MODE", "1")
    body = np.full(16000, 0.1, dtype=np.float32).tobytes()
    assert _status(await routes_notes.post_satellite_note(_req(body=body))) == 403


@pytest.mark.asyncio
async def test_note_route_over_real_asgi_with_chunked_body(notes_state):
    """End-to-end through FastAPI: a chunked (no Content-Length) body is read
    via request.stream()."""
    import httpx
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(routes_notes.router)
    pcm = (np.full(16000 * 2, 0.2) * 32767).astype("<i2").tobytes()

    async def gen():
        for i in range(0, len(pcm), 4096):
            yield pcm[i:i + 4096]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.post("/api/satellite/note", content=gen(), headers={
            "Authorization": "Bearer full_tok", "X-Sample-Rate": "16000",
            "X-Sample-Format": "s16le"})
        assert r.status_code == 200, r.text
        assert r.json()["duration_s"] == 2.0
        notes_state.conversation_log.fetch_notes = AsyncMock(
            return_value={"rows": [], "has_more": False})
        r = await c.get("/api/notes?limit=10&q=milk")
        assert r.status_code == 200 and r.json() == {"rows": [], "has_more": False}
        notes_state.conversation_log.delete_note = AsyncMock(return_value=True)
        r = await c.delete("/api/notes/3")
        assert r.status_code == 204


# --- list / delete ---------------------------------------------------------------

def _get_req(token=None, **qp):
    h = {"authorization": f"Bearer {token}"} if token else {}
    return SimpleNamespace(app=SimpleNamespace(), headers=h, query_params=qp)


@pytest.mark.asyncio
async def test_get_notes_passes_paging_and_query(notes_state):
    notes_state.conversation_log.fetch_notes = AsyncMock(
        return_value={"rows": [{"id": 1}], "has_more": False})
    out = await routes_notes.get_notes(_get_req(limit="50", before_id="9", q="milk"))
    assert out["rows"] == [{"id": 1}]
    notes_state.conversation_log.fetch_notes.assert_awaited_once_with(
        limit=50, before_id=9, q="milk")


@pytest.mark.asyncio
async def test_get_notes_disabled_and_scope_and_503(notes_state):
    assert _status(await routes_notes.get_notes(_get_req("watch_tok"))) == 403
    notes_state.conversation_log.fetch_notes = AsyncMock(side_effect=RuntimeError("down"))
    assert _status(await routes_notes.get_notes(_get_req())) == 503
    notes_state.conversation_log = SimpleNamespace(enabled=False)
    assert await routes_notes.get_notes(_get_req()) == {
        "rows": [], "has_more": False, "disabled": True}


@pytest.mark.asyncio
async def test_delete_note_204_404(notes_state, monkeypatch):
    notes_state.conversation_log.delete_note = AsyncMock(side_effect=[True, False])
    assert _status(await routes_notes.delete_note(4, _get_req("full_tok"))) == 204
    assert _status(await routes_notes.delete_note(5, _get_req())) == 404
    assert _status(await routes_notes.delete_note(4, _get_req("watch_tok"))) == 403
    monkeypatch.setenv("HAL_REQUIRE_TOKEN", "1")
    assert _status(await routes_notes.delete_note(4, _get_req())) == 401


# --- local tools + MCP -------------------------------------------------------------

def test_mcp_exposes_notes_tools():
    from server.app.mcp_server import build_mcp_server
    mcp = build_mcp_server(SimpleNamespace(local_tools=None))
    assert {"show_notes", "hide_notes", "list_notes", "get_note", "delete_note"} <= set(
        mcp._tool_manager._tools)


@pytest.mark.asyncio
async def test_mcp_notes_tools_dispatch_to_local_tools():
    from server.app.mcp_server import build_mcp_server
    local = SimpleNamespace(
        tool_names={"list_notes", "get_note", "delete_note", "show_notes"},
        call_tool=AsyncMock(return_value="ok"))
    mcp = build_mcp_server(SimpleNamespace(local_tools=local))
    tools = mcp._tool_manager._tools
    await tools["list_notes"].fn(limit=5, query="milk")
    local.call_tool.assert_awaited_with("list_notes", {"limit": 5, "query": "milk"})
    await tools["get_note"].fn(id=3)
    local.call_tool.assert_awaited_with("get_note", {"id": 3})
    await tools["delete_note"].fn(id=3)
    local.call_tool.assert_awaited_with("delete_note", {"id": 3})
    await tools["show_notes"].fn()
    local.call_tool.assert_awaited_with("show_notes", {})


@pytest.fixture
def local_tools(monkeypatch):
    """build_local_tools against a fake main + a fake notes store."""
    import server.app.main as real_main  # noqa: F401  (ensure importable)
    from server.app.local_tools_register import build_local_tools
    pushed = []

    async def push(state, msg):
        pushed.append(("rpi", msg))
        return True

    async def bcast(state, msg):
        pushed.append(("ui", msg))

    monkeypatch.setattr("server.app.main._push_to_rpi", push)
    monkeypatch.setattr("server.app.main.broadcast_to_ui", bcast)
    store = SimpleNamespace(
        enabled=True,
        fetch_notes=AsyncMock(return_value={"rows": [
            {"id": 1, "ts": "2026-11-14T08:00:00+00:00", "text": "old", "origin": "A", "duration_s": 2.0},
            {"id": 2, "ts": "2026-11-15T08:00:00+00:00", "text": "new", "origin": None, "duration_s": 3.0},
        ], "has_more": False}),
        get_note=AsyncMock(return_value={"id": 2, "ts": "2026-11-15T08:00:00+00:00",
                                         "text": "new", "origin": "B", "duration_s": 3.0}),
        delete_note=AsyncMock(return_value=True),
    )
    from server.app.main import AppState
    state = AppState()
    state.conversation_log = store
    tools = build_local_tools(state)
    return tools, store, pushed


@pytest.mark.asyncio
async def test_local_notes_tools(local_tools):
    tools, store, pushed = local_tools
    assert {"show_notes", "hide_notes", "list_notes", "get_note", "delete_note"} <= set(tools.tool_names)
    out = await tools.call_tool("list_notes", {"limit": 5, "query": "x"})
    store.fetch_notes.assert_awaited_once_with(limit=5, q="x")
    assert out.index("#2") < out.index("#1")        # newest first
    assert "unknown device" in out
    assert "#2" in await tools.call_tool("get_note", {"id": 2})
    assert "Deleted" in await tools.call_tool("delete_note", {"id": 2})
    store.delete_note.return_value = False
    assert "no voice note" in await tools.call_tool("delete_note", {"id": 9})
    assert "numeric" in await tools.call_tool("get_note", {"id": "abc"})

    await tools.call_tool("show_notes", {})
    assert ("rpi", {"type": "show_notes", "duration_s": 30}) in pushed
    assert ("ui", {"type": "show_notes", "duration_s": 30}) in pushed
    await tools.call_tool("hide_notes", {})
    assert ("rpi", {"type": "hide_notes"}) in pushed


# --- intent hints + MQTT -----------------------------------------------------------

def test_notes_intent_hints():
    from server.app.conversation import _match_intent_hint
    assert _match_intent_hint("show my notes").tool == "show_notes"
    assert _match_intent_hint("can you open the voice notes please").tool == "show_notes"
    assert _match_intent_hint("hide my notes").tool == "hide_notes"
    assert _match_intent_hint("show the conversation log").tool == "show_conversation_log"
    assert _match_intent_hint("take a note to buy milk") is None


def test_mqtt_notes_buttons_and_dispatch():
    from server.app.mqtt_bridge import MQTTBridge
    b = MQTTBridge(host="x", device_id="hal-default", device_name="HAL")
    names = {p.get("name") for _t, p in b._discovery_payloads()}
    assert {"Show Notes", "Hide Notes"} <= names


def test_audio_streamer_forwards_notes_messages():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "rpi" / "audio_streamer" / "main.py").read_text()
    for t in ('"show_notes"', '"hide_notes"', '"note_added"'):
        assert t in src
    # the hub page (same-origin :8080) reaches the notes API through the proxy
    assert 'add_get("/api/notes", self._proxy_to_ai_server)' in src
    assert "add_delete(r\"/api/notes/{note_id:\\d+}\", self._proxy_to_ai_server)" in src
