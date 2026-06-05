"""Tests for the persistent conversation log: the ConversationLog module
(mocked asyncpg — no postgres needed), the logging seam in ConversationManager,
the origin-resolution rule (device names, never tokens), and the HTTP page
endpoint."""
import json
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import server.app.conversation_log as clog_mod
from server.app.conversation_log import ConversationLog


# --- fakes -------------------------------------------------------------------

class _FakeConn:
    def __init__(self, fetch_rows=None, fail=False):
        self.execute = AsyncMock()
        self.fetch = AsyncMock(return_value=fetch_rows or [])
        if fail:
            self.execute.side_effect = RuntimeError("db down")
            self.fetch.side_effect = RuntimeError("db down")


class _FakePool:
    def __init__(self, conn):
        self._conn = conn
        self.close = AsyncMock()

    def acquire(self):
        conn = self._conn
        class _Ctx:
            async def __aenter__(self):  # noqa: ANN001
                return conn
            async def __aexit__(self, *a):  # noqa: ANN001
                return False
        return _Ctx()


def _row(id, kind="user", text="hi", origin=None, meta=None):
    return {
        "id": id,
        "ts": datetime(2026, 6, 5, 10, 0, id % 60, tzinfo=timezone.utc),
        "kind": kind, "text": text, "origin": origin, "meta": meta,
    }


def _patched_pool(conn):
    fake_asyncpg = MagicMock()
    fake_asyncpg.create_pool = AsyncMock(return_value=_FakePool(conn))
    return patch.object(clog_mod, "asyncpg", fake_asyncpg)


# --- ConversationLog ----------------------------------------------------------

@pytest.mark.asyncio
async def test_log_inserts_row():
    conn = _FakeConn()
    with _patched_pool(conn):
        cl = ConversationLog("postgresql://x")
        await cl.log("user", "turn on the lights", origin="iPhone Air")
    args = conn.execute.await_args_list[-1].args
    assert "INSERT INTO conversation_log" in args[0]
    assert args[1:] == ("user", "turn on the lights", "iPhone Air", None)


@pytest.mark.asyncio
async def test_log_never_raises_on_db_failure():
    conn = _FakeConn(fail=True)
    with _patched_pool(conn):
        cl = ConversationLog("postgresql://x")
        await cl.log("assistant", "done")          # must not raise
    assert cl._pool is None                         # dropped for lazy reconnect


@pytest.mark.asyncio
async def test_log_skips_empty_and_invalid():
    conn = _FakeConn()
    with _patched_pool(conn):
        cl = ConversationLog("postgresql://x")
        await cl.log("user", "   ")
        await cl.log("bogus-kind", "hello")
    # only schema execute (on connect), no INSERTs
    inserts = [c for c in conn.execute.await_args_list if "INSERT" in c.args[0]]
    assert inserts == []


@pytest.mark.asyncio
async def test_fetch_keyset_query_and_asc_reversal():
    rows_desc = [_row(5), _row(4), _row(3)]        # DESC from the DB
    conn = _FakeConn(fetch_rows=rows_desc)
    with _patched_pool(conn):
        cl = ConversationLog("postgresql://x")
        out = await cl.fetch(limit=3, before_id=6)
    q = conn.fetch.await_args.args
    assert "id < $1" in q[0] and "ORDER BY id DESC" in q[0]
    assert q[1] == 6 and q[2] == 3
    assert [r["id"] for r in out["rows"]] == [3, 4, 5]   # reversed to ASC
    assert out["has_more"] is True                        # page was full


@pytest.mark.asyncio
async def test_fetch_disabled_without_dsn():
    cl = ConversationLog("")
    out = await cl.fetch()
    assert out == {"rows": [], "has_more": False, "disabled": True}
    await cl.log("user", "x")                       # silently no-ops


@pytest.mark.asyncio
async def test_lazy_reconnect_on_use():
    conn = _FakeConn()
    fake_asyncpg = MagicMock()
    fake_asyncpg.create_pool = AsyncMock(side_effect=[RuntimeError("not up"), _FakePool(conn)])
    with patch.object(clog_mod, "asyncpg", fake_asyncpg):
        cl = ConversationLog("postgresql://x")
        await cl.log("user", "first try")           # connect fails → dropped
        assert cl._pool is None
        await cl.log("user", "second try")          # reconnects → inserted
    inserts = [c for c in conn.execute.await_args_list if "INSERT" in c.args[0]]
    assert len(inserts) == 1


# --- conversation seam ---------------------------------------------------------

@pytest.fixture
def conversation(mock_mcp_client, mock_tts):
    from server.app.conversation import ConversationManager
    cm = ConversationManager(
        wake_word="hey homie",
        ollama_host="http://fake-ollama:11434",
        ollama_model="test-model",
        mcp_client=mock_mcp_client,
        tts_engine=mock_tts,
        event_logger=AsyncMock(),
    )
    cm.on_response = AsyncMock()
    return cm


@pytest.mark.asyncio
async def test_turn_logs_user_then_assistant(conversation):
    await conversation.process_text("hey homie turn on the lights")
    with patch.object(conversation, "_chat_completion", new_callable=AsyncMock) as chat:
        chat.return_value = {"message": {"content": "Done, Master."}}
        await conversation.on_silence()
    calls = conversation.event_logger.await_args_list
    kinds = [c.args[0] for c in calls]
    assert kinds == ["user", "assistant"]
    assert calls[0].args[1] == "turn on the lights"
    assert calls[1].args[1] == "Done, Master."
    # origin token (None for voice) is passed through for the wrapper to resolve
    assert calls[0].kwargs.get("origin_token") is None


@pytest.mark.asyncio
async def test_satellite_turn_passes_origin_token(conversation):
    conversation._pending_origin = "tok-secret"
    await conversation.process_text("hey homie hello")
    with patch.object(conversation, "_chat_completion", new_callable=AsyncMock) as chat:
        chat.return_value = {"message": {"content": "Hi."}}
        await conversation.on_silence()
    for c in conversation.event_logger.await_args_list:
        assert c.kwargs.get("origin_token") == "tok-secret"


@pytest.mark.asyncio
async def test_empty_response_not_logged_as_assistant(conversation):
    await conversation.process_text("hey homie hello")
    with patch.object(conversation, "_chat_completion", new_callable=AsyncMock) as chat:
        chat.return_value = {"message": {"content": ""}}
        await conversation.on_silence()
    kinds = [c.args[0] for c in conversation.event_logger.await_args_list]
    assert kinds == ["user"]                        # no empty assistant row


@pytest.mark.asyncio
async def test_event_logger_failure_does_not_break_turn(conversation):
    conversation.event_logger = AsyncMock(side_effect=RuntimeError("pg down"))
    await conversation.process_text("hey homie hello")
    with patch.object(conversation, "_chat_completion", new_callable=AsyncMock) as chat:
        chat.return_value = {"message": {"content": "Still fine."}}
        await conversation.on_silence()
    conversation.on_response.assert_awaited_once()  # turn completed normally


# --- origin resolution (the wrapper rule: device names, never tokens) ---------

def test_pairing_device_name_accessor():
    from server.app.pairing import PairingManager
    pm = PairingManager.__new__(PairingManager)
    pm._tokens = {"tok-abc": {"device_name": "iPhone Air", "created_at": 0}}
    assert pm.device_name("tok-abc") == "iPhone Air"
    assert pm.device_name("missing") is None
    assert pm.device_name(None) is None


# --- HTTP endpoint -------------------------------------------------------------

@pytest.fixture
def fake_main(monkeypatch):
    st = SimpleNamespace(conversation_log=None)
    fake = SimpleNamespace(_get_state=lambda app: st)
    monkeypatch.setitem(sys.modules, "server.app.main", fake)
    return st


def _req(**params):
    return SimpleNamespace(app=SimpleNamespace(), headers={}, query_params=params)


@pytest.mark.asyncio
async def test_endpoint_disabled_shape(fake_main):
    from server.app.routes_http import get_conversation_log
    fake_main.conversation_log = ConversationLog("")
    out = await get_conversation_log(_req())
    assert out["disabled"] is True and out["rows"] == []


@pytest.mark.asyncio
async def test_endpoint_pages_and_clamps(fake_main):
    from server.app.routes_http import get_conversation_log
    with patch.object(clog_mod, "asyncpg", MagicMock()):   # make .enabled true
        cl = ConversationLog("postgresql://x")
        cl.fetch = AsyncMock(return_value={"rows": [], "has_more": False})
        fake_main.conversation_log = cl
        await get_conversation_log(_req(limit="9999", before_id="42"))
        kwargs = cl.fetch.await_args.kwargs
        assert kwargs["before_id"] == 42
        assert kwargs["limit"] == 9999  # endpoint passes through; module clamps


@pytest.mark.asyncio
async def test_endpoint_503_when_db_down(fake_main):
    from server.app.routes_http import get_conversation_log
    with patch.object(clog_mod, "asyncpg", MagicMock()):
        cl = ConversationLog("postgresql://x")
        cl.fetch = AsyncMock(side_effect=RuntimeError("unavailable"))
        fake_main.conversation_log = cl
        resp = await get_conversation_log(_req())
    assert resp.status_code == 503
    assert json.loads(resp.body)["error"] == "log_unavailable"
