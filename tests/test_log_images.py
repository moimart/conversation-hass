"""Orb images in the conversation log (schema v2).

Covers: the idempotent schema migration statements, log() storing thumbnail
bytes, fetch() exposing has_image (never bytes), fetch_image(), the media
dispatcher's thumbnail + log seam, and the lazy image HTTP route."""
import asyncio
import io
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import server.app.conversation_log as clog_mod
from server.app.conversation_log import _SCHEMA, VALID_KINDS, ConversationLog
from tests.test_conversation_log import _FakeConn, _FakePool, _row, _patched_pool


# --- schema / migration ----------------------------------------------------------

def test_schema_carries_v2_migration():
    """ensure_schema runs on every connect — the v2 statements must be
    idempotent ALTERs that migrate a v1 database in place."""
    assert "ADD COLUMN IF NOT EXISTS image BYTEA" in _SCHEMA
    assert "ADD COLUMN IF NOT EXISTS image_mime TEXT" in _SCHEMA
    assert "DROP CONSTRAINT IF EXISTS conversation_log_kind_check" in _SCHEMA
    assert "'image'" in _SCHEMA
    assert "image" in VALID_KINDS


# --- log() / fetch() / fetch_image() ----------------------------------------------

@pytest.mark.asyncio
async def test_log_stores_image_bytes():
    conn = _FakeConn()
    with _patched_pool(conn):
        cl = ConversationLog("postgresql://x")
        await cl.log("image", "Image shown on the orb", origin="camera.front_door",
                     image=b"JPGDATA", image_mime="image/jpeg")
    args = conn.execute.await_args_list[-1].args
    assert "INSERT" in args[0] and "image" in args[0]
    assert args[1] == "image"
    assert args[5] == b"JPGDATA" and args[6] == "image/jpeg"


@pytest.mark.asyncio
async def test_fetch_exposes_has_image_not_bytes():
    rows_desc = [_row(2, kind="image", text="Image shown on the orb", has_image=True),
                 _row(1)]
    conn = _FakeConn(fetch_rows=rows_desc)
    with _patched_pool(conn):
        cl = ConversationLog("postgresql://x")
        out = await cl.fetch()
    assert out["rows"][0]["has_image"] is False
    assert out["rows"][1]["has_image"] is True
    assert "image" not in out["rows"][1] or out["rows"][1].get("image") is None
    # The page query itself must not select the bytes column.
    sql = conn.fetch.await_args.args[0]
    assert "(image IS NOT NULL)" in sql and "SELECT id" in sql


@pytest.mark.asyncio
async def test_fetch_image_returns_bytes_and_mime():
    conn = _FakeConn(fetchrow_result={"image": b"JPG", "image_mime": "image/jpeg"})
    with _patched_pool(conn):
        cl = ConversationLog("postgresql://x")
        assert await cl.fetch_image(7) == (b"JPG", "image/jpeg")
        conn.fetchrow.return_value = None
        assert await cl.fetch_image(8) is None


# --- thumbnail + dispatch seam ------------------------------------------------------

def _png_bytes(size=(1200, 800), mode="RGB"):
    from PIL import Image
    buf = io.BytesIO()
    Image.new(mode, size, (200, 30, 30) if mode == "RGB" else (200, 30, 30, 128)).save(buf, "PNG")
    return buf.getvalue()


def test_make_thumbnail_downscales_to_jpeg():
    from PIL import Image
    from server.app.media import _LOG_THUMB_MAX, _make_thumbnail
    out = _make_thumbnail(_png_bytes((1200, 800)))
    assert out is not None
    data, mime = out
    assert mime == "image/jpeg"
    img = Image.open(io.BytesIO(data))
    assert max(img.size) <= _LOG_THUMB_MAX
    assert img.format == "JPEG"


def test_make_thumbnail_flattens_alpha():
    from server.app.media import _make_thumbnail
    out = _make_thumbnail(_png_bytes((300, 300), mode="RGBA"))
    assert out is not None and out[1] == "image/jpeg"


def test_make_thumbnail_rejects_junk():
    from server.app.media import _make_thumbnail
    assert _make_thumbnail(b"not an image") is None


@pytest.mark.asyncio
async def test_log_orb_image_writes_thumbnail():
    import base64
    from server.app.media import _log_orb_image
    clog = SimpleNamespace(enabled=True, log=AsyncMock())
    state = SimpleNamespace(conversation_log=clog)
    await _log_orb_image(state, base64.b64encode(_png_bytes()).decode(),
                         "image/png", "camera.front_door")
    clog.log.assert_awaited_once()
    args, kwargs = clog.log.await_args
    assert args[0] == "image"
    assert kwargs["origin"] == "camera.front_door"
    assert kwargs["image_mime"] == "image/jpeg"
    assert len(kwargs["image"]) > 100


@pytest.mark.asyncio
async def test_log_orb_image_noop_when_log_disabled():
    from server.app.media import _log_orb_image
    state = SimpleNamespace(conversation_log=None)
    await _log_orb_image(state, "xx", "image/png", "e")   # must not raise


# --- HTTP image route -----------------------------------------------------------------

@pytest.fixture
def fake_main(monkeypatch):
    st = SimpleNamespace(conversation_log=None)
    fake = SimpleNamespace(_get_state=lambda app: st)
    monkeypatch.setitem(sys.modules, "server.app.main", fake)
    return st


def _req(**params):
    return SimpleNamespace(app=SimpleNamespace(), headers={}, query_params=params)


@pytest.mark.asyncio
async def test_image_route_serves_bytes(fake_main):
    from server.app.routes_http import get_conversation_log_image
    with patch.object(clog_mod, "asyncpg", MagicMock()):
        cl = ConversationLog("postgresql://x")
        cl.fetch_image = AsyncMock(return_value=(b"JPG", "image/jpeg"))
        fake_main.conversation_log = cl
        resp = await get_conversation_log_image(_req(id="7"))
    assert resp.status_code == 200
    assert resp.body == b"JPG"
    assert resp.media_type == "image/jpeg"


@pytest.mark.asyncio
async def test_image_route_404_and_400(fake_main):
    from server.app.routes_http import get_conversation_log_image
    # disabled log -> 404
    assert (await get_conversation_log_image(_req(id="7"))).status_code == 404
    with patch.object(clog_mod, "asyncpg", MagicMock()):
        cl = ConversationLog("postgresql://x")
        cl.fetch_image = AsyncMock(return_value=None)
        fake_main.conversation_log = cl
        assert (await get_conversation_log_image(_req(id="999"))).status_code == 404
        assert (await get_conversation_log_image(_req(id="abc"))).status_code == 400


@pytest.mark.asyncio
async def test_image_route_503_when_db_down(fake_main):
    from server.app.routes_http import get_conversation_log_image
    with patch.object(clog_mod, "asyncpg", MagicMock()):
        cl = ConversationLog("postgresql://x")
        cl.fetch_image = AsyncMock(side_effect=RuntimeError("down"))
        fake_main.conversation_log = cl
        assert (await get_conversation_log_image(_req(id="7"))).status_code == 503
