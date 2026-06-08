"""Satellite gateway: the allowlist is the security boundary, so these tests
pin DEFAULT-DENY (every non-listed route/method 404s before any upstream
call), edge token enforcement, the auth-subrequest cache, query-vs-Bearer
token extraction, public-route exemptions, and the WS token requirement.

The gateway lives in gateway/ (not a package), loaded by file path. A fake
upstream aiohttp server stands in for ai-server: it implements
/api/pair/status (200 iff token == GOOD) and echoes everything else, and
counts pair/status hits so the cache can be observed.
"""
import importlib.util
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

GW_PATH = Path(__file__).resolve().parent.parent / "gateway" / "gateway.py"

GOOD = "valid-token-123"


def _load_gateway():
    spec = importlib.util.spec_from_file_location("hal_gateway", GW_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hal_gateway"] = mod
    spec.loader.exec_module(mod)
    return mod


gw_mod = _load_gateway()


# --- pure helpers --------------------------------------------------------------

def test_redacts_token_in_query():
    assert gw_mod._redact("token=secret&x=1") == "token=<redacted>&x=1"
    assert gw_mod._redact("a=1&token=abc") == "a=1&token=<redacted>"
    assert gw_mod._redact("a=1") == "a=1"


def test_rate_limiter():
    rl = gw_mod.RateLimiter(3)
    assert [rl.allow("ip") for _ in range(4)] == [True, True, True, False]
    assert rl.allow("other") is True       # per-IP


def test_auth_cache_positive_only_and_expiry(monkeypatch):
    cache = gw_mod.AuthCache(ttl=10)
    assert cache.fresh("t") is False        # nothing cached
    cache.store("t")
    assert cache.fresh("t") is True
    # expire
    now = [gw_mod.time.monotonic() + 100]
    monkeypatch.setattr(gw_mod.time, "monotonic", lambda: now[0])
    assert cache.fresh("t") is False


# --- proxy integration ---------------------------------------------------------

@pytest.fixture
async def upstream():
    """Fake ai-server: pair/status gate + echo, counting auth hits."""
    state = {"status_hits": 0, "proxied": []}

    async def pair_status(request):
        state["status_hits"] += 1
        auth = request.headers.get("Authorization", "")
        if auth == f"Bearer {GOOD}":
            return web.json_response({"valid": True})
        return web.json_response({"valid": False}, status=401)

    async def echo(request):
        state["proxied"].append((request.method, request.path))
        return web.json_response({"ok": True, "path": request.path})

    async def ws(request):
        wsr = web.WebSocketResponse()
        await wsr.prepare(request)
        await wsr.send_str("hello-from-server")
        async for msg in wsr:
            if msg.type == web.WSMsgType.TEXT:
                await wsr.send_str(f"echo:{msg.data}")
        return wsr

    app = web.Application()
    app.router.add_get("/api/pair/status", pair_status)
    app.router.add_get("/ws/ui", ws)
    app.router.add_route("*", "/{tail:.*}", echo)
    server = TestServer(app)
    await server.start_server()
    state["url"] = str(server._root).rstrip("/")
    yield state, server
    await server.close()


@pytest.fixture
async def client(upstream, monkeypatch):
    state, _server = upstream
    monkeypatch.setattr(gw_mod, "AI_SERVER_URL", state["url"])
    monkeypatch.setattr(gw_mod, "AUTH_CACHE_TTL", 30)
    app = gw_mod.build_app()
    cl = TestClient(TestServer(app))
    await cl.start_server()
    yield cl, state
    await cl.close()


def _auth(token=GOOD):
    return {"Authorization": f"Bearer {token}"}


# default-deny

@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("POST", "/api/pair/redeem"),          # pairing mint — must NOT be reachable
    ("POST", "/api/pair/request"),
    ("POST", "/api/pair/revoke"),          # device kill switch — LAN-only
    ("GET",  "/api/pair/devices"),         # device list — LAN-only
    ("POST", "/api/speak"),                # announce
    ("POST", "/api/display"),
    ("POST", "/api/volume"),
    ("POST", "/api/ptt/start"),
    ("GET",  "/api/snapshot.jpg"),
    ("GET",  "/mcp/sse"),
    ("GET",  "/api/photo_frame/idle"),     # kiosk photo frame (not satellite)
    ("DELETE", "/api/command"),            # wrong method on an allowed path
    ("GET",  "/api/command"),              # wrong method
    ("GET",  "/themes/../etc/passwd"),     # traversal shape
])
async def test_denied_routes_404_without_touching_upstream(client, method, path):
    cl, state = client
    resp = await cl.request(method, path, headers=_auth())
    assert resp.status == 404
    assert state["proxied"] == []          # never proxied
    assert state["status_hits"] == 0       # not even auth-checked


# auth enforcement

@pytest.mark.asyncio
async def test_protected_route_needs_valid_token(client):
    cl, state = client
    assert (await cl.post("/api/command")).status == 401            # no token
    assert (await cl.post("/api/command", headers=_auth("bad"))).status == 401
    good = await cl.post("/api/command", headers=_auth())
    assert good.status == 200
    assert ("POST", "/api/command") in state["proxied"]


@pytest.mark.asyncio
async def test_token_via_query_param(client):
    """The MJPEG <img> tag and WS upgrade can only carry ?token=."""
    cl, state = client
    resp = await cl.get(f"/api/satellite/stream.mjpeg?token={GOOD}")
    assert resp.status == 200
    assert (await cl.get("/api/satellite/stream.mjpeg?token=bad")).status == 401


# CORS preflight — the app's WebView origin differs from the gateway host, so
# token-bearing fetches are preceded by an unauthenticated OPTIONS preflight.

@pytest.mark.asyncio
async def test_preflight_allowed_route_no_token(client):
    """OPTIONS preflight for an allowlisted (method, path) is forwarded WITHOUT
    a token (browsers strip credentials from preflights) and never auth-checked."""
    cl, state = client
    resp = await cl.options(
        "/api/conversation/log?limit=100",
        headers={"Access-Control-Request-Method": "GET",
                 "Origin": "capacitor://localhost"},
    )
    assert resp.status == 200
    assert ("OPTIONS", "/api/conversation/log") in state["proxied"]
    assert state["status_hits"] == 0       # preflight is not token-validated


@pytest.mark.asyncio
async def test_preflight_unlisted_route_denied(client):
    """A preflight for a route not in the allowlist is denied, untouched."""
    cl, state = client
    resp = await cl.options(
        "/api/speak",
        headers={"Access-Control-Request-Method": "POST"},
    )
    assert resp.status == 404
    assert state["proxied"] == []


@pytest.mark.asyncio
async def test_preflight_wrong_method_for_path_denied(client):
    """Preflight names the real method; DELETE on /api/command isn't allowed."""
    cl, state = client
    resp = await cl.options(
        "/api/command",
        headers={"Access-Control-Request-Method": "DELETE"},
    )
    assert resp.status == 404
    assert state["proxied"] == []


@pytest.mark.asyncio
async def test_auth_cache_avoids_repeat_subrequests(client):
    cl, state = client
    for _ in range(4):
        assert (await cl.get("/api/conversation/log", headers=_auth())).status == 200
    assert state["status_hits"] == 1       # validated once, cached after


# public routes

@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/health", "/api/themes", "/themes/dark/theme.css"])
async def test_public_routes_need_no_token(client, path):
    cl, state = client
    resp = await cl.get(path)
    assert resp.status == 200
    assert state["status_hits"] == 0       # no auth subrequest for public routes


@pytest.mark.asyncio
async def test_cloud_llm_post_allowed_with_token(client):
    """User decision: remote Cloud LLM toggle is permitted."""
    cl, state = client
    resp = await cl.post("/api/cloud_llm", headers=_auth(), json={"enabled": True})
    assert resp.status == 200
    assert ("POST", "/api/cloud_llm") in state["proxied"]


# websocket

@pytest.mark.asyncio
async def test_ws_requires_token(client):
    cl, state = client
    with pytest.raises(Exception):
        await cl.ws_connect("/ws/ui")          # no token → 401, handshake fails


@pytest.mark.asyncio
async def test_ws_proxies_both_directions_with_token(client):
    cl, state = client
    ws = await cl.ws_connect(f"/ws/ui?token={GOOD}")
    assert (await ws.receive()).data == "hello-from-server"
    await ws.send_str("ping")
    assert (await ws.receive()).data == "echo:ping"
    await ws.close()
