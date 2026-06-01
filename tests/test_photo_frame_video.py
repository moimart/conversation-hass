"""Tests for the looping photo-frame video download/cache + endpoint
(server/app/main.py)."""

import os
from types import SimpleNamespace

import pytest

from server.app import main as srv


# ---------------------------------------------------------------------------
# Fake httpx streaming client
# ---------------------------------------------------------------------------

class _FakeStreamResponse:
    def __init__(self, content_type, chunks, status_ok=True):
        self.headers = {"content-type": content_type}
        self._chunks = chunks
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            raise RuntimeError("HTTP error")

    async def aiter_bytes(self, size):
        for c in self._chunks:
            yield c

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, response):
        self._response = response

    def stream(self, method, url, headers=None):
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _patch_httpx(monkeypatch, content_type, chunks, status_ok=True):
    import httpx
    resp = _FakeStreamResponse(content_type, chunks, status_ok)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(resp))


def _state():
    return SimpleNamespace(photo_frame_video_hash="")


@pytest.fixture
def cache(tmp_path, monkeypatch):
    p = tmp_path / "photo_frame_video.mp4"
    monkeypatch.setenv("PHOTO_FRAME_VIDEO_CACHE", str(p))
    return p


# ---------------------------------------------------------------------------
# _download_photo_frame_video
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_download_writes_cache_and_sets_hash(cache, monkeypatch):
    _patch_httpx(monkeypatch, "video/mp4", [b"abc", b"def"])
    state = _state()
    ok = await srv._download_photo_frame_video(state, "http://nas/loop.mp4")
    assert ok is True
    assert cache.read_bytes() == b"abcdef"
    assert state.photo_frame_video_hash != ""
    # Sidecar holds the same hash.
    sidecar = str(cache) + ".sha256"
    assert os.path.isfile(sidecar)
    with open(sidecar) as f:
        assert f.read().strip() == state.photo_frame_video_hash


@pytest.mark.asyncio
async def test_download_rejects_non_video(cache, monkeypatch):
    _patch_httpx(monkeypatch, "image/png", [b"\x89PNG"])
    state = _state()
    ok = await srv._download_photo_frame_video(state, "http://nas/not_a_video.png")
    assert ok is False
    assert not cache.exists()
    assert state.photo_frame_video_hash == ""


@pytest.mark.asyncio
async def test_download_aborts_over_cap(cache, monkeypatch):
    from server.app import photo_frame as pf
    monkeypatch.setattr(pf, "PHOTO_FRAME_VIDEO_MAX_BYTES", 4)
    _patch_httpx(monkeypatch, "video/mp4", [b"aa", b"bb", b"cc"])  # 6 > 4
    state = _state()
    ok = await srv._download_photo_frame_video(state, "http://nas/big.mp4")
    assert ok is False
    # Temp file cleaned up, no cache left behind.
    assert not cache.exists()
    assert state.photo_frame_video_hash == ""


@pytest.mark.asyncio
async def test_download_empty_url_returns_false(cache, monkeypatch):
    state = _state()
    assert await srv._download_photo_frame_video(state, "") is False


# ---------------------------------------------------------------------------
# cache helpers
# ---------------------------------------------------------------------------

def test_load_hash_from_sidecar(cache):
    cache.write_bytes(b"video-bytes")
    (cache.parent / (cache.name + ".sha256")).write_text("deadbeef\n")
    state = _state()
    h = srv._load_photo_frame_video_hash(state)
    assert h == "deadbeef"
    assert state.photo_frame_video_hash == "deadbeef"


def test_load_hash_missing_cache_returns_empty(cache):
    state = SimpleNamespace(photo_frame_video_hash="stale")
    h = srv._load_photo_frame_video_hash(state)
    assert h == ""
    assert state.photo_frame_video_hash == ""


def test_clear_cache_removes_files_and_hash(cache):
    cache.write_bytes(b"v")
    sidecar = cache.parent / (cache.name + ".sha256")
    sidecar.write_text("abc\n")
    state = SimpleNamespace(photo_frame_video_hash="abc")
    srv._clear_photo_frame_video_cache(state)
    assert not cache.exists()
    assert not sidecar.exists()
    assert state.photo_frame_video_hash == ""


# ---------------------------------------------------------------------------
# GET /api/photo_frame/video
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_endpoint_404_when_no_cache(cache):
    resp = await srv.get_photo_frame_video()
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_endpoint_serves_file_when_cached(cache):
    cache.write_bytes(b"loopbytes")
    resp = await srv.get_photo_frame_video()
    from fastapi.responses import FileResponse
    assert isinstance(resp, FileResponse)
    assert resp.path == str(cache)
    assert resp.media_type == "video/mp4"
