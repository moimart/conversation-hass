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
    state = {"status_hits": 0, "proxied": [], "bodies": []}

    async def pair_status(request):
        state["status_hits"] += 1
        auth = request.headers.get("Authorization", "")
        if auth == f"Bearer {GOOD}":
            return web.json_response({"valid": True})
        return web.json_response({"valid": False}, status=401)

    async def echo(request):
        state["proxied"].append((request.method, request.path))
        n = 0
        async for chunk in request.content.iter_chunked(65536):
            n += len(chunk)
        state["bodies"].append((request.method, request.path, n,
                                request.headers.get("Transfer-Encoding")))
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
    ("POST", "/api/pair/derive"),          # scoped-token mint — LAN-only, like pairing
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
async def test_photo_broadcast_allowed_with_token(client):
    """The mirror shutter posts a captured still through the gateway (token-gated)."""
    cl, state = client
    assert (await cl.post("/api/satellite/photo-broadcast")).status == 401   # no token
    good = await cl.post("/api/satellite/photo-broadcast", headers=_auth())
    assert good.status == 200
    assert ("POST", "/api/satellite/photo-broadcast") in state["proxied"]


@pytest.mark.asyncio
async def test_satellite_stt_allowed_with_token(client):
    """A phone POSTs captured mic audio for server-side STT (token-gated)."""
    cl, state = client
    assert (await cl.post("/api/satellite/stt")).status == 401   # no token
    good = await cl.post("/api/satellite/stt", headers=_auth())
    assert good.status == 200
    assert ("POST", "/api/satellite/stt") in state["proxied"]


@pytest.mark.asyncio
async def test_satellite_voice_allowed_with_token(client):
    """A phone reads/sets its own TTS voice through the gateway (token-gated)."""
    cl, state = client
    assert (await cl.get("/api/satellite/voice")).status == 401   # no token
    assert (await cl.get("/api/satellite/voice", headers=_auth())).status == 200
    assert (await cl.post("/api/satellite/voice", headers=_auth())).status == 200
    assert ("GET", "/api/satellite/voice") in state["proxied"]
    assert ("POST", "/api/satellite/voice") in state["proxied"]


@pytest.mark.asyncio
async def test_native_device_routes_token_gated(client):
    """PAL Round (ESP32 firmware) reaches its device routes through the gateway."""
    cl, state = client
    paths = ["/api/device/info", "/api/device/orb", "/api/device/pages",
             "/api/device/orb/0123456789abcdef0123.palm", "/api/device/firmware/pal-round.bin"]
    for p in paths:
        assert (await cl.get(p)).status == 401
        assert (await cl.get(p, headers=_auth())).status == 200
        assert ("GET", p) in state["proxied"]
    # anything else under /api/device stays closed
    assert (await cl.get("/api/device/orb/../../etc.palm", headers=_auth())).status == 404
    assert (await cl.post("/api/device/info", headers=_auth())).status == 404


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


# push notifications


@pytest.mark.asyncio
async def test_push_register_requires_token(client):
    cl, state = client
    assert (await cl.post("/api/pair/push-register")).status == 401   # no token
    good = await cl.post("/api/pair/push-register", headers=_auth(),
                         json={"platform": "android", "push_token": "x"})
    assert good.status == 200
    assert ("POST", "/api/pair/push-register") in state["proxied"]


@pytest.mark.asyncio
async def test_push_image_is_public_but_jpg_anchored(client):
    """The signed image URL is server-gated, so the gateway lets it through
    WITHOUT a token — but ONLY the /{id}.jpg shape (server checks the HMAC)."""
    cl, state = client
    ok = await cl.get("/api/push/image/5.jpg?exp=1&sig=abc")     # no auth header
    assert ok.status == 200
    assert state["status_hits"] == 0                             # not auth-checked
    # no extension / non-numeric id -> not allowlisted -> denied, never proxied
    assert (await cl.get("/api/push/image/5")).status == 404
    assert (await cl.get("/api/push/image/abc.jpg")).status == 404


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


# --- demo-only pairing passthrough (GATEWAY_ALLOW_PAIR_REDEEM) ----------------
# The public review demo needs /api/pair/redeem reachable over the tunnel so a
# reviewer can pair with the standing 000000 code. It is OFF by default (pinned
# by the default-deny parametrize above, which 404s POST /api/pair/redeem).

def test_pair_redeem_off_by_default_in_module():
    # The module loaded at import time (no env) must NOT allowlist redeem.
    assert gw_mod.ALLOW_PAIR_REDEEM is False
    assert not any(m == "POST" and rx.match("/api/pair/redeem")
                   for (m, rx, a, s) in gw_mod._ALLOWLIST)


@pytest.mark.asyncio
async def test_pair_redeem_passthrough_when_enabled(upstream, monkeypatch):
    """With GATEWAY_ALLOW_PAIR_REDEEM=1, redeem is proxied WITHOUT a token (the
    demo code in the body is the gate) — so a remote reviewer can pair."""
    state, _server = upstream
    monkeypatch.setenv("GATEWAY_ALLOW_PAIR_REDEEM", "1")
    mod = _load_gateway()                       # re-exec with the env set
    assert mod.ALLOW_PAIR_REDEEM is True
    monkeypatch.setattr(mod, "AI_SERVER_URL", state["url"])
    monkeypatch.setattr(mod, "AUTH_CACHE_TTL", 30)
    cl = TestClient(TestServer(mod.build_app()))
    await cl.start_server()
    try:
        resp = await cl.post("/api/pair/redeem", json={"code": "000000"})  # no token
        assert resp.status == 200
        assert ("POST", "/api/pair/redeem") in state["proxied"]
        assert state["status_hits"] == 0        # never auth-subrequested
        # A DIFFERENT unlisted pairing route is still denied even in demo mode.
        assert (await cl.post("/api/pair/request", headers=_auth())).status == 404
    finally:
        await cl.close()
    # Restore the shared module to deny-by-default for any later reload.
    monkeypatch.delenv("GATEWAY_ALLOW_PAIR_REDEEM", raising=False)
    _load_gateway()


# satellite HA cards + layouts

@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("GET", "/api/ha/bootstrap"), ("GET", "/api/ha/entities"), ("GET", "/api/ha/cards"),
    ("POST", "/api/ha/service"), ("POST", "/api/ha/ws"), ("POST", "/api/ha/template"),
    ("GET", "/api/ha/proxy"),
    ("GET", "/api/satellite/layout"), ("PUT", "/api/satellite/layout"),
    ("DELETE", "/api/satellite/layout"),
])
async def test_ha_card_routes_need_token(client, method, path):
    cl, state = client
    assert (await cl.request(method, path)).status == 401
    good = await cl.request(method, path, headers=_auth())
    assert good.status == 200
    assert (method, path) in state["proxied"]


@pytest.mark.asyncio
async def test_ha_proxy_accepts_query_token(client):
    """<img> can't send headers: camera snapshots authenticate via ?token=."""
    cl, state = client
    resp = await cl.get(f"/api/ha/proxy?token={GOOD}&path=/api/camera_proxy/camera.x")
    assert resp.status == 200


@pytest.mark.asyncio
async def test_ha_static_files_are_public(client):
    """Custom-card scripts load via <script type=module> (no token possible)."""
    cl, state = client
    resp = await cl.get("/hacsfiles/mushroom/mushroom.js")
    assert resp.status == 200
    assert (await cl.get("/local/house.png")).status == 200
    assert state["status_hits"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("GET", "/api/ha/states"),             # not an exposed HA route
    ("POST", "/api/ha/bootstrap"),         # wrong method
    ("POST", "/hacsfiles/x.js"),           # static files are GET-only
    ("GET", "/local/%2e%2e/api/speak"),    # traversal via the public prefix
    ("GET", "/hacsfiles/a/../../api/speak"),
    ("PATCH", "/api/satellite/layout"),
])
async def test_ha_card_surface_stays_tight(client, method, path):
    cl, state = client
    resp = await cl.request(method, path, headers=_auth())
    assert resp.status == 404
    assert state["proxied"] == []


# custom orbs

@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("GET", "/api/orbs"), ("POST", "/api/orbs"), ("POST", "/api/orbs/select"),
    ("GET", "/api/orbs/moon-a1b2c3/message"), ("PATCH", "/api/orbs/moon-a1b2c3"),
    ("DELETE", "/api/orbs/moon-a1b2c3"), ("PUT", "/api/orbs/moon-a1b2c3/media/idle"),
    ("DELETE", "/api/orbs/moon-a1b2c3/media/speaking"), ("GET", "/orbs/moon-a1b2c3/idle.mp4"),
])
async def test_orb_routes_need_token(client, method, path):
    cl, state = client
    assert (await cl.request(method, path)).status == 401
    assert (await cl.request(method, path, headers=_auth())).status == 200
    assert (method, path) in state["proxied"]


@pytest.mark.asyncio
async def test_orb_media_accepts_query_token(client):
    cl, _ = client
    assert (await cl.get(f"/orbs/moon-a1b2c3/idle.jpg?v=1&token={GOOD}")).status == 200


@pytest.mark.asyncio
async def test_large_orb_upload_passes_the_edge(client):
    """A multi-MB phone video must not hit aiohttp's 1 MiB default limit."""
    cl, state = client
    body = b"\x00" * (5 * 1024 * 1024)
    resp = await cl.put("/api/orbs/moon-a1b2c3/media/idle", data=body,
                        headers={**_auth(), "Content-Type": "video/mp4"})
    assert resp.status == 200
    assert ("PUT", "/api/orbs/moon-a1b2c3/media/idle") in state["proxied"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/orbs/x/../../api/speak", "/orbs/moon/index.json"])
async def test_orb_media_surface_tight(client, path):
    cl, state = client
    assert (await cl.get(path, headers=_auth())).status == 404


# --- voice notes ----------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("POST", "/api/satellite/note"),
    ("GET", "/api/notes"),
    ("DELETE", "/api/notes/42"),
])
async def test_notes_routes_need_token(client, method, path):
    cl, state = client
    assert (await cl.request(method, path)).status == 401
    assert (await cl.request(method, path, headers=_auth("bad"))).status == 401
    assert state["proxied"] == []
    good = await cl.request(method, path, headers=_auth())
    assert good.status == 200
    assert (method, path) in state["proxied"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("DELETE", "/api/notes"),              # bulk delete: not a thing
    ("DELETE", "/api/notes/abc"),
    ("PUT", "/api/notes/1"),
    ("GET", "/api/satellite/note"),
    ("POST", "/api/notes"),
])
async def test_notes_surface_tight(client, method, path):
    cl, state = client
    assert (await cl.request(method, path, headers=_auth())).status == 404
    assert state["proxied"] == []


@pytest.mark.asyncio
async def test_large_note_upload_is_streamed_through(client):
    """A multi-MB note (past aiohttp's 1 MiB default) passes, sent upstream
    chunked (streamed, not buffered whole)."""
    cl, state = client
    body = b"\x01\x00" * (6 * 1024 * 1024)            # 12 MiB
    resp = await cl.post("/api/satellite/note", data=body, headers={
        **_auth(), "X-Sample-Rate": "16000", "X-Sample-Format": "s16le"})
    assert resp.status == 200
    method, path, n, te = state["bodies"][-1]
    assert (method, path, n) == ("POST", "/api/satellite/note", len(body))
    assert te == "chunked"


@pytest.mark.asyncio
async def test_chunked_note_upload_from_client_passes(client):
    cl, state = client

    async def gen():
        for _ in range(40):
            yield b"\x00" * 32768

    resp = await cl.post("/api/satellite/note", data=gen(), headers=_auth())
    assert resp.status == 200
    assert state["bodies"][-1][2] == 40 * 32768


@pytest.mark.asyncio
async def test_note_upload_over_cap_is_413(client, monkeypatch):
    cl, state = client
    monkeypatch.setattr(gw_mod, "_STREAM_UPLOADS", [
        ("POST", gw_mod.re.compile(r"^/api/satellite/note$"), 1000, 30)])
    # declared Content-Length over the cap → rejected before proxying
    resp = await cl.post("/api/satellite/note", data=b"\x00" * 5000, headers=_auth())
    assert resp.status == 413

    # chunked (no Content-Length) over the cap → 413 mid-stream
    async def gen():
        for _ in range(10):
            yield b"\x00" * 500

    resp = await cl.post("/api/satellite/note", data=gen(), headers=_auth())
    assert resp.status == 413


# remote MCP (Claude connectors)

@pytest.mark.asyncio
async def test_remote_mcp_routes_off_by_default(client):
    cl, state = client
    for method, path in [("GET", "/.well-known/oauth-protected-resource"),
                         ("GET", "/.well-known/oauth-authorization-server"),
                         ("POST", "/oauth/register"), ("GET", "/oauth/authorize"),
                         ("POST", "/oauth/token"), ("POST", "/mcp")]:
        assert (await cl.request(method, path, headers=_auth())).status == 404
    assert state["proxied"] == []


@pytest.mark.asyncio
async def test_remote_mcp_routes_when_enabled(upstream, monkeypatch):
    """GATEWAY_ALLOW_MCP=1: the OAuth surface + /mcp pass WITHOUT a pairing
    token (the server enforces OAuth), /mcp is rewritten to /remote-mcp/mcp,
    and the LAN-only MCP + admin routes stay unreachable."""
    state, _server = upstream
    monkeypatch.setenv("GATEWAY_ALLOW_MCP", "1")
    mod = _load_gateway()
    assert mod.ALLOW_MCP is True
    monkeypatch.setattr(mod, "AI_SERVER_URL", state["url"])
    cl = TestClient(TestServer(mod.build_app()))
    await cl.start_server()
    try:
        for method, path in [("GET", "/.well-known/oauth-protected-resource"),
                             ("GET", "/.well-known/oauth-protected-resource/mcp"),
                             ("GET", "/.well-known/oauth-authorization-server"),
                             ("POST", "/oauth/register"), ("GET", "/oauth/authorize"),
                             ("POST", "/oauth/authorize"), ("POST", "/oauth/token")]:
            assert (await cl.request(method, path)).status == 200, path
            assert (method, path) in state["proxied"]
        r = await cl.post("/mcp?x=1", data=b'{"jsonrpc":"2.0"}')
        assert r.status == 200
        assert ("POST", "/remote-mcp/mcp") in state["proxied"]
        assert state["status_hits"] == 0          # no pairing-token subrequests
        # never exposed: the LAN MCP, the remote mount directly, admin + pairing
        for method, path in [("GET", "/mcp/sse"), ("POST", "/remote-mcp/mcp"),
                             ("GET", "/api/oauth/grants"), ("POST", "/api/oauth/revoke"),
                             ("POST", "/api/pair/request"), ("POST", "/api/pair/redeem")]:
            assert (await cl.request(method, path, headers=_auth())).status == 404, path
    finally:
        await cl.close()
    monkeypatch.delenv("GATEWAY_ALLOW_MCP", raising=False)
    _load_gateway()
