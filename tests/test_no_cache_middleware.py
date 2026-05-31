"""Tests for the kiosk web server's no-cache middleware
(rpi/audio_streamer/main.AudioManager._no_cache_middleware).

Regression: static files are served by aiohttp's FileResponse, whose
Content-Type isn't set until send time (after the middleware runs). A
content-type-only check therefore skipped every static .js/.css, so the
kiosk kept serving stale ES modules from cache — which broke a deploy that
added a new export to photo_frame.js. The middleware must key off the
request path too."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# main.py imports pyaudio at module load; it isn't needed for the pure
# middleware logic and isn't installed in the test env, so stub it.
sys.modules.setdefault("pyaudio", MagicMock())

from rpi.audio_streamer.main import AudioManager  # noqa: E402


def _req(path):
    return SimpleNamespace(path=path)


def _resp(content_type=""):
    # Mimic the relevant surface of an aiohttp response.
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    return SimpleNamespace(headers=headers)


async def _run(path, content_type=""):
    resp = _resp(content_type)
    handler = AsyncMock(return_value=resp)
    # _no_cache_middleware is a @staticmethod @web.middleware — the
    # decorator leaves the coroutine callable as (request, handler).
    out = await AudioManager._no_cache_middleware(_req(path), handler)
    return out.headers


@pytest.mark.asyncio
async def test_static_js_gets_no_cache_even_without_content_type():
    # The exact failure case: FileResponse with no Content-Type yet.
    headers = await _run("/photo_frame.js", content_type="")
    assert headers["Cache-Control"] == "no-cache, no-store, must-revalidate"
    assert headers["Pragma"] == "no-cache"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/", "/app.js", "/style.css", "/index.html",
                                  "/fonts/x.woff2", "/api/themes.json"])
async def test_asset_paths_get_no_cache(path):
    headers = await _run(path, content_type="")
    assert "Cache-Control" in headers


@pytest.mark.asyncio
async def test_content_type_still_works_as_fallback():
    # A handler-built JSON response (Content-Type set, non-asset path).
    headers = await _run("/api/music/state", content_type="application/json")
    assert "Cache-Control" in headers


@pytest.mark.asyncio
async def test_non_asset_is_left_alone():
    # e.g. a video file or octet-stream on a non-asset path — must NOT be
    # marked no-store (we WANT the kiosk to cache the big looping video).
    headers = await _run("/media/loop.mp4", content_type="video/mp4")
    assert "Cache-Control" not in headers
