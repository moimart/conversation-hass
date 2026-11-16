"""Satellite gateway — an internet-exposable, token-gated reverse proxy.

Phones (satellites) reach PAL from anywhere through this container; the AI
server itself stays LAN-only. The gateway reimplements NO feature — it
forwards an explicit ALLOWLIST of satellite-capable routes (plus the /ws/ui
upgrade) to ai-server:8765 and rejects everything else with 404. Routes that
carry private actions require a valid pairing token, validated at the edge by
an auth subrequest to the server's own /api/pair/status (Bearer → 200/401),
with a short positive cache.

Security properties:
- Default-DENY: only routes in ALLOWLIST exist; the home-control surface
  (pairing mint/redeem, speak, display, volume, ptt, mcp, mqtt) is absent.
- No mirror mode: /ws/ui REQUIRES a valid token here (tokenless = LAN mirror,
  which would leak every household broadcast — never allowed publicly).
- Holds NO secrets: compromising this container yields only the network
  position any LAN device already has.
- Token never logged: ?token= query values are redacted from access logs.

Env:
  AI_SERVER_URL   upstream base, default http://ai-server:8765
  GATEWAY_PORT    listen port, default 8766
  AUTH_CACHE_TTL  seconds a validated token is trusted, default 30
  RATE_LIMIT_RPM  requests/min per client IP, default 240
  TRUST_CF_IP     read CF-Connecting-IP for the client IP, default "1"
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import defaultdict, deque

import aiohttp
from aiohttp import web

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("hal.gateway")

AI_SERVER_URL = os.environ.get("AI_SERVER_URL", "http://ai-server:8765").rstrip("/")
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "8766"))
AUTH_CACHE_TTL = float(os.environ.get("AUTH_CACHE_TTL", "30"))
RATE_LIMIT_RPM = int(os.environ.get("RATE_LIMIT_RPM", "240"))
TRUST_CF_IP = os.environ.get("TRUST_CF_IP", "1").strip().lower() in ("1", "true", "yes", "on")
# Demo-only: expose the pairing redeem endpoint through the gateway so an App
# Store reviewer can pair over the internet (a token can normally only be minted
# on the LAN). OFF by default — real deployments keep pairing home-side.
ALLOW_PAIR_REDEEM = os.environ.get("GATEWAY_ALLOW_PAIR_REDEEM", "").strip().lower() in ("1", "true", "yes", "on")
# Remote MCP for Claude connectors (server: oauth_mcp.py, PAL_REMOTE_MCP=1).
# OFF by default. When on, the OAuth endpoints + /mcp are exposed WITHOUT a
# pairing token: the SERVER enforces OAuth on /mcp (it must answer 401 with
# WWW-Authenticate itself, or Claude can't discover the auth server), and an
# authorization can only be approved with a code shown on a PAL screen.
ALLOW_MCP = os.environ.get("GATEWAY_ALLOW_MCP", "").strip().lower() in ("1", "true", "yes", "on")

# Hop-by-hop headers that must not be forwarded verbatim.
_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

# --- Route allowlist ---------------------------------------------------------
# Each entry: (method, regex, auth_required, streaming).
#   auth_required: a valid pairing token must be presented (Bearer or ?token=).
#   streaming:     proxy the upstream body in chunks (MJPEG) instead of buffering.
# Public routes (auth_required=False) expose NO private data: a healthcheck and
# the display theme assets the UI loads. Everything else needs a token.
_ALLOWLIST = [
    ("GET",  r"^/health$",                              False, False),
    ("GET",  r"^/api/themes$",                          False, False),
    ("GET",  r"^/themes/[^/]+/[^/]+$",                  False, False),

    ("GET",  r"^/api/pair/status$",                     True,  False),
    ("POST", r"^/api/command$",                         True,  False),
    ("GET",  r"^/api/satellite/tts$",                   True,  False),
    ("GET",  r"^/api/satellite/stream\.mjpeg$",        True,  True),
    ("POST", r"^/api/satellite/photo_frame/start$",     True,  False),
    ("POST", r"^/api/satellite/photo_frame/stop$",      True,  False),
    # Mirror shutter: a phone broadcasts a captured still to the home surfaces.
    ("POST", r"^/api/satellite/photo-broadcast$",       True,  False),
    # Server-side STT: a phone POSTs captured mic audio, gets back a transcript
    # (for devices whose on-device recognizer PAL can't use).
    ("POST", r"^/api/satellite/stt$",                   True,  False),
    # Voice notes: a device streams (often chunked) raw PCM while recording;
    # the server transcribes + stores it. The body is STREAMED upstream, not
    # buffered (see _STREAM_UPLOADS). List + delete for the Notes view.
    ("POST",   r"^/api/satellite/note$",                True,  False),
    ("GET",    r"^/api/notes$",                         True,  False),
    ("DELETE", r"^/api/notes/\d+$",                     True,  False),
    # The device's own TTS voice (pick from the TTS server's voices).
    # A phone asks online PAL Rounds to open Bluetooth pairing (BLE relay).
    ("POST", r"^/api/satellite/ble_pair$",              True,  False),
    ("GET",  r"^/api/satellite/voice$",                 True,  False),
    ("POST", r"^/api/satellite/voice$",                 True,  False),
    ("GET",  r"^/api/conversation/log$",                True,  False),
    ("GET",  r"^/api/conversation/log/image$",          True,  False),
    ("GET",  r"^/api/cloud_llm$",                        True,  False),
    ("POST", r"^/api/cloud_llm$",                        True,  False),
    ("POST", r"^/api/pair/push-register$",              True,  False),
    ("POST", r"^/api/pair/rename$",                     True,  False),
    # Intercom directory (token-gated). Call SIGNALING rides the already-proxied
    # /ws/ui socket; media is peer-to-peer (TURN), never through the gateway.
    ("GET",  r"^/api/intercom/devices$",                True,  False),
    # Push image: signature-gated by the SERVER (short-lived HMAC), so the
    # gateway exposes it tokenless — the .jpg anchor keeps the surface tight.
    ("GET",  r"^/api/push/image/\d+\.jpg$",             False, False),
    # Satellite HA cards (token-gated; the server also checks the `ha` scope).
    # Live state rides /ws/ui. Service calls ARE allowed remotely (user decision):
    # a token-holder can toggle HA entities directly, not only via the LLM.
    ("GET",  r"^/api/ha/(status|bootstrap|entities|history|forecast|dashboards|cards)$",
                                                        True,  False),
    ("POST", r"^/api/ha/(ws|service|template)$",        True,  False),
    ("GET",  r"^/api/ha/proxy$",                        True,  False),  # <img>: ?token=
    # HA's own static files for custom cards. Public, like on HA itself: module
    # scripts + their absolute sibling imports can't carry a token. The server
    # rejects traversal after decoding.
    ("GET",  r"^/(local|hacsfiles)/(?!.*\.\.)[^?#\\]+$", False, False),
    # This device's display layout (token-gated; `layout` scope server-side).
    ("GET",    r"^/api/satellite/layout$",              True,  False),
    ("PUT",    r"^/api/satellite/layout$",              True,  False),
    ("DELETE", r"^/api/satellite/layout$",              True,  False),
    # Custom orbs (token-gated; `orbs` scope server-side). Uploads are raw
    # video/image bodies (see client_max_size below).
    ("GET",    r"^/api/orbs$",                          True,  False),
    ("POST",   r"^/api/orbs(/select)?$",                True,  False),
    ("GET",    r"^/api/orbs/[a-z0-9-]+/message$",       True,  False),
    ("PATCH",  r"^/api/orbs/[a-z0-9-]+$",               True,  False),
    ("DELETE", r"^/api/orbs/[a-z0-9-]+$",               True,  False),
    ("PUT",    r"^/api/orbs/[a-z0-9-]+/media/[a-z]+$",  True,  False),
    ("DELETE", r"^/api/orbs/[a-z0-9-]+/media/[a-z]+$",  True,  False),
    ("GET",    r"^/orbs/[a-z0-9-]+/[a-z]+\.(mp4|jpg)$", True,  False),  # <video>: ?token=
    # Native satellites (PAL Round firmware; `device` scope server-side).
    ("GET",    r"^/api/device/(info|orb|pages)$",       True,  False),
    ("GET",    r"^/api/device/orb/[0-9a-f]{20}\.palm$", True,  False),
    ("GET",    r"^/api/device/firmware/[a-z0-9-]+\.bin$", True, False),
]
if ALLOW_PAIR_REDEEM:
    # Pre-auth: the redeem body's (demo) code is the gate, so no token here.
    _ALLOWLIST.append(("POST", r"^/api/pair/redeem$", False, False))
    log.info("gateway: /api/pair/redeem allowlisted (GATEWAY_ALLOW_PAIR_REDEEM)")
# Public path -> upstream path (the public MCP URL is <gateway>/mcp; the LAN
# server's own /mcp is the unauthenticated LAN MCP and must never be reached).
_REWRITES: dict[str, str] = {}
if ALLOW_MCP:
    _ALLOWLIST += [
        ("GET",    r"^/\.well-known/oauth-protected-resource(/mcp)?$", False, False),
        ("GET",    r"^/\.well-known/oauth-authorization-server$",      False, False),
        ("POST",   r"^/oauth/register$",                               False, False),
        ("GET",    r"^/oauth/authorize$",                              False, False),
        ("POST",   r"^/oauth/authorize$",                              False, False),
        ("POST",   r"^/oauth/token$",                                  False, False),
        ("POST",   r"^/mcp$",                                          False, True),
        ("GET",    r"^/mcp$",                                          False, True),
        ("DELETE", r"^/mcp$",                                          False, False),
    ]
    _REWRITES["/mcp"] = "/remote-mcp/mcp"
    log.info("gateway: remote MCP + OAuth allowlisted (GATEWAY_ALLOW_MCP)")
_ALLOWLIST = [(m, re.compile(p), a, s) for (m, p, a, s) in _ALLOWLIST]


def _upstream_path(request) -> str:
    """rel_url with any public→upstream path rewrite applied."""
    path = _REWRITES.get(request.path)
    if path is None:
        return str(request.rel_url)
    qs = request.query_string
    return path + (f"?{qs}" if qs else "")

# Allowlisted upload routes whose request body is STREAMED to the upstream
# (chunk by chunk, never held whole in memory) with their own byte cap, and a
# longer upstream timeout (the server transcribes before answering). aiohttp's
# client_max_size only governs request.read(), so the cap is enforced here.
# (method, regex) -> (max_body_bytes, upstream_timeout_s)
NOTE_MAX_BODY = 64 * 1024 * 1024
_STREAM_UPLOADS = [
    ("POST", re.compile(r"^/api/satellite/note$"), NOTE_MAX_BODY, 300),
]


class _BodyTooLarge(Exception):
    pass


async def _capped_body(content, cap: int):
    """Async generator forwarding the client's body in chunks; raises
    _BodyTooLarge once more than `cap` bytes have arrived."""
    seen = 0
    async for chunk in content.iter_chunked(65536):
        seen += len(chunk)
        if seen > cap:
            raise _BodyTooLarge()
        yield chunk


# /ws/ui is handled by a dedicated WebSocket route (always token-required).
_WS_PATH = "/ws/ui"

_TOKEN_QS = re.compile(r"(?i)(^|[?&])(token=)[^&]*")


def _redact(qs: str) -> str:
    return _TOKEN_QS.sub(r"\g<1>\g<2><redacted>", qs or "")


def _client_ip(request: web.Request) -> str:
    if TRUST_CF_IP:
        cf = request.headers.get("CF-Connecting-IP")
        if cf:
            return cf.split(",")[0].strip()
    return request.remote or "?"


def _extract_token(request: web.Request) -> str | None:
    """Bearer header first (the app's normal path), then ?token= (used by the
    <img> MJPEG tag and the WS upgrade, which can't set headers)."""
    auth = request.headers.get("Authorization") or ""
    if auth[:7].lower() == "bearer ":
        t = auth[7:].strip()
        if t:
            return t
    t = request.query.get("token")
    return t.strip() if t else None


def _forward_headers(request: web.Request) -> dict:
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in _HOP_HEADERS}
    # Preserve the real client IP for the server's logs/audit.
    headers["X-Forwarded-For"] = _client_ip(request)
    return headers


class AuthCache:
    """Positive-only LRU-ish cache of validated tokens. A revoked token stops
    working within AUTH_CACHE_TTL (negative results are never cached, so a
    freshly paired token is accepted on first use)."""

    def __init__(self, ttl: float):
        self._ttl = ttl
        self._ok: dict[str, float] = {}

    def fresh(self, token: str) -> bool:
        exp = self._ok.get(token)
        if exp is None:
            return False
        if exp < time.monotonic():
            self._ok.pop(token, None)
            return False
        return True

    def store(self, token: str) -> None:
        self._ok[token] = time.monotonic() + self._ttl
        if len(self._ok) > 4096:  # bound memory
            now = time.monotonic()
            self._ok = {k: v for k, v in self._ok.items() if v > now}


class RateLimiter:
    """Fixed-window-ish per-IP limiter (sliding 60s deque)."""

    def __init__(self, rpm: int):
        self._rpm = rpm
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, ip: str) -> bool:
        if self._rpm <= 0:
            return True
        now = time.monotonic()
        dq = self._hits[ip]
        while dq and dq[0] < now - 60:
            dq.popleft()
        if len(dq) >= self._rpm:
            return False
        dq.append(now)
        return True


class Gateway:
    def __init__(self):
        self._auth = AuthCache(AUTH_CACHE_TTL)
        self._rl = RateLimiter(RATE_LIMIT_RPM)
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, connect=5, sock_read=None))
        return self._session

    # --- auth ---------------------------------------------------------------
    async def _validate(self, token: str | None) -> bool:
        if not token:
            return False
        if self._auth.fresh(token):
            return True
        try:
            session = await self._ensure_session()
            async with session.get(
                f"{AI_SERVER_URL}/api/pair/status",
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    self._auth.store(token)
                    return True
                return False
        except Exception as e:
            log.warning(f"auth subrequest failed: {type(e).__name__}: {e}")
            return False

    # --- HTTP proxy ---------------------------------------------------------
    async def handle(self, request: web.Request) -> web.StreamResponse:
        ip = _client_ip(request)
        if not self._rl.allow(ip):
            return web.Response(status=429, text="rate limited")

        # CORS preflight: the app's WebView origin (capacitor://localhost) differs
        # from the gateway host, so any fetch carrying an Authorization header is
        # preceded by an OPTIONS preflight. Browsers strip credentials from
        # preflights, so it can't be token-gated — answer it WITHOUT auth, but
        # only for a (method, path) pair that IS in the allowlist (the browser
        # names the real method in Access-Control-Request-Method). Forward to the
        # server so its CORS policy produces the same headers as the LAN path.
        if request.method == "OPTIONS":
            return await self._handle_preflight(request, ip)

        match = next(
            (entry for entry in _ALLOWLIST
             if entry[0] == request.method and entry[1].match(request.path)),
            None,
        )
        if match is None:
            log.info(f"DENY {request.method} {request.path}?{_redact(request.query_string)} from {ip}")
            return web.Response(status=404, text="not found")

        _, _, auth_required, streaming = match
        if auth_required and not await self._validate(_extract_token(request)):
            log.info(f"401 {request.method} {request.path} from {ip}")
            return web.Response(status=401, text="unauthorized")

        target = f"{AI_SERVER_URL}{_upstream_path(request)}"
        headers = _forward_headers(request)
        session = await self._ensure_session()
        upload = next(((cap, tmo) for (m, rx, cap, tmo) in _STREAM_UPLOADS
                       if m == request.method and rx.match(request.path)), None)
        if upload is not None:
            return await self._proxy_upload(request, session, target, headers, *upload)
        body = await request.read() if request.body_exists else None

        try:
            if streaming:
                return await self._proxy_stream(request, session, target, headers, body)
            async with session.request(
                request.method, target, headers=headers, data=body,
                allow_redirects=False,
                # 100s: above /api/command wait_reply's 90s turn cap (the
                # watch's synchronous reply channel), below Cloudflare's ~100s
                # edge timeout. Still bounds a hung upstream.
                timeout=aiohttp.ClientTimeout(total=100),
            ) as up:
                out_headers = {k: v for k, v in up.headers.items()
                               if k.lower() not in _HOP_HEADERS}
                data = await up.read()
                return web.Response(status=up.status, body=data, headers=out_headers)
        except Exception as e:
            log.warning(f"proxy {request.method} {request.path} failed: {type(e).__name__}: {e}")
            return web.Response(status=502, text="bad gateway")

    async def _proxy_upload(self, request, session, target, headers,
                            cap: int, timeout_s: float) -> web.StreamResponse:
        """Forward a large (possibly chunked) upload by streaming the body
        upstream without buffering it; 413 past `cap`."""
        declared = request.content_length
        if declared is not None and declared > cap:
            return web.Response(status=413, text="payload too large")
        data = _capped_body(request.content, cap) if request.body_exists else None
        try:
            async with session.request(
                request.method, target, headers=headers, data=data,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as up:
                out_headers = {k: v for k, v in up.headers.items()
                               if k.lower() not in _HOP_HEADERS}
                return web.Response(status=up.status, body=await up.read(),
                                    headers=out_headers)
        except _BodyTooLarge:
            return web.Response(status=413, text="payload too large")
        except Exception as e:
            # aiohttp may wrap the generator's exception.
            if isinstance(getattr(e, "__cause__", None), _BodyTooLarge) or \
                    isinstance(getattr(e, "__context__", None), _BodyTooLarge):
                return web.Response(status=413, text="payload too large")
            log.warning(f"upload {request.method} {request.path} failed: {type(e).__name__}: {e}")
            return web.Response(status=502, text="bad gateway")

    async def _handle_preflight(self, request: web.Request, ip: str) -> web.StreamResponse:
        """Answer a CORS preflight for allowlisted routes (no token required)."""
        want = (request.headers.get("Access-Control-Request-Method") or "").upper()
        allowed = any(e[0] == want and e[1].match(request.path) for e in _ALLOWLIST)
        if not allowed:
            log.info(f"DENY OPTIONS {request.path}?{_redact(request.query_string)} from {ip}")
            return web.Response(status=404, text="not found")
        target = f"{AI_SERVER_URL}{request.rel_url}"
        session = await self._ensure_session()
        try:
            async with session.request(
                "OPTIONS", target, headers=_forward_headers(request),
                allow_redirects=False, timeout=aiohttp.ClientTimeout(total=10),
            ) as up:
                out_headers = {k: v for k, v in up.headers.items()
                               if k.lower() not in _HOP_HEADERS}
                return web.Response(status=up.status, body=await up.read(),
                                    headers=out_headers)
        except Exception as e:
            log.warning(f"preflight {request.path} failed: {type(e).__name__}: {e}")
            return web.Response(status=502, text="bad gateway")

    async def _proxy_stream(self, request, session, target, headers, body) -> web.StreamResponse:
        """Chunk-forward a streaming upstream (MJPEG) without buffering."""
        up = await session.request(
            request.method, target, headers=headers, data=body,
            allow_redirects=False, timeout=aiohttp.ClientTimeout(total=None, sock_read=None),
        )
        out = web.StreamResponse(status=up.status, headers={
            k: v for k, v in up.headers.items() if k.lower() not in _HOP_HEADERS
        })
        await out.prepare(request)
        try:
            async for chunk in up.content.iter_chunked(16384):
                await out.write(chunk)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        except Exception as e:
            log.debug(f"mjpeg stream ended: {type(e).__name__}: {e}")
        finally:
            up.close()
        return out

    # --- WebSocket proxy ----------------------------------------------------
    async def handle_ws(self, request: web.Request) -> web.StreamResponse:
        ip = _client_ip(request)
        if not self._rl.allow(ip):
            return web.Response(status=429, text="rate limited")
        # Public /ws/ui MUST present a valid token: tokenless connections are
        # LAN mirrors that receive every household broadcast — never exposed.
        token = _extract_token(request)
        if not await self._validate(token):
            log.info(f"WS 401 from {ip}")
            return web.Response(status=401, text="unauthorized")

        client_ws = web.WebSocketResponse(heartbeat=30, max_msg_size=0)
        await client_ws.prepare(request)
        session = await self._ensure_session()
        # Upstream keeps the ?token= so the server classifies it as a satellite.
        up_url = f"{AI_SERVER_URL.replace('http', 'ws', 1)}{request.rel_url}"
        try:
            async with session.ws_connect(up_url, heartbeat=30, max_msg_size=0) as up_ws:
                log.info(f"WS open {ip} (device satellite)")
                await asyncio.gather(
                    self._pump(client_ws, up_ws),
                    self._pump(up_ws, client_ws),
                )
        except Exception as e:
            log.warning(f"WS upstream failed: {type(e).__name__}: {e}")
        finally:
            if not client_ws.closed:
                await client_ws.close()
            log.info(f"WS close {ip}")
        return client_ws

    @staticmethod
    async def _pump(src, dst) -> None:
        try:
            async for msg in src:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await dst.send_str(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await dst.send_bytes(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                                  aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        except Exception:
            pass
        finally:
            if not dst.closed:
                await dst.close()

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


def build_app() -> web.Application:
    gw = Gateway()
    # Orb uploads carry whole phone videos (the server caps them at 100 MB);
    # aiohttp's default 1 MiB would reject them at the edge.
    app = web.Application(client_max_size=110 * 1024 * 1024)
    app.router.add_get(_WS_PATH, gw.handle_ws)
    # Everything else funnels through the allowlist handler.
    app.router.add_route("*", "/{tail:.*}", gw.handle)
    app.on_cleanup.append(lambda _app: gw.close())
    return app


def main() -> None:
    log.info(f"satellite gateway → {AI_SERVER_URL} on :{GATEWAY_PORT} "
             f"(auth_cache={AUTH_CACHE_TTL}s, rate={RATE_LIMIT_RPM}/min, cf_ip={TRUST_CF_IP})")
    web.run_app(build_app(), host="0.0.0.0", port=GATEWAY_PORT, access_log=None)


if __name__ == "__main__":
    main()
